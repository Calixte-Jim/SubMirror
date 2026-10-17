# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Windows WASAPI loopback capture pipeline (official entry).

Layering
    src/audio/loopback.py   backend adapters (pyaudiowpatch -> soundcard -> null),
                            device enumeration, plug/unplug watchdog, reconnect
    src/audio/ring_buffer.py fixed-capacity circular buffer + resampler + meter
    src/audio/capture_win.py  <-- this file: the pipeline the rest of the app uses

What this layer adds on top of the backend
    * transient ("spike") rejection - samples that stick out of the running
      baseline and last under SPIKE_MAX_MS are muted in place, which removes
      gunshots, clicks and buffer pops without eating speech
    * long-silence notice - logs once after silence_notice_seconds and keeps the
      ASR pipeline idle instead of feeding it empty jobs
    * a single fan-out interface (set_sink) so VAD / ring buffer consumers all
      see exactly the same post-filter frames
    * a three-state status feed (ok / busy / error) for the overlay indicator

Safety: user-mode WASAPI loopback of the speaker mix only. No process injection,
no memory access, no hooks, no drivers; we never request microphone permission.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.audio.dsp import (
    BURST_MARGIN_DB,
    BURST_MAX_MS,
    NOISE_REDUCTION_DB,
    BurstFilter,
    NoiseSuppressor,
)
from src.audio.loopback import LoopbackCapture
from src.audio.ring_buffer import (
    TARGET_SAMPLE_RATE,
    AudioFrame,
    LevelMeter,
    Resampler,
)

try:  # pragma: no cover - optional acceleration
    from PySide6.QtCore import QObject, Signal
    _HAS_QT = True
except Exception:  # pragma: no cover
    _HAS_QT = False

    class _FallbackSignal:
        def __init__(self, *_args, **_kwargs) -> None:
            self._slots: List[Any] = []

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
                    logging.getLogger("submirror.audio").exception("slot failed")

    def Signal(*_args, **_kwargs):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()

__all__ = [
    "AudioCaptureWorker",
    "WasapiLoopbackCapture",
    "CaptureSettings",
    "SpikeFilter",
    "SilenceMonitor",
    "BurstFilter",
    "NoiseSuppressor",
    "SOURCE_SPEAKER",
    "SOURCE_MICROPHONE",
    "SOURCE_MIXED",
    "SPIKE_MAX_MS",
]

logger = logging.getLogger("submirror.audio.capture")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

TARGET_RATE = TARGET_SAMPLE_RATE          # faster-whisper input rate, 16000
DEFAULT_BLOCK_MS = 30                     # requested read block
SPIKE_MAX_MS = 10.0                       # spec: transients shorter than this go
SPIKE_MARGIN_DB = 12.0                    # how far above baseline a spike must be
SILENCE_THRESHOLD_DBFS = -50.0
SILENCE_NOTICE_SECONDS = 30.0
STATUS_THROTTLE_S = 0.2
BASELINE_WINDOW = 32                      # blocks feeding the running median
RECONNECT_MAX_ATTEMPTS = 5
MIN_DBFS = -90.0

# -- v0.1.19 capture sources ------------------------------------------------ #
SOURCE_SPEAKER = "speaker"                # system sound only (default)
SOURCE_MICROPHONE = "microphone"          # default input only
SOURCE_MIXED = "mixed"                    # both directions, weighted
MIX_GRACE_MS = 250                        # secondary stream freshness window
MIX_TAIL_PAD = 0.0                        # reserved: no sample padding today

STATUS_OK = "ok"
STATUS_BUSY = "busy"
STATUS_ERROR = "error"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@dataclass
class CaptureSettings:
    """Capture-pipeline knobs, all overridable through config.json."""

    target_rate: int = TARGET_RATE
    block_ms: int = DEFAULT_BLOCK_MS
    spike_filter: bool = True
    spike_max_ms: float = SPIKE_MAX_MS
    spike_margin_db: float = SPIKE_MARGIN_DB
    silence_threshold_dbfs: float = SILENCE_THRESHOLD_DBFS
    silence_notice_seconds: float = SILENCE_NOTICE_SECONDS
    reconnect_max_attempts: int = RECONNECT_MAX_ATTEMPTS
    feed_when_silent: bool = True
    # -- v0.1.19: source selection, mixing and conditioning ----------------- #
    source: str = SOURCE_SPEAKER
    mic_device: str = ""
    mic_gain: float = 1.0
    speaker_gain: float = 1.0
    mix_grace_ms: int = MIX_GRACE_MS
    noise_suppression: bool = False
    noise_reduction_db: float = NOISE_REDUCTION_DB
    burst_filter: bool = False
    burst_max_ms: float = BURST_MAX_MS
    burst_margin_db: float = BURST_MARGIN_DB
    pause_capture_on_full: bool = True
    queue_limit: int = 3

    @classmethod
    def from_config(cls, config: Any) -> "CaptureSettings":
        """Read and clamp every value; bad input falls back to the default."""
        settings = cls()

        def read(key: Any, default: Any, cast: Callable[[Any], Any]) -> Any:
            """Read one key, or the first key of a tuple that is present.

            BL-1: the settings panel writes the public names (audio.reconnect_
            attempts, audio.silence_log_seconds, audio.spike_filter_ms); the
            older internal names stay readable so an existing config.json keeps
            working.
            """
            keys = (key,) if isinstance(key, str) else tuple(key)
            if config is None:
                return default
            for name in keys:
                try:
                    value = config.get(name, None)
                except Exception:
                    value = None
                if value is None:
                    continue
                try:
                    return cast(value)
                except Exception:
                    logger.debug("config key %s invalid, using %r", name, default)
                    return default
            return default

        settings.target_rate = int(read("audio.sample_rate", TARGET_RATE, int))
        settings.block_ms = max(10, min(200, int(read("audio.block_ms", DEFAULT_BLOCK_MS, int))))
        settings.spike_filter = bool(read("audio.spike_filter", True, bool))
        settings.spike_max_ms = max(1.0, min(50.0, float(read(
            ("audio.spike_filter_ms", "audio.spike_max_ms"), SPIKE_MAX_MS, float))))
        settings.spike_margin_db = max(6.0, min(40.0, float(read("audio.spike_margin_db", SPIKE_MARGIN_DB, float))))
        settings.silence_threshold_dbfs = max(
            -80.0, min(-20.0, float(read("audio.silence_dbfs", SILENCE_THRESHOLD_DBFS, float))))
        settings.silence_notice_seconds = max(5.0, float(read(
            ("audio.silence_log_seconds", "audio.silence_notice_seconds"),
            SILENCE_NOTICE_SECONDS, float)))
        settings.reconnect_max_attempts = max(0, int(read(
            ("audio.reconnect_attempts", "audio.reconnect_max_attempts"),
            RECONNECT_MAX_ATTEMPTS, int)))
        settings.feed_when_silent = bool(read("audio.feed_when_silent", True, bool))

        # -- v0.1.19 ------------------------------------------------------- #
        source = str(read("audio.source", SOURCE_SPEAKER, str) or SOURCE_SPEAKER).strip().lower()
        if source not in (SOURCE_SPEAKER, SOURCE_MICROPHONE, SOURCE_MIXED):
            logger.debug("config key audio.source=%r unknown, using %r", source, SOURCE_SPEAKER)
            source = SOURCE_SPEAKER
        settings.source = source
        settings.mic_device = str(read("audio.mic_device", "", str) or "")
        settings.mic_gain = max(0.0, min(4.0, float(read("audio.mic_gain", 1.0, float))))
        settings.speaker_gain = max(0.0, min(4.0, float(read("audio.speaker_gain", 1.0, float))))
        settings.mix_grace_ms = max(50, min(2000, int(read("audio.mix_grace_ms", MIX_GRACE_MS, int))))
        settings.noise_suppression = bool(read("audio.noise_suppression", False, bool))
        settings.noise_reduction_db = max(
            3.0, min(40.0, float(read("audio.noise_reduction_db", NOISE_REDUCTION_DB, float))))
        settings.burst_filter = bool(read("audio.burst_filter", False, bool))
        settings.burst_max_ms = max(
            2.0, min(200.0, float(read("audio.burst_max_ms", BURST_MAX_MS, float))))
        settings.burst_margin_db = max(
            6.0, min(40.0, float(read("audio.burst_margin_db", BURST_MARGIN_DB, float))))
        settings.pause_capture_on_full = bool(read("perf.pause_capture_on_full", True, bool))
        settings.queue_limit = max(1, min(64, int(read("perf.asr_queue_max", 3, int))))
        if settings.target_rate < 8000:
            settings.target_rate = TARGET_RATE
        return settings

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target_rate": self.target_rate,
            "block_ms": self.block_ms,
            "spike_filter": self.spike_filter,
            "spike_max_ms": self.spike_max_ms,
            "spike_margin_db": self.spike_margin_db,
            "silence_threshold_dbfs": self.silence_threshold_dbfs,
            "silence_notice_seconds": self.silence_notice_seconds,
            "reconnect_max_attempts": self.reconnect_max_attempts,
            "feed_when_silent": self.feed_when_silent,
            "source": self.source,
            "mic_device": self.mic_device,
            "mic_gain": self.mic_gain,
            "speaker_gain": self.speaker_gain,
            "mix_grace_ms": self.mix_grace_ms,
            "noise_suppression": self.noise_suppression,
            "noise_reduction_db": self.noise_reduction_db,
            "burst_filter": self.burst_filter,
            "burst_max_ms": self.burst_max_ms,
            "burst_margin_db": self.burst_margin_db,
            "pause_capture_on_full": self.pause_capture_on_full,
            "queue_limit": self.queue_limit,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _dbfs(samples: Sequence[float]) -> float:
    """dBFS of a sample block (-inf clamped to MIN_DBFS)."""
    try:
        rms = LevelMeter.rms(samples)
    except Exception:
        return MIN_DBFS
    if rms <= 1e-7:
        return MIN_DBFS
    import math
    return max(MIN_DBFS, 20.0 * math.log10(rms))


def _as_list(samples: Any) -> List[float]:
    """Cheap float view without forcing a numpy dependency."""
    if samples is None:
        return []
    if hasattr(samples, "tolist"):
        return [float(v) for v in samples.tolist()]
    return [float(v) for v in samples]


def _runs_above(values: Sequence[float], gate: float) -> List[Tuple[int, int]]:
    """
    Half-open index ranges whose |value| stays above *gate*.

    Implemented as a plain scan so the filter works without numpy; a 30 ms block
    is 480 samples, so the cost is irrelevant next to the WASAPI read itself.
    """
    runs: List[Tuple[int, int]] = []
    start = -1
    for index, value in enumerate(values):
        loud = abs(value) > gate
        if loud and start < 0:
            start = index
        elif not loud and start >= 0:
            runs.append((start, index))
            start = -1
    if start >= 0:
        runs.append((start, len(values)))
    return runs


# --------------------------------------------------------------------------- #
# Spike filter
# --------------------------------------------------------------------------- #

class SpikeFilter:
    """
    Drop sub-10 ms transients that sit far above the running audio baseline.

    The baseline is the median RMS of the last few blocks, so a loud but
    *sustained* passage (gunfire, explosion, music) raises the baseline and is
    never mistaken for a spike; an isolated click inside near-silence is tens of
    dB above it and gets muted. Muted samples are zeroed in place rather than
    removed, keeping the timeline aligned for VAD and Whisper.
    """

    def __init__(self, sample_rate: int = TARGET_RATE, max_ms: float = SPIKE_MAX_MS,
                 margin_db: float = SPIKE_MARGIN_DB, enabled: bool = True) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.max_ms = float(max_ms)
        self.margin_db = float(margin_db)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._history: deque = deque(maxlen=BASELINE_WINDOW)
        self._baseline_db = MIN_DBFS
        self._spikes = 0
        self._muted_samples = 0

    # -- state ------------------------------------------------------------- #

    @property
    def baseline_db(self) -> float:
        with self._lock:
            return self._baseline_db

    def configure(self, sample_rate: Optional[int] = None, max_ms: Optional[float] = None,
                  margin_db: Optional[float] = None, enabled: Optional[bool] = None) -> None:
        """Apply live settings changes."""
        with self._lock:
            if sample_rate:
                self.sample_rate = max(1, int(sample_rate))
            if max_ms is not None:
                self.max_ms = max(0.5, float(max_ms))
            if margin_db is not None:
                self.margin_db = max(0.0, float(margin_db))
            if enabled is not None:
                self.enabled = bool(enabled)

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._baseline_db = MIN_DBFS

    def stats(self) -> Dict[str, float]:
        with self._lock:
            return {"spikes": self._spikes, "muted_samples": self._muted_samples,
                    "baseline_db": round(self._baseline_db, 2)}

    # -- processing -------------------------------------------------------- #

    def process(self, samples: Any) -> Tuple[Any, int]:
        """
        Return ``(maybe_filtered_block, muted_sample_count)``.

        Never raises: filtering is best-effort, and a failure must not stop the
        capture stream (spec: a bad frame is dropped, not fatal).
        """
        try:
            values = _as_list(samples)
            if not values:
                return samples, 0
            level_db = _dbfs(values)
            with self._lock:
                enabled = self.enabled
                if enabled:
                    self._history.append(level_db)
                baseline = statistics.median(self._history) if self._history else level_db
                self._baseline_db = float(baseline)
                margin = self.margin_db
                max_span = int(self.sample_rate * self.max_ms / 1000.0)

            if not enabled or max_span < 1:
                return samples, 0

            gate_linear = 10.0 ** ((baseline + margin) / 20.0)
            # A block that never exceeds the gate cannot contain a spike.
            if max(abs(v) for v in values) <= gate_linear:
                return samples, 0

            muted = 0
            for start, end in _runs_above(values, gate_linear):
                if (end - start) <= max_span:
                    for index in range(start, end):
                        values[index] = 0.0
                    muted += end - start

            if muted:
                with self._lock:
                    self._spikes += 1
                    self._muted_samples += muted
            return values, muted
        except Exception:
            logger.exception("spike filter failed; passing the block through")
            return samples, 0


# --------------------------------------------------------------------------- #
# Silence monitor
# --------------------------------------------------------------------------- #

class SilenceMonitor:
    """Tracks continuous silence and reports it once per silent stretch."""

    def __init__(self, threshold_dbfs: float = SILENCE_THRESHOLD_DBFS,
                 notice_after_s: float = SILENCE_NOTICE_SECONDS) -> None:
        self.threshold_dbfs = float(threshold_dbfs)
        self.notice_after_s = float(notice_after_s)
        self._silent_since: Optional[float] = None
        self._notified = False
        self._lock = threading.RLock()

    def update(self, level_db: float, now: Optional[float] = None) -> Optional[float]:
        """
        Feed one block level.

        Returns the silence duration the first time the notice threshold is
        crossed, else None. Sound resets the tracker.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            if level_db > self.threshold_dbfs:
                self._silent_since = None
                self._notified = False
                return None
            if self._silent_since is None:
                self._silent_since = moment
                return None
            elapsed = moment - self._silent_since
            if elapsed >= self.notice_after_s and not self._notified:
                self._notified = True
                return elapsed
            return None

    @property
    def is_silent(self) -> bool:
        with self._lock:
            return self._silent_since is not None

    @property
    def silent_seconds(self) -> float:
        with self._lock:
            if self._silent_since is None:
                return 0.0
            return time.monotonic() - self._silent_since

    def reset(self) -> None:
        with self._lock:
            self._silent_since = None
            self._notified = False

    def configure(self, threshold_dbfs: Optional[float] = None,
                  notice_after_s: Optional[float] = None) -> None:
        with self._lock:
            if threshold_dbfs is not None:
                self.threshold_dbfs = float(threshold_dbfs)
            if notice_after_s is not None:
                self.notice_after_s = float(notice_after_s)


# --------------------------------------------------------------------------- #
# Capture worker
# --------------------------------------------------------------------------- #

class AudioCaptureWorker(LoopbackCapture):
    """
    Official capture entry point: source chain + conditioning + fan-out.

    Additional signals
        frame_ready(object)        post-filter AudioFrame (16 kHz mono)
        silence_notice(float)      silence has lasted this many seconds
        status_changed(str, str)   state ("ok"/"busy"/"error") and display text
        spike_dropped(int)         samples muted in the last block
        capture_paused(bool)       backpressure paused/resumed the capture
    """

    frame_ready = Signal(object)
    silence_notice = Signal(float)
    status_changed = Signal(str, str)
    spike_dropped = Signal(int)
    capture_paused = Signal(bool)

    def __init__(self, config: Any = None, ring_buffer: Any = None,
                 kind: Optional[str] = None) -> None:
        settings = CaptureSettings.from_config(config)
        super().__init__(config=config, ring_buffer=ring_buffer,
                         kind=kind or self._primary_kind(settings))
        self.settings = settings
        self.spike_filter = SpikeFilter(
            sample_rate=self.settings.target_rate,
            max_ms=self.settings.spike_max_ms,
            margin_db=self.settings.spike_margin_db,
            enabled=self.settings.spike_filter,
        )
        #: mode-driven burst rejection (game mode) and microphone conditioning
        self.burst_filter = BurstFilter(
            sample_rate=self.settings.target_rate,
            max_ms=self.settings.burst_max_ms,
            margin_db=self.settings.burst_margin_db,
            enabled=self.settings.burst_filter,
        )
        self.noise_suppressor = NoiseSuppressor(
            sample_rate=self.settings.target_rate,
            reduction_db=self.settings.noise_reduction_db,
            enabled=self.settings.noise_suppression,
        )
        self.silence_monitor = SilenceMonitor(
            threshold_dbfs=self.settings.silence_threshold_dbfs,
            notice_after_s=self.settings.silence_notice_seconds,
        )
        self._sink: Optional[Callable[[AudioFrame], None]] = None
        self._lock = threading.RLock()
        self._pipeline_stats: Dict[str, float] = {
            "blocks": 0, "muted_samples": 0, "frames_out": 0,
            "silence_notices": 0, "reconnects": 0,
            "paused_blocks": 0, "pauses": 0, "resumes": 0,
            "secondary_blocks": 0, "mix_windows": 0, "mix_stale": 0,
        }
        self._last_status = (STATUS_OK, "")
        self._last_status_at = 0.0
        # -- v0.1.19: backpressure and the optional secondary stream ------- #
        self._paused = False
        self._pause_reason = ""
        self._queue_probe: Optional[Callable[[], int]] = None
        self._queue_limit = int(getattr(self.settings, "queue_limit", 3) or 3)
        self._secondary: Optional[LoopbackCapture] = None
        self._secondary_block: Any = None
        self._secondary_at = 0.0
        self._primary_gain, self._secondary_gain = self._mix_gains(self.settings)
        self._wire_backend_signals()
        self._ensure_secondary()

    # -- source selection (v0.1.19) ---------------------------------------- #

    @staticmethod
    def _primary_kind(settings: CaptureSettings) -> str:
        """Direction of the primary stream for the configured source."""
        if settings.source == SOURCE_MICROPHONE:
            return "microphone"
        if settings.source == SOURCE_MIXED:
            # "Prefer the microphone" is expressed by the weights, so the
            # louder side becomes the primary stream and therefore also the
            # one whose device selector and ring buffer drive the pipeline.
            return "microphone" if settings.mic_gain >= settings.speaker_gain else "loopback"
        return "loopback"

    @staticmethod
    def _mix_gains(settings: CaptureSettings) -> Tuple[float, float]:
        """``(primary_gain, secondary_gain)`` for the configured source."""
        primary_is_mic = AudioCaptureWorker._primary_kind(settings) == "microphone"
        if primary_is_mic:
            return settings.mic_gain, settings.speaker_gain
        return settings.speaker_gain, settings.mic_gain

    def _ensure_secondary(self) -> None:
        """Open the opposite direction when the source asks for a mix."""
        wanted = self.settings.source == SOURCE_MIXED
        if not wanted:
            if self._secondary is not None:
                try:
                    self._secondary.set_block_sink(None)
                    self._secondary.stop("source-changed")
                except Exception:
                    logger.debug("secondary stop failed", exc_info=True)
                self._secondary = None
            return
        if self._secondary is not None:
            return
        kind = "loopback" if self.is_microphone else "microphone"
        try:
            secondary = LoopbackCapture(config=self._config, kind=kind)
            secondary.set_block_sink(self._on_secondary_audio)
            self._secondary = secondary
            logger.info("mix source armed: primary=%s secondary=%s", self.kind, kind)
        except Exception:
            logger.exception("could not arm the secondary capture stream")
            self._secondary = None

    # -- backpressure (v0.1.19) -------------------------------------------- #

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def pause_reason(self) -> str:
        with self._lock:
            return self._pause_reason

    def set_queue_probe(self, probe: Optional[Callable[[], int]],
                        limit: Optional[int] = None) -> None:
        """
        Register the backlog probe used to pause capture when the ASR lane is
        full ("满载暂停音频采集"). The probe is called once per block and must be
        cheap and non-blocking; a failure simply stops pausing.
        """
        self._queue_probe = probe
        if limit is not None:
            self._queue_limit = max(1, int(limit))

    def pause_capture(self, reason: str = "backpressure") -> bool:
        """Stop feeding the pipeline (the device stream stays open)."""
        with self._lock:
            if self._paused:
                return False
            self._paused = True
            self._pause_reason = reason
            self._pipeline_stats["pauses"] += 1
        logger.info("capture paused (%s): backlog reached the queue limit", reason)
        self._emit_status(STATUS_BUSY, self._text("overlay.status.capture_paused",
                                                  "队列已满，暂停采集"), force=True)
        try:
            self.capture_paused.emit(True)
        except Exception:
            logger.debug("capture_paused emit failed", exc_info=True)
        return True

    def resume_capture(self, reason: str = "drained") -> bool:
        """Resume feeding after a pause; the ring buffer keeps its gap."""
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
            self._pause_reason = ""
            self._pipeline_stats["resumes"] += 1
        self.silence_monitor.reset()
        logger.info("capture resumed (%s)", reason)
        try:
            self.capture_paused.emit(False)
        except Exception:
            logger.debug("capture_paused emit failed", exc_info=True)
        return True

    def _check_backpressure(self) -> None:
        """Pause on a full lane, resume once it drains (probe-driven)."""
        probe = self._queue_probe
        if probe is None or not self.settings.pause_capture_on_full:
            return
        try:
            pending = int(probe())
        except Exception:
            logger.debug("queue probe failed", exc_info=True)
            return
        limit = max(1, int(self._queue_limit or 1))
        if pending >= limit and not self.is_paused:
            self.pause_capture("queue-full:%d" % pending)
        elif pending < limit and self.is_paused:
            self.resume_capture("queue-drained:%d" % pending)

    def _text(self, key: str, fallback: str) -> str:
        """Localised status text, with a hard fallback (never raises)."""
        config = self._config
        getter = getattr(config, "t", None)
        if callable(getter):
            try:
                value = getter(key)
                if value:
                    return str(value)
            except Exception:
                logger.debug("could not translate %s", key, exc_info=True)
        return fallback

    # -- secondary stream (v0.1.19) ---------------------------------------- #

    def _on_secondary_audio(self, raw: Any, channels: int, sample_rate: int) -> None:
        """Ingress of the opposite direction: condition, then park one block."""
        if self.is_paused:
            return
        try:
            source_rate = int(sample_rate or self.settings.target_rate)
            mono = Resampler.to_mono(raw, channels)
            if source_rate != self.settings.target_rate:
                mono = Resampler.resample(mono, source_rate, self.settings.target_rate)
            if self.settings.noise_suppression:
                mono = self.noise_suppressor.process(mono)
            with self._lock:
                self._secondary_block = mono
                self._secondary_at = time.monotonic()
                self._pipeline_stats["secondary_blocks"] += 1
        except Exception:
            logger.exception("secondary stream block failed (dropped)")

    def _mix_blocks(self, primary: Any) -> Any:
        """Sum the primary block with the freshest secondary block, weighted."""
        secondary = self._secondary
        if secondary is None:
            return primary
        with self._lock:
            block = self._secondary_block
            seen_at = self._secondary_at
        if block is None:
            return primary
        age_ms = (time.monotonic() - seen_at) * 1000.0
        if age_ms > max(50, int(self.settings.mix_grace_ms)):
            # A stale peer would smear the timeline; the primary stands alone.
            with self._lock:
                self._pipeline_stats["mix_stale"] += 1
            return primary
        try:
            primary_values = _as_list(primary)
            secondary_values = _as_list(block)
            length = min(len(primary_values), len(secondary_values))
            if length <= 0:
                return primary
            mixed = [primary_values[i] * self._primary_gain
                     + secondary_values[i] * self._secondary_gain
                     for i in range(length)]
            with self._lock:
                self._pipeline_stats["mix_windows"] += 1
            return mixed
        except Exception:
            logger.exception("mixing failed; using the primary block")
            return primary

    # -- wiring ------------------------------------------------------------ #

    def _wire_backend_signals(self) -> None:
        """Mirror backend lifecycle onto the three-state status feed."""
        self._connect(getattr(self, "error", None), self._on_backend_error)
        self._connect(getattr(self, "started", None), self._on_backend_started)
        self._connect(getattr(self, "stopped", None), self._on_backend_stopped)
        self._connect(getattr(self, "device_changed", None), self._on_device_changed)
        self._connect(getattr(self, "reconnected", None), self._on_reconnected)

    @staticmethod
    def _connect(signal: Any, slot: Callable[..., None]) -> None:
        """Connect when the signal exists; a missing one is not an error."""
        if signal is None:
            return
        try:
            signal.connect(slot)
        except Exception:
            logger.debug("could not connect signal %r", signal, exc_info=True)

    # -- sink -------------------------------------------------------------- #

    def set_sink(self, sink: Optional[Callable[[AudioFrame], None]]) -> None:
        """
        Register the single downstream consumer (normally the VAD pipeline).

        Every post-filter frame is delivered here exactly once, so the ring
        buffer, the VAD and any future consumer never drift apart.
        """
        self._sink = sink

    def clear_sink(self) -> None:
        self._sink = None

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        """Reload settings live (settings panel save / one-click reset)."""
        previous_kind = self.kind
        self.settings = CaptureSettings.from_config(self._config)
        self.spike_filter.configure(
            sample_rate=self.settings.target_rate,
            max_ms=self.settings.spike_max_ms,
            margin_db=self.settings.spike_margin_db,
            enabled=self.settings.spike_filter,
        )
        self.burst_filter.configure(
            sample_rate=self.settings.target_rate,
            max_ms=self.settings.burst_max_ms,
            margin_db=self.settings.burst_margin_db,
            enabled=self.settings.burst_filter,
        )
        self.noise_suppressor.configure(
            sample_rate=self.settings.target_rate,
            reduction_db=self.settings.noise_reduction_db,
            enabled=self.settings.noise_suppression,
        )
        self.silence_monitor.configure(
            threshold_dbfs=self.settings.silence_threshold_dbfs,
            notice_after_s=self.settings.silence_notice_seconds,
        )
        self._queue_limit = int(self.settings.queue_limit or 3)
        self._primary_gain, self._secondary_gain = self._mix_gains(self.settings)
        if not self.settings.pause_capture_on_full and self.is_paused:
            self.resume_capture("pause-disabled")
        self._ensure_secondary()
        if self.kind != previous_kind:
            # Switching direction needs a fresh stream on the right endpoints.
            logger.info("capture direction changed: %s -> %s", previous_kind, self.kind)
            with self._lock:
                self._device_selector = None
        parent_apply = getattr(super(), "apply_config", None)
        if callable(parent_apply):
            try:
                parent_apply()
            except Exception:
                logger.exception("backend apply_config failed")

    def reset_background_tasks(self) -> bool:
        """
        One-click restart of the audio background tasks (no app restart).

        Clears the filters and meter, then restarts the backend stream; the UI
        and the OCR chain are untouched.
        """
        logger.info("resetting audio capture pipeline")
        self.spike_filter.reset()
        self.burst_filter.reset()
        self.noise_suppressor.reset()
        self.silence_monitor.reset()
        if self.is_paused:
            self.resume_capture("manual-reset")
        try:
            self._meter.reset()
        except Exception:
            pass
        restart = getattr(self, "restart", None)
        try:
            if callable(restart):
                return bool(restart("manual-reset"))
        except Exception:
            logger.exception("audio restart failed")
            return False
        return False

    # -- lifecycle (v0.1.19: the primary stream plus an optional peer) ------ #

    def start(self) -> bool:
        """Start the primary stream, then the mixing peer when one is armed."""
        started = bool(super().start())
        secondary = self._secondary
        if started and secondary is not None:
            try:
                if not secondary.start():
                    logger.warning("secondary %s stream did not start; "
                                   "continuing with the primary only", secondary.kind)
            except Exception:
                logger.exception("secondary stream failed to start")
        return started

    def stop(self, reason: str = "") -> None:
        """Stop both streams; the peer stays armed for a later restart."""
        secondary = self._secondary
        if secondary is not None:
            try:
                secondary.stop(reason or "primary-stopped")
            except Exception:
                logger.debug("secondary stop failed", exc_info=True)
        if self.is_paused:
            self._paused = False
            self._pause_reason = ""
        super().stop(reason)

    # -- audio ingress ----------------------------------------------------- #

    def _on_audio(self, raw: Any, channels: int, sample_rate: int) -> None:
        """
        Backend callback: down-mix, reject spikes, meter, fan out, then store.

        Runs on the backend reader thread - never touch widgets here; only Qt
        signals cross over to the UI.
        """
        try:
            self._check_backpressure()
            if self.is_paused:
                # Backpressure: the device keeps recording, but nothing new
                # enters the ring buffer or the VAD until the lane drains.
                with self._lock:
                    self._pipeline_stats["paused_blocks"] += 1
                return

            source_rate = int(sample_rate or self.settings.target_rate)
            mono = Resampler.to_mono(raw, channels)
            if source_rate != self.settings.target_rate:
                mono = Resampler.resample(mono, source_rate, self.settings.target_rate)

            if self.settings.noise_suppression and self.is_microphone:
                mono = self.noise_suppressor.process(mono)

            if self._secondary is not None:
                mono = self._mix_blocks(mono)

            mono, burst_muted = self.burst_filter.process(mono)
            filtered, muted = self.spike_filter.process(mono)
            muted = int(muted) + int(burst_muted)
            level_db = _dbfs(filtered)
            notice = self.silence_monitor.update(level_db)

            # Frames still reach the ring buffer and the VAD so speech onset is
            # never clipped; the VAD decides there is nothing to transcribe,
            # which is what keeps empty ASR jobs from being created.
            super()._on_audio(filtered, 1, self.settings.target_rate)

            with self._lock:
                self._pipeline_stats["blocks"] += 1
                self._pipeline_stats["muted_samples"] += muted

            if muted:
                self.spike_dropped.emit(int(muted))

            if notice is not None:
                with self._lock:
                    self._pipeline_stats["silence_notices"] += 1
                logger.info("系统声音已静音 %.0f 秒（未生成 ASR 任务）", notice)
                self.silence_notice.emit(float(notice))
                self._emit_status(STATUS_OK, "系统声音静音中", force=True)

            if self.settings.feed_when_silent or not self.silence_monitor.is_silent:
                self._publish_frame(filtered, level_db)

            self._emit_status(STATUS_BUSY if level_db > SILENCE_THRESHOLD_DBFS else STATUS_OK,
                              "音频采集中", force=False)
        except Exception:
            # A bad frame is dropped; the stream and the app keep running.
            logger.exception("capture pipeline block failed (frame dropped)")

    def _publish_frame(self, samples: Any, level_db: float) -> None:
        """Hand one post-filter frame to the downstream sink exactly once."""
        sink = self._sink
        if sink is None:
            return
        try:
            frame = AudioFrame(samples=samples, sample_rate=self.settings.target_rate,
                               rms=0.0)
            frame.rms = 10.0 ** (max(MIN_DBFS, level_db) / 20.0)
            sink(frame)
            with self._lock:
                self._pipeline_stats["frames_out"] += 1
        except Exception:
            logger.exception("downstream sink rejected a frame")

    # -- status ------------------------------------------------------------ #

    def _emit_status(self, state: str, text: str, force: bool = False) -> None:
        """Throttled three-state feed for the overlay indicator dot."""
        now = time.monotonic()
        with self._lock:
            if not force and state == self._last_status[0] and now - self._last_status_at < STATUS_THROTTLE_S:
                return
            self._last_status = (state, text)
            self._last_status_at = now
        try:
            self.status_changed.emit(state, text)
        except Exception:
            logger.debug("status emit failed", exc_info=True)

    def _on_backend_started(self, *_args) -> None:
        self._emit_status(STATUS_OK, "音频已启动", force=True)

    def _on_backend_stopped(self, *_args) -> None:
        self._emit_status(STATUS_OK, "音频已停止", force=True)

    def _on_backend_error(self, message: Any = "") -> None:
        text = str(message or "音频采集异常")
        self._emit_status(STATUS_ERROR, text, force=True)
        logger.warning("audio backend error: %s", text)

    def _on_device_changed(self, *_args) -> None:
        self.silence_monitor.reset()
        self._emit_status(STATUS_BUSY, "音频设备切换中", force=True)

    def _on_reconnected(self, *args) -> None:
        with self._lock:
            self._pipeline_stats["reconnects"] += 1
        detail = args[0] if args else ""
        self._emit_status(STATUS_OK, "音频已重连 %s" % detail, force=True)

    # -- introspection ----------------------------------------------------- #

    def pipeline_stats(self) -> Dict[str, Any]:
        """Capture-pipeline counters for the diagnostics page."""
        with self._lock:
            data = dict(self._pipeline_stats)
            paused = self._paused
            reason = self._pause_reason
            secondary = self._secondary
        data.update(self.spike_filter.stats())
        data.update(self.burst_filter.stats())
        data.update(self.noise_suppressor.stats())
        data["silent_seconds"] = round(self.silence_monitor.silent_seconds, 1)
        data["paused"] = bool(paused)
        data["pause_reason"] = reason
        data["queue_limit"] = int(self._queue_limit or 0)
        data["source"] = self.settings.source
        data["primary_kind"] = self.kind
        data["secondary_kind"] = secondary.kind if secondary is not None else ""
        data["secondary_running"] = bool(secondary is not None and secondary.is_running)
        data["mix_primary_gain"] = self._primary_gain
        data["mix_secondary_gain"] = self._secondary_gain
        data["settings"] = self.settings.as_dict()
        return data

    def close(self) -> None:
        """Stop the stream and drop the sink (called on application exit)."""
        self.clear_sink()
        secondary, self._secondary = self._secondary, None
        if secondary is not None:
            try:
                secondary.set_block_sink(None)
                secondary.stop("shutdown")
            except Exception:
                logger.debug("secondary shutdown failed", exc_info=True)
        try:
            self.stop("shutdown")
        except Exception:
            logger.exception("audio stop failed during shutdown")


#: Backwards-friendly alias - the official module name is capture_win.
WasapiLoopbackCapture = AudioCaptureWorker
