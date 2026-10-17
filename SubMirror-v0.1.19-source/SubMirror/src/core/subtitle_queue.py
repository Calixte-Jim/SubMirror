# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Independent ASR / OCR subtitle queues.

Design
    * Audio and OCR subtitles are two completely independent channels; clearing
      one never touches the other.
    * Each channel keeps at most ``subtitle.queue_max`` items (schema caps this
      at 2) and drops the oldest entry when a third one arrives.
    * Every item carries its own expiry time. A small sweeper thread removes
      expired entries, so the queue works with or without a Qt event loop.
    * A single subtitle is clamped to ``asr.max_subtitle_chars`` characters
      (200 by default) with an ellipsis, protecting the overlay and the API.
    * Mutations are published through Qt signals only, and always emitted after
      the internal lock is released so a slot can safely call back in.

Safety: user-mode only. Pure in-memory bookkeeping; no injection, no memory
access, no hooks, no drivers.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SubtitleItem",
    "SubtitleQueue",
    "SubtitleManager",
    "truncate_text",
    "CHANNEL_ASR",
    "CHANNEL_OCR",
    "MAX_QUEUE_ITEMS",
    "MAX_SUBTITLE_CHARS",
]

logger = logging.getLogger("submirror.subtitle")

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
                    logger.exception("subtitle signal slot failed")

    def Signal(*_args, **_kwargs):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

CHANNEL_ASR = "asr"
CHANNEL_OCR = "ocr"

MAX_QUEUE_ITEMS = 2          # hard cap shared with config.SINGLE_QUEUE_MAX
MAX_SUBTITLE_CHARS = 200     # hard cap of one subtitle line
ELLIPSIS = "\u2026"

SWEEP_INTERVAL_S = 0.1
DEFAULT_TIMEOUT_S = 8.0

_ITEM_IDS = itertools.count(1)
_ITEM_ID_LOCK = threading.Lock()


def _next_item_id() -> int:
    with _ITEM_ID_LOCK:
        return next(_ITEM_IDS)


def truncate_text(text: str, limit: int = MAX_SUBTITLE_CHARS,
                  ellipsis: str = ELLIPSIS) -> str:
    """
    Clamp a single subtitle string to *limit* characters.

    Whitespace is collapsed first so an ASR result full of newlines cannot blow
    up the overlay layout. Returns "" for empty input.
    """
    if not text:
        return ""
    cleaned = " ".join(str(text).split())
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    if limit <= len(ellipsis):
        return cleaned[:limit]
    return cleaned[: limit - len(ellipsis)].rstrip() + ellipsis


# --------------------------------------------------------------------------- #
# Subtitle item
# --------------------------------------------------------------------------- #

@dataclass(eq=False)
class SubtitleItem:
    """One subtitle line with its own lifetime."""

    id: int
    channel: str
    original: str
    translated: str = ""
    failed: bool = False
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = 0.0
    # True while the line only carries the incremental (unfinished) source
    # text: the translation is still on its way and may replace it in place.
    pending: bool = False
    # Utterance number from the VAD, so a finished line can find the preview
    # line it belongs to and replace it instead of adding a second one.
    sequence: int = 0

    def is_expired(self, now: Optional[float] = None) -> bool:
        """True once this item's lifetime has elapsed."""
        moment = time.monotonic() if now is None else now
        return self.expires_at > 0.0 and moment >= self.expires_at

    def remaining(self, now: Optional[float] = None) -> float:
        """Seconds of life left (0.0 when expired)."""
        moment = time.monotonic() if now is None else now
        return max(0.0, self.expires_at - moment)

    def display_text(self, show_original: bool = False) -> str:
        """
        Text meant for the overlay.

        With *show_original* the source text is shown above the translation; a
        failed translation falls back to the original so nothing is lost.
        """
        source = self.original or ""
        target = (self.translated or "").strip()
        if not target or self.failed or target == source:
            return source
        if show_original and source and source != target:
            return source + "\n" + target
        return target


# --------------------------------------------------------------------------- #
# Single channel queue
# --------------------------------------------------------------------------- #

class SubtitleQueue(QObject):
    """
    Bounded, timed, thread-safe subtitle stack for one channel.

    Signals
        changed(channel)             any content change (re-render trigger)
        item_added(channel, item_id)
        item_expired(channel, item_id)
        cleared(channel)
    """

    changed = Signal(str)
    item_added = Signal(str, int)
    item_expired = Signal(str, int)
    cleared = Signal(str)

    def __init__(self, channel: str, config: Any = None, parent=None) -> None:
        super().__init__(parent)
        self._channel = channel
        self._config = config
        self._lock = threading.RLock()
        self._items: List[SubtitleItem] = []
        self._max_items = MAX_QUEUE_ITEMS
        self._timeout_s = DEFAULT_TIMEOUT_S
        self._max_chars = MAX_SUBTITLE_CHARS
        self._stop_event = threading.Event()
        self._sweeper: Optional[threading.Thread] = None
        self._stats: Dict[str, int] = {
            "added": 0, "dropped": 0, "expired": 0, "cleared": 0,
        }
        self.apply_config()

    # -- properties -------------------------------------------------------- #

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def is_started(self) -> bool:
        thread = self._sweeper
        return bool(thread and thread.is_alive())

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        """
        Re-read queue limits from config.

        Called at construction and whenever the settings panel saves, so new
        font/timeout/queue values take effect without a restart.
        """
        config = self._config
        max_items = MAX_QUEUE_ITEMS
        timeout_s = DEFAULT_TIMEOUT_S
        max_chars = MAX_SUBTITLE_CHARS
        if config is not None:
            try:
                max_items = int(config.get("subtitle.queue_max", MAX_QUEUE_ITEMS))
                timeout_s = float(config.get("subtitle.timeout_seconds", DEFAULT_TIMEOUT_S))
                max_chars = int(config.get("asr.max_subtitle_chars", MAX_SUBTITLE_CHARS))
            except (TypeError, ValueError):
                logger.warning("invalid subtitle settings, using defaults")

        with self._lock:
            self._max_items = max(1, min(int(max_items), MAX_QUEUE_ITEMS))
            self._timeout_s = max(0.1, float(timeout_s))
            # Honour the 200 character rule even if a hand-edited config is larger.
            self._max_chars = max(1, min(int(max_chars), MAX_SUBTITLE_CHARS))

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Start the background sweeper that retires expired subtitles."""
        with self._lock:
            if self.is_started:
                return
            self._stop_event.clear()
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                name="SubMirrorSubtitle-%s" % self._channel,
                daemon=True,
            )
            self._sweeper.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the sweeper; safe to call more than once."""
        self._stop_event.set()
        thread = self._sweeper
        if thread and thread.is_alive():
            thread.join(timeout)
        self._sweeper = None

    # -- mutation ---------------------------------------------------------- #

    def push(self, original: str, translated: str = "", failed: bool = False,
             timeout_s: Optional[float] = None, pending: bool = False,
             sequence: int = 0) -> Optional[SubtitleItem]:
        """
        Add one subtitle line.

        The strings are truncated to the configured limit; when the queue is at
        capacity the oldest entry is dropped. Returns None for empty input, so a
        blank ASR result never occupies a slot.  *pending* marks an incremental
        line whose translation has not arrived yet (see ``update``).
        """
        dropped: Optional[SubtitleItem] = None
        with self._lock:
            source = truncate_text(original or "", self._max_chars)
            target = truncate_text(translated or "", self._max_chars)
            if not source and not target:
                return None

            lifetime = self._timeout_s if timeout_s is None else max(0.1, float(timeout_s))
            item = SubtitleItem(
                id=_next_item_id(),
                channel=self._channel,
                original=source,
                translated=target,
                failed=bool(failed),
                expires_at=time.monotonic() + lifetime,
                pending=bool(pending),
                sequence=int(sequence or 0),
            )

            self._items.append(item)
            while len(self._items) > self._max_items:
                dropped = self._items.pop(0)

            self._stats["added"] += 1
            if dropped is not None:
                self._stats["dropped"] += 1

        if dropped is not None:
            self.item_expired.emit(self._channel, dropped.id)
        self.item_added.emit(self._channel, item.id)
        self.changed.emit(self._channel)
        return item

    def update(self, item_id: int, original: Optional[str] = None,
               translated: Optional[str] = None, failed: Optional[bool] = None,
               pending: Optional[bool] = None,
               timeout_s: Optional[float] = None,
               refresh_lifetime: bool = True) -> Optional[SubtitleItem]:
        """
        Change a line that is already on screen.

        This is what makes incremental output work: the preview line is pushed
        once and then rewritten as better text arrives, so the overlay never
        flickers between two competing lines.  Fields left as None keep their
        value.  Returns None when the item is gone (expired or cleared).
        """
        with self._lock:
            item = None
            for candidate in self._items:
                if candidate.id == item_id:
                    item = candidate
                    break
            if item is None:
                return None
            if original is not None:
                item.original = truncate_text(original or "", self._max_chars)
            if translated is not None:
                item.translated = truncate_text(translated or "", self._max_chars)
            if failed is not None:
                item.failed = bool(failed)
            if pending is not None:
                item.pending = bool(pending)
            if timeout_s is not None and float(timeout_s) > 0.0:
                item.expires_at = time.monotonic() + max(0.1, float(timeout_s))
            elif refresh_lifetime:
                item.expires_at = time.monotonic() + self._timeout_s
            self._stats["updated"] = self._stats.get("updated", 0) + 1
        self.changed.emit(self._channel)
        return item

    def clear(self, reason: str = "") -> int:
        """
        Remove every subtitle of this channel.

        The OCR channel calls this when the area OCR hotkey is released; the ASR
        channel is unaffected because the queues are independent.
        """
        with self._lock:
            removed = len(self._items)
            self._items.clear()
            if removed:
                self._stats["cleared"] += 1
        if removed:
            logger.debug("subtitle channel %s cleared (%s)", self._channel, reason or "n/a")
            self.cleared.emit(self._channel)
            self.changed.emit(self._channel)
        return removed

    def remove(self, item_id: int) -> bool:
        """Remove one subtitle by id; True when something was removed."""
        with self._lock:
            for index, item in enumerate(self._items):
                if item.id == item_id:
                    del self._items[index]
                    break
            else:
                return False
        self.changed.emit(self._channel)
        return True

    # -- read -------------------------------------------------------------- #

    def items(self) -> List[SubtitleItem]:
        """Snapshot of the queue, oldest first."""
        with self._lock:
            return list(self._items)

    def latest(self) -> Optional[SubtitleItem]:
        """Newest subtitle, or None when the channel is empty."""
        with self._lock:
            return self._items[-1] if self._items else None

    def is_empty(self) -> bool:
        with self._lock:
            return not self._items

    def display_texts(self, show_original: bool = False) -> List[str]:
        """
        Render-ready strings, oldest first (the overlay paints the newest last).
        """
        with self._lock:
            snapshot = list(self._items)
        return [item.display_text(show_original) for item in snapshot]

    # -- internals --------------------------------------------------------- #

    def _sweep_loop(self) -> None:
        """Retire expired subtitles until stop() is called."""
        while not self._stop_event.wait(SWEEP_INTERVAL_S):
            try:
                self._remove_expired()
            except Exception:
                logger.exception("subtitle sweeper failed for %s", self._channel)

    def _remove_expired(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [item for item in self._items if item.is_expired(now)]
            if expired:
                expired_ids = {item.id for item in expired}
                self._items = [i for i in self._items if i.id not in expired_ids]
                self._stats["expired"] += len(expired)

        for item in expired:
            self.item_expired.emit(self._channel, item.id)
        if expired:
            self.changed.emit(self._channel)


# --------------------------------------------------------------------------- #
# Manager for both channels
# --------------------------------------------------------------------------- #

class SubtitleManager(QObject):
    """
    Wraps the two independent subtitle channels used by the overlay.

    Signals
        asr_changed()   the ASR (top) line needs a repaint
        ocr_changed()   the OCR (bottom) line needs a repaint
    """

    asr_changed = Signal()
    ocr_changed = Signal()

    def __init__(self, config: Any = None, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self.asr = SubtitleQueue(CHANNEL_ASR, config)
        self.ocr = SubtitleQueue(CHANNEL_OCR, config)
        self.asr.changed.connect(lambda _channel: self.asr_changed.emit())
        self.ocr.changed.connect(lambda _channel: self.ocr_changed.emit())

    def start(self) -> None:
        """Start both sweepers."""
        self.asr.start()
        self.ocr.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop both sweepers during application exit."""
        self.asr.stop(timeout)
        self.ocr.stop(timeout)

    def apply_config(self) -> None:
        """Propagate live settings changes to both channels."""
        self.asr.apply_config()
        self.ocr.apply_config()

    def clear_ocr(self, reason: str = "region-release") -> int:
        """Clear the OCR line, e.g. when the area OCR hotkey is released."""
        return self.ocr.clear(reason)

    def clear_all(self, reason: str = "") -> None:
        """Clear both channels (used when the overlay is hidden or reset)."""
        self.asr.clear(reason)
        self.ocr.clear(reason)

    def display_texts(self, show_original: bool = False) -> Tuple[List[str], List[str]]:
        """Return (asr_lines, ocr_lines) ready for the overlay."""
        return (
            self.asr.display_texts(show_original),
            self.ocr.display_texts(show_original),
        )
