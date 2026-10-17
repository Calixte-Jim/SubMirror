# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Task scheduler, lane pools and throttling primitives.

Design
    * Work is grouped into named *lanes* (audio / vad / asr / ocr / translate).
      Each lane owns its own bounded queue and its own worker threads, so a slow
      subsystem can never stall another one.
    * Lane limits enforce the v0.1 throttling rules:
          asr  -> 1 worker, queue 1   (only one ASR task at a time)
          ocr  -> 1 worker, queue 1   (drop the stale frame, keep the newest)
      When a bounded queue is full the OLDEST pending item is discarded and
      reported through task_dropped; submitting never blocks the caller.
    * Every task receives a CancelToken as its first argument. Cancellation is
      cooperative: pending tasks are skipped, running tasks observe the token.
    * A failing task is contained: it only emits task_failed and is logged.
    * The scheduler talks to the UI exclusively through Qt signals, so worker
      threads never touch widgets.

Safety: user-mode only. No injection, no memory access, no hooks, no drivers.
"""

from __future__ import annotations

import collections
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

__all__ = [
    "TaskScheduler",
    "TaskHandle",
    "CancelToken",
    "TaskCancelled",
    "LaneConfig",
    "RateLimiter",
    "IntervalLimiter",
    "LANE_AUDIO",
    "LANE_VAD",
    "LANE_ASR",
    "LANE_OCR",
    "LANE_TRANSLATE",
    "DEFAULT_LANES",
    "build_lanes",
]

logger = logging.getLogger("submirror.scheduler")

# --------------------------------------------------------------------------- #
# Qt import with a minimal degraded fallback
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - import guard
    from PySide6.QtCore import QObject, Signal
    _HAS_QT = True
except Exception:  # pragma: no cover - degraded, non-Qt environments
    _HAS_QT = False

    class _FallbackSignal:
        def __init__(self, *_args, **_kwargs) -> None:
            self._slots: List[object] = []

        def connect(self, slot) -> None:
            self._slots.append(slot)

        def disconnect(self, slot=None) -> None:
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args) -> None:
            for slot in list(self._slots):
                try:
                    slot(*args)
                except Exception:
                    logger.exception("scheduler signal slot failed")

    def Signal(*_args, **_kwargs):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()


# --------------------------------------------------------------------------- #
# Lane names and default limits
# --------------------------------------------------------------------------- #

LANE_AUDIO = "audio"
LANE_VAD = "vad"
LANE_ASR = "asr"
LANE_OCR = "ocr"
LANE_TRANSLATE = "translate"


@dataclass
class LaneConfig:
    """Concurrency and backlog limits of one lane."""

    name: str
    workers: int = 1
    queue_max: int = 1          # 0 means unbounded
    drop_oldest: bool = True    # discard the oldest pending item when full

    def make_queue(self) -> "queue.Queue":
        return queue.Queue(maxsize=self.queue_max if self.queue_max > 0 else 0)


DEFAULT_LANES: Dict[str, LaneConfig] = {
    # Loopback capture hands chunks over quickly; keep only the newest chunk.
    LANE_AUDIO: LaneConfig(LANE_AUDIO, workers=1, queue_max=1, drop_oldest=True),
    # VAD may need a couple of chunks of context while one is being processed.
    LANE_VAD: LaneConfig(LANE_VAD, workers=1, queue_max=4, drop_oldest=True),
    # Hard rule: only one ASR task may run at any moment.
    LANE_ASR: LaneConfig(LANE_ASR, workers=1, queue_max=1, drop_oldest=True),
    # Hard rule: an OCR backlog of more than one frame drops the stale frame.
    LANE_OCR: LaneConfig(LANE_OCR, workers=1, queue_max=1, drop_oldest=True),
    # Translation may overlap a little, but stays narrow to avoid API bursts.
    LANE_TRANSLATE: LaneConfig(LANE_TRANSLATE, workers=2, queue_max=8, drop_oldest=True),
}

# Which "perf.<key>" config entry bounds each lane's backlog.
_QUEUE_MAX_KEYS: Dict[str, str] = {
    LANE_AUDIO: "audio_queue_max",
    LANE_VAD: "vad_queue_max",
    LANE_ASR: "asr_queue_max",
    LANE_OCR: "ocr_queue_max",
    LANE_TRANSLATE: "translate_queue_max",
}

# Which "perf.<key>" config entry overrides a lane's worker count. Only the
# translation lane is exposed: one request at a time keeps the public endpoint
# from being hammered and keeps the replies in order (v0.1.18 item 3).
_WORKER_KEYS: Dict[str, str] = {
    LANE_TRANSLATE: "translate_workers",
}


def build_lanes(config: Any = None) -> Dict[str, LaneConfig]:
    """
    Return the lane table, applying the "perf.*_queue_max" overrides.

    The defaults in DEFAULT_LANES are the tuned values; the config entries only
    let a user widen or narrow a backlog without touching the code. A missing
    or unreadable config keeps every default, so this never fails at startup.
    """
    lanes: Dict[str, LaneConfig] = {}
    for name, base in DEFAULT_LANES.items():
        queue_max = int(base.queue_max)
        workers = int(base.workers)
        if config is not None:
            key = _QUEUE_MAX_KEYS.get(name)
            if key:
                try:
                    raw = config.get("perf.%s" % key, queue_max)
                except Exception:
                    raw = queue_max
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    value = queue_max
                if value >= 0:
                    queue_max = value
            worker_key = _WORKER_KEYS.get(name)
            if worker_key:
                try:
                    raw_workers = config.get("perf.%s" % worker_key, workers)
                except Exception:
                    raw_workers = workers
                try:
                    worker_value = int(raw_workers)
                except (TypeError, ValueError):
                    worker_value = workers
                if worker_value >= 1:
                    workers = worker_value
        lanes[name] = LaneConfig(name, workers=workers,
                                 queue_max=queue_max,
                                 drop_oldest=base.drop_oldest)
    return lanes


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

class TaskCancelled(Exception):
    """Raised inside a task when its CancelToken was signalled."""


class CancelToken:
    """
    Cooperative cancellation flag handed to every submitted task.

    The token itself never interrupts a function; the task decides where it is
    safe to stop, which keeps file handles and audio buffers consistent.
    """

    __slots__ = ("_event", "reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "") -> None:
        """Signal cancellation; idempotent and thread safe."""
        if reason and not self.reason:
            self.reason = reason
        self._event.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until cancelled (or timeout); True when cancelled."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        """Raise TaskCancelled when the token has been signalled."""
        if self._event.is_set():
            raise TaskCancelled(self.reason or "cancelled")


_TASK_IDS = itertools.count(1)
_TASK_ID_LOCK = threading.Lock()


def _next_task_id() -> int:
    with _TASK_ID_LOCK:
        return next(_TASK_IDS)


@dataclass(eq=False)
class TaskHandle:
    """
    Reference to one submitted task.

    ``eq=False`` keeps identity semantics, so handles stay hashable inside the
    running-task sets of a lane.
    """

    id: int
    lane: str
    func: Callable[..., Any]
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    token: CancelToken = field(default_factory=CancelToken)
    submitted_at: float = field(default_factory=time.monotonic)
    started: bool = False
    finished: bool = False

    def cancel(self, reason: str = "user") -> None:
        """Request cancellation of this task."""
        self.token.cancel(reason)


# --------------------------------------------------------------------------- #
# Lane runtime
# --------------------------------------------------------------------------- #

class _Lane:
    """Internal runtime state of one lane."""

    def __init__(self, config: LaneConfig) -> None:
        self.config = config
        self.queue: "queue.Queue" = config.make_queue()
        self.threads: List[threading.Thread] = []
        self.condition = threading.Condition()
        self.running: Set[TaskHandle] = set()
        self.started = False
        self.stopping = False
        self.stats: Dict[str, int] = {
            "submitted": 0, "completed": 0, "failed": 0,
            "cancelled": 0, "dropped": 0,
        }

    def running_count(self) -> int:
        with self.condition:
            return len(self.running)

    def pending_count(self) -> int:
        return self.queue.qsize()

    def is_idle(self) -> bool:
        return self.running_count() == 0 and self.pending_count() == 0


_STOP = object()


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #

class TaskScheduler(QObject):
    """
    Lane based thread scheduler used by every background subsystem.

    Signals
        task_submitted(lane, task_id)
        task_started(lane, task_id)
        task_finished(lane, task_id)
        task_failed(lane, task_id, error)
        task_cancelled(lane, task_id)
        task_dropped(lane, task_id)
    """

    task_submitted = Signal(str, int)
    task_started = Signal(str, int)
    task_finished = Signal(str, int)
    task_failed = Signal(str, int, str)
    task_cancelled = Signal(str, int)
    task_dropped = Signal(str, int)

    def __init__(self, config=None, lanes: Optional[Dict[str, LaneConfig]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._shutdown_done = False
        self._lanes: Dict[str, _Lane] = {}
        for name, lane_config in (lanes or build_lanes(config)).items():
            self._lanes[name] = _Lane(lane_config)

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Start the worker threads of every known lane."""
        with self._lock:
            self._stopping.clear()
            for lane in self._lanes.values():
                self._ensure_started(lane)

    def shutdown(self, timeout: float = 3.0, cancel_pending: bool = True) -> None:
        """
        Stop every lane: cancel pending work, wake the workers and join them.

        Safe to call more than once and from the Qt main thread during exit.
        """
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._stopping.set()

            for lane in self._lanes.values():
                lane.stopping = True
                if cancel_pending:
                    self._drain_lane(lane, reason="shutdown")
                with lane.condition:
                    lane.running.clear() if False else None
                for handle in list(lane.running):
                    handle.token.cancel("shutdown")
                for _ in lane.threads:
                    self._force_put(lane, _STOP)

        deadline = time.monotonic() + max(timeout, 0.0)
        for lane in self._lanes.values():
            for thread in lane.threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(remaining)
        logger.info("task scheduler stopped")

    # -- submission -------------------------------------------------------- #

    def submit(self, lane_name: str, func: Callable[..., Any], *args: Any,
               **kwargs: Any) -> Optional[TaskHandle]:
        """
        Queue *func* on *lane_name*.

        The callable is invoked as ``func(token, *args, **kwargs)`` on a worker
        thread. Returns the TaskHandle, or None when the lane is shutting down.
        """
        if self._stopping.is_set():
            return None

        with self._lock:
            lane = self._lanes.get(lane_name)
            if lane is None:
                lane = self._lanes[lane_name] = _Lane(
                    LaneConfig(lane_name, workers=1, queue_max=1, drop_oldest=True)
                )
            self._ensure_started(lane)

        handle = TaskHandle(
            id=_next_task_id(), lane=lane_name, func=func, args=args, kwargs=kwargs
        )
        lane.stats["submitted"] += 1
        self.task_submitted.emit(lane_name, handle.id)

        if not self._offer(lane, handle):
            # Extremely rare: the queue is full and the new item is the one that
            # has to go. Report it instead of blocking the caller.
            handle.token.cancel("queue-full")
            lane.stats["dropped"] += 1
            logger.warning("lane %s queue full: dropped the new task #%d",
                           lane_name, handle.id)
            self.task_dropped.emit(lane_name, handle.id)
            return None
        return handle

    def cancel(self, handle: Optional[TaskHandle], reason: str = "user") -> None:
        """Cancel one task; safe with None."""
        if handle is not None:
            handle.cancel(reason)

    # -- lane maintenance -------------------------------------------------- #

    def reset_lane(self, lane_name: str, cancel_running: bool = True) -> int:
        """
        Drop pending work and optionally request cancellation of running tasks.

        Used by the settings panel buttons ("restart audio / OCR tasks") so a
        stuck subsystem can recover without restarting the application.
        Returns the number of pending tasks discarded.
        """
        with self._lock:
            lane = self._lanes.get(lane_name)
            if lane is None:
                return 0
            dropped = self._drain_lane(lane, reason="reset")
            if cancel_running:
                for handle in list(lane.running):
                    handle.token.cancel("reset")
        logger.info("lane %s reset, %d pending task(s) dropped", lane_name, dropped)
        return dropped

    def reset_all(self, cancel_running: bool = True) -> int:
        """Reset every lane; used by the one-click background task reset."""
        total = 0
        for name in list(self._lanes.keys()):
            total += self.reset_lane(name, cancel_running=cancel_running)
        return total

    def is_idle(self, lane_name: str) -> bool:
        """True when the lane has nothing running and nothing pending."""
        with self._lock:
            lane = self._lanes.get(lane_name)
        return True if lane is None else lane.is_idle()

    def wait_idle(self, lane_name: str, timeout: float = 5.0) -> bool:
        """Block until the lane is idle or *timeout* expires."""
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            lane = self._lanes.get(lane_name)
        if lane is None:
            return True
        while time.monotonic() < deadline:
            if lane.is_idle():
                return True
            time.sleep(0.02)
        return lane.is_idle()

    def pending_count(self, lane_name: str) -> int:
        """
        Pending items of one lane.

        Cheap on purpose: the lane table is immutable after construction, so
        neither the global lock nor a full snapshot is needed. The audio
        pipeline polls this to decide whether the slices have to grow.
        """
        lane = self._lanes.get(lane_name)
        if lane is None:
            return 0
        try:
            return int(lane.pending_count())
        except Exception:
            return 0

    def stats(self) -> Dict[str, Dict[str, int]]:
        """Snapshot counters for logging and the settings panel diagnostics."""
        # The lane table never changes after construction, so copying it under
        # the lock is enough: the per-lane counters are then read with the lock
        # released, so a busy submit() loop cannot stall the GUI thread that
        # calls this for its diagnostics.
        with self._lock:
            lanes = list(self._lanes.items())
        result: Dict[str, Dict[str, int]] = {}
        for name, lane in lanes:
            snapshot = dict(lane.stats)
            snapshot["running"] = lane.running_count()
            snapshot["pending"] = lane.pending_count()
            snapshot["workers"] = len(lane.threads)
            result[name] = snapshot
        return result

    # -- internals --------------------------------------------------------- #

    def _ensure_started(self, lane: _Lane) -> None:
        """Create the worker threads of a lane exactly once."""
        if lane.started or lane.stopping:
            return
        lane.started = True
        for index in range(max(1, lane.config.workers)):
            thread = threading.Thread(
                target=self._worker_loop,
                args=(lane,),
                name="SubMirror-%s-%d" % (lane.config.name, index + 1),
                daemon=True,
            )
            lane.threads.append(thread)
            thread.start()

    def _offer(self, lane: _Lane, handle: TaskHandle) -> bool:
        """Enqueue a handle, discarding the oldest pending item when full."""
        try:
            lane.queue.put_nowait(handle)
            return True
        except queue.Full:
            pass

        dropped: Any = None
        try:
            dropped = lane.queue.get_nowait()
        except queue.Empty:
            pass

        if dropped is _STOP:
            # Never lose the stop sentinel: put it back before the new item.
            self._force_put(lane, _STOP)
            dropped = None
        elif dropped is not None and lane.config.drop_oldest:
            dropped.token.cancel("superseded")
            lane.stats["dropped"] += 1
            # Overload is written down (v0.1.18 item 3): a dropped task must be
            # diagnosable in the log, not just visible as a counter.
            logger.warning("lane %s queue full (max %d): dropped the oldest task #%d",
                           lane.config.name, lane.config.queue_max, dropped.id)
            self.task_dropped.emit(lane.config.name, dropped.id)
        elif dropped is not None:
            # drop_newest policy: keep the old item and reject the new one.
            lane.queue.put_nowait(dropped)
            return False

        try:
            lane.queue.put_nowait(handle)
            return True
        except queue.Full:
            return False

    def _force_put(self, lane: _Lane, item: Any) -> None:
        """Enqueue an internal item (sentinel), dropping data if unavoidable."""
        while True:
            try:
                lane.queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    lane.queue.get_nowait()
                except queue.Empty:
                    return

    def _drain_lane(self, lane: _Lane, reason: str) -> int:
        """Cancel and remove every pending item of a lane."""
        dropped = 0
        while True:
            try:
                item = lane.queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            item.token.cancel(reason)
            lane.stats["cancelled"] += 1
            dropped += 1
            self.task_cancelled.emit(lane.config.name, item.id)
        return dropped

    def _worker_loop(self, lane: _Lane) -> None:
        """Consume one lane until the stop sentinel arrives."""
        lane_name = lane.config.name
        while True:
            try:
                item = lane.queue.get(timeout=0.25)
            except queue.Empty:
                if lane.stopping and lane.queue.empty():
                    return
                continue

            if item is _STOP:
                return

            handle: TaskHandle = item
            if handle.token.is_cancelled:
                lane.stats["cancelled"] += 1
                self.task_cancelled.emit(lane_name, handle.id)
                continue

            with lane.condition:
                lane.running.add(handle)
            handle.started = True
            self.task_started.emit(lane_name, handle.id)

            try:
                handle.func(handle.token, *handle.args, **handle.kwargs)
            except TaskCancelled:
                lane.stats["cancelled"] += 1
                self.task_cancelled.emit(lane_name, handle.id)
            except Exception as exc:
                # Containment: one broken task must not take down the lane.
                lane.stats["failed"] += 1
                logger.exception("task %s#%d failed", lane_name, handle.id)
                self.task_failed.emit(lane_name, handle.id, str(exc))
            else:
                if handle.token.is_cancelled:
                    lane.stats["cancelled"] += 1
                    self.task_cancelled.emit(lane_name, handle.id)
                else:
                    lane.stats["completed"] += 1
                    self.task_finished.emit(lane_name, handle.id)
            finally:
                handle.finished = True
                with lane.condition:
                    lane.running.discard(handle)
                    lane.condition.notify_all()


# --------------------------------------------------------------------------- #
# Throttling primitives
# --------------------------------------------------------------------------- #

class RateLimiter:
    """
    Sliding-window rate limiter.

    Used for the "too many invalid ASR results" guard and to keep translation
    requests from flooding a public endpoint.
    """

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self._max_calls = max(1, int(max_calls))
        self._period = max(0.001, float(period_seconds))
        self._events: "collections.deque[float]" = collections.deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """True when one more call fits inside the current window."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._events) >= self._max_calls:
                return False
            self._events.append(now)
            return True

    def reset(self) -> None:
        """Forget all recorded calls."""
        with self._lock:
            self._events.clear()

    def retry_after(self) -> float:
        """Seconds until the next call would be allowed (0.0 when allowed now)."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._events) < self._max_calls:
                return 0.0
            return max(0.0, self._period - (now - self._events[0]))

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] >= self._period:
            self._events.popleft()


class IntervalLimiter:
    """Minimum-interval gate, e.g. the fullscreen OCR 800 ms trigger limit."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._interval = max(0.0, float(min_interval_seconds))
        self._last = 0.0
        self._lock = threading.Lock()

    def set_interval(self, min_interval_seconds: float) -> None:
        self._interval = max(0.0, float(min_interval_seconds))

    def ready(self) -> bool:
        """True and consumes the slot when the interval has elapsed."""
        now = time.monotonic()
        with self._lock:
            if now - self._last < self._interval:
                return False
            self._last = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._last = 0.0
