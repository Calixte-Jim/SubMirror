# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Energy based voice activity detection and segmentation.

Purpose
    Recognition is the expensive step, so silent audio must never reach it.
    This module watches the capture stream, tracks an adaptive noise floor and
    emits only the spans that actually contain speech.

Why not webrtcvad
    It adds a native dependency for a job the energy gate already does on game
    audio.  Keeping the gate local also means the app stays installable offline
    and starts instantly.

Optional Silero backend
    Silero VAD (ONNX, ~2 MB, no torch) is used when it is installed, because it
    separates voices from music and gunfire far better than a level gate.  The
    energy detector below stays the default fallback: without onnxruntime or
    the model file nothing changes.  See ``src/audio/silero_vad.py``.

State machine
    SILENCE --(loud for onset_ms)--> SPEECH --(quiet for hangover_ms)--> SILENCE

    * onset confirmation rejects clicks, key taps and single-frame spikes.
    * hangover keeps short pauses inside one sentence from splitting it.
    * pre-roll keeps the samples just before onset so the first phoneme is not
      clipped off the segment.
    * max_segment_seconds force-flushes long speech so a monologue is not held
      hostage waiting for silence.

All processing is in-memory; user-mode only, no hooks, no injection.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:  # pragma: no cover - optional Qt
    from PySide6.QtCore import QObject, Signal
    _HAS_QT = True
except Exception:  # pragma: no cover
    _HAS_QT = False

    class _FallbackSignal:  # mirrors src/core/subtitle_queue.py
        def __init__(self, *_a, **_k) -> None:
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

    def Signal(*_a, **_k):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        pass

from src.audio.ring_buffer import LevelMeter, Resampler

__all__ = [
    "VadConfig",
    "SpeechSegment",
    "VoiceActivityDetector",
    "SILENCE",
    "SPEECH",
]

logger = logging.getLogger("submirror.audio")

SILENCE = "silence"
SPEECH = "speech"

_EPSILON = 1e-9
_SILENCE_DBFS = -120.0


# --------------------------------------------------------------------------- #
# Configuration and results
# --------------------------------------------------------------------------- #

# BL-1: floors applied in FPS mode. Gunfire bursts are far shorter than the
# minimum speech duration, so raising the floor is what actually filters them.
FPS_ONSET_MS_FLOOR = 150
FPS_MIN_SPEECH_MS_FLOOR = 300

# The Silero ONNX model is fixed to 512-sample frames at 16 kHz.
SILERO_WINDOW_SAMPLES = 512
SILERO_SAMPLE_RATE = 16_000


@dataclass
class VadConfig:
    """Tunables for the energy gate (all times in milliseconds)."""

    threshold_db: float = -45.0     # absolute dBFS gate
    margin_db: float = 9.0          # required rise above the noise floor
    onset_ms: int = 120             # speech must persist this long to start
    hangover_ms: int = 320          # silence needed before the segment ends
    min_speech_ms: int = 300        # shorter bursts are discarded
    max_segment_seconds: float = 6.0
    # The energy gate has no model behind it, so it only slices on a timer: a
    # longer slice produces fewer recognition tasks per minute, which is what
    # keeps the ASR lane from backing up while Silero is unavailable.
    energy_max_segment_seconds: float = 9.0
    preroll_ms: int = 200
    adaptive: bool = True
    noise_adapt_up: float = 0.020   # slow: noise must not chase speech
    noise_adapt_down: float = 0.150 # fast: a quieter room is recognised at once
    noise_floor_init_db: float = -70.0
    overlap_ms: int = 400           # tail replayed into the next forced slice
    # -- short-chunk slicing (v0.1.18) --
    # A monologue is cut every chunk_seconds instead of being held for one long
    # call, which is what keeps the first line quick. When the queue is
    # overloaded the target is stretched towards chunk_max_seconds, so fewer
    # tasks are produced rather than letting the backlog grow.
    chunk_seconds: float = 2.0
    chunk_max_seconds: float = 4.0
    # -- optional Silero backend --
    engine: str = "auto"            # auto | silero | energy
    silero_path: str = ""
    silero_threshold: float = 0.5
    silero_negative_margin: float = 0.15
    silero_min_speech_ms: int = 250

    @classmethod
    def from_config(cls, config: Any) -> "VadConfig":
        """Build from the settings store, ignoring anything missing or invalid."""
        if config is None:
            return cls()

        def _get(keys: Sequence[str], default: Any) -> Any:
            for key in keys:
                try:
                    value = config.get(key, None)
                except Exception:
                    value = None
                if value is not None:
                    return value
            return default

        def _float(keys: Sequence[str], default: float) -> float:
            try:
                return float(_get(keys, default))
            except (TypeError, ValueError):
                return default

        def _int(keys: Sequence[str], default: int) -> int:
            try:
                return int(_get(keys, default))
            except (TypeError, ValueError):
                return default

        # BL-1: the settings panel writes the public asr.* names; the older
        # audio.* names stay readable so an existing config.json keeps working.
        mode = str(_get(("asr.vad_mode",), "single")).strip().lower()
        onset_ms = _int(("audio.vad_onset_ms",), 120)
        min_speech_ms = _int(
            ("asr.vad_min_speech_ms", "audio.vad_min_speech_ms"), 300
        )
        if mode == "fps":
            onset_ms = max(onset_ms, FPS_ONSET_MS_FLOOR)
            min_speech_ms = max(min_speech_ms, FPS_MIN_SPEECH_MS_FLOOR)

        return cls(
            threshold_db=_float(
                ("asr.vad_threshold_db", "audio.vad_threshold_db"), -45.0
            ),
            margin_db=_float(("audio.vad_margin_db",), 9.0),
            onset_ms=onset_ms,
            hangover_ms=_int(("audio.vad_hangover_ms",), 320),
            min_speech_ms=min_speech_ms,
            max_segment_seconds=_float(("audio.vad_max_segment_seconds",), 6.0),
            energy_max_segment_seconds=_float(
                ("audio.vad_energy_max_segment_seconds",), 9.0
            ),
            preroll_ms=_int(("audio.vad_preroll_ms",), 200),
            adaptive=bool(_get(("audio.vad_adaptive",), True)),
            overlap_ms=_int(("audio.vad_overlap_ms",), 400),
            chunk_seconds=_float(("audio.vad_chunk_seconds",), 2.0),
            chunk_max_seconds=_float(("audio.vad_chunk_max_seconds",), 4.0),
            engine=str(_get(("audio.vad_engine",), "auto") or "auto").strip().lower(),
            silero_path=str(_get(("audio.vad_silero_path",), "") or ""),
            silero_threshold=_float(("audio.vad_silero_threshold",), 0.5),
            silero_negative_margin=_float(("audio.vad_silero_negative_margin",), 0.15),
            silero_min_speech_ms=_int(("audio.vad_silero_min_speech_ms",), 250),
        )


@dataclass
class SpeechSegment:
    """One detected utterance ready for the recogniser."""

    samples: Any
    sample_rate: int
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float = field(default_factory=time.monotonic)
    dbfs: float = _SILENCE_DBFS
    zcr: float = 0.0
    is_forced: bool = False
    # True when the forced slice carries no speech at all (level below the gate)
    # - the pipeline drops it instead of spending a recognition pass on it.
    silent: bool = False
    # Monotonic per-utterance counter. The streaming pipeline uses it to match
    # a preview (or a late translation) to the line it belongs to.
    sequence: int = 0

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def duration(self) -> float:
        return self.count / float(self.sample_rate or 1)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "duration": round(self.duration, 3),
            "dbfs": round(self.dbfs, 1),
            "zcr": round(self.zcr, 4),
            "forced": self.is_forced,
            "sequence": int(self.sequence),
        }


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #

class VoiceActivityDetector(QObject):
    """
    Streaming energy gate with hysteresis, pre-roll and forced segmentation.

    Signals
        speech_started()
        speech_ended()
        segment_ready(object)   a finished ``SpeechSegment`` (async consumers)
    """

    speech_started = Signal()
    speech_ended = Signal()
    segment_ready = Signal(object)

    def __init__(self, config: Any = None, sample_rate: int = 16_000,
                 vad_config: Optional[VadConfig] = None, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self.sample_rate = max(1, int(sample_rate))
        self.config = vad_config or VadConfig.from_config(config)

        self._state = SILENCE
        self._noise_floor_db = float(self.config.noise_floor_init_db)

        # Segment assembly
        self._preroll: List[Any] = []
        self._preroll_samples = 0
        self._speech: List[Any] = []
        self._speech_samples = 0
        self._segment_started_at = 0.0
        self._loud_ms = 0.0             # consecutive loud time while SILENCE
        self._quiet_ms = 0.0            # consecutive quiet time while SPEECH
        self._last_dbfs = _SILENCE_DBFS
        self._last_zcr = 0.0

        self._stats: Dict[str, float] = {
            "blocks": 0, "speech_blocks": 0, "segments": 0, "discarded": 0,
        }

        # -- optional Silero backend --
        self._silero: Any = None            # silero_vad.SileroVad, when loaded
        self._silero_error = ""
        self._silero_open = False           # hysteresis state of the model gate
        self._silero_pending: List[Any] = []
        self._silero_leftover = 0
        self._sequence = 0                  # last handed-out utterance number
        # Adaptive short-chunk target (v0.1.18): starts at the configured
        # value and is stretched while the recognition queue is overloaded.
        self._chunk_seconds = max(1.0, float(self.config.chunk_seconds or 2.0))
        self._chunk_stretch_count = 0
        self._chunk_relax_count = 0
        self._load_silero()

    # -- backend selection -------------------------------------------------- #

    def _load_silero(self) -> None:
        """
        Try to attach the Silero model.

        "energy" never tries, "silero" tries and logs loudly, "auto" tries and
        silently keeps the energy gate when the model is not installed.
        """
        mode = str(getattr(self.config, "engine", "auto") or "auto").lower()
        if mode == "energy":
            self._silero_error = "disabled"
            return
        try:
            from src.audio.silero_vad import SileroVad, find_model_path
        except Exception:
            self._silero_error = "module-missing"
            if mode == "silero":
                logger.warning("Silero VAD module is unavailable")
            return
        path = find_model_path(getattr(self.config, "silero_path", "") or "")
        if not path:
            self._silero_error = "model-missing"
            if mode == "silero":
                logger.warning("Silero VAD model not found; using the energy gate")
            return
        detector = SileroVad(path)
        if not detector.is_loaded:
            self._silero_error = detector.error or "load-failed"
            return
        self._silero = detector
        self._silero_error = ""
        logger.info("voice activity: Silero VAD (%s)", os.path.basename(path))

    @property
    def engine(self) -> str:
        """``"silero"`` when the model is active, otherwise ``"energy"``."""
        return "silero" if self._silero is not None else "energy"

    @property
    def segment_limit_seconds(self) -> float:
        """
        Longest slice held before a forced flush, honouring the active engine.

        The energy gate carries no speech model, so slicing it on a longer
        timer trades a little latency for noticeably fewer ASR tasks.
        """
        limit = float(self.config.max_segment_seconds)
        if self.engine != "silero":
            limit = max(limit, float(self.config.energy_max_segment_seconds))
        return max(1.0, limit)

    @property
    def current_chunk_seconds(self) -> float:
        """Adaptive slicing target for the next forced flush."""
        with self._lock:
            return float(self._chunk_seconds)

    @property
    def chunk_limit_seconds(self) -> float:
        """
        Slice length really used by :meth:`push`.

        Short chunks make the first line quick; the engine ceiling above still
        wins when it is the smaller of the two, so a caller can never hold more
        audio than the configured hard cap.
        """
        return max(1.0, min(self.segment_limit_seconds,
                            self.current_chunk_seconds))

    def stretch_chunk(self, step: float = 0.5) -> float:
        """
        Lengthen the slice target after an overload (v0.1.18 item 6).

        Fewer, longer chunks produce fewer recognition tasks per minute, which
        is the honest way to shed load without dropping audio on purpose.
        """
        ceiling = max(float(self.config.chunk_seconds or 2.0),
                      float(self.config.chunk_max_seconds or 4.0))
        with self._lock:
            before = self._chunk_seconds
            self._chunk_seconds = min(ceiling, before + max(0.1, float(step)))
            if self._chunk_seconds > before:
                self._chunk_stretch_count += 1
            return self._chunk_seconds

    def relax_chunk(self, step: float = 0.25) -> float:
        """Shrink the slice target back towards the configured value."""
        floor = max(1.0, float(self.config.chunk_seconds or 2.0))
        with self._lock:
            before = self._chunk_seconds
            self._chunk_seconds = max(floor, before - max(0.05, float(step)))
            if self._chunk_seconds < before:
                self._chunk_relax_count += 1
            return self._chunk_seconds

    def note_load(self, overloaded: bool) -> float:
        """
        Feed the current queue pressure back into the slice length.

        Called by the pipeline after every recognised chunk; overloaded stretches
        the target, a healthy queue relaxes it again.
        """
        return self.stretch_chunk() if overloaded else self.relax_chunk()

    @property
    def engine_error(self) -> str:
        """Why Silero is not active - empty while it is."""
        return "" if self._silero is not None else (self._silero_error or "unavailable")

    @property
    def current_sequence(self) -> int:
        """Number of the utterance currently being spoken (may be unfinished)."""
        with self._lock:
            return self._sequence + 1

    def active_audio(self) -> Optional[Any]:
        """
        Copy of the audio buffered for the utterance in progress.

        The streaming pipeline uses this to recognise a preview while the
        speaker is still talking.  Returns None when nothing is being spoken;
        the buffer itself is never cleared here.
        """
        with self._lock:
            if self._state != SPEECH or not self._speech:
                return None
            return self._concat_locked(list(self._speech))

    def _silero_speech_locked(self, data: Any, rate: int) -> Optional[bool]:
        """
        Speech decision for one block, or None when the model cannot judge it.

        The model only accepts 512-sample frames at 16 kHz, so the incoming
        blocks are resampled when needed and accumulated; every complete frame
        is evaluated.  Opening needs ``silero_threshold``; staying open only
        needs that value minus the hysteresis margin, which is what stops the
        gate from chattering on a single quiet frame.
        """
        detector = self._silero
        if detector is None:
            return None
        try:
            import numpy as np

            samples = np.asarray(data, dtype="float32").reshape(-1)
            if int(rate) != SILERO_SAMPLE_RATE:
                samples = np.asarray(
                    Resampler.resample(samples, int(rate), SILERO_SAMPLE_RATE),
                    dtype="float32").reshape(-1)
            if samples.size:
                self._silero_pending.append(samples)
        except Exception:
            logger.debug("Silero block resampling failed", exc_info=True)
            return None
        window = SILERO_WINDOW_SAMPLES
        decision: Optional[bool] = None
        while self._silero_pending:
            available = sum(len(part) for part in self._silero_pending)
            if available < window:
                break
            frame = np.concatenate(self._silero_pending) if len(self._silero_pending) > 1 \
                else self._silero_pending[0]
            chunk = frame[:window]
            rest = frame[window:]
            self._silero_pending = [rest] if rest.size else []
            probability = detector.probability(chunk)
            if probability is None:
                self._silero = None       # a broken runtime must not keep failing
                self._silero_error = "inference-failed"
                return None
            threshold = float(getattr(self.config, "silero_threshold", 0.5) or 0.5)
            margin = float(getattr(self.config, "silero_negative_margin", 0.15) or 0.15)
            if self._silero_open:
                opened = probability >= max(0.0, threshold - margin)
            else:
                opened = probability >= threshold
            self._silero_open = bool(opened)
            decision = bool(decision or opened)
        return decision

    # -- introspection ----------------------------------------------------- #

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def noise_floor_db(self) -> float:
        with self._lock:
            return self._noise_floor_db

    @property
    def current_threshold_db(self) -> float:
        with self._lock:
            return self._threshold_locked()

    @property
    def is_speaking(self) -> bool:
        return self.state == SPEECH

    @property
    def stats(self) -> Dict[str, float]:
        with self._lock:
            data = dict(self._stats)
            engine = self.engine
            error = self.engine_error
            silero = self._silero.stats() if self._silero is not None else {}
        data["engine"] = engine
        data["engine_error"] = error
        data["silero"] = silero
        data["segment_limit_seconds"] = self.segment_limit_seconds
        data["chunk_seconds"] = self._chunk_seconds
        data["chunk_limit_seconds"] = self.chunk_limit_seconds
        data["chunk_stretched"] = self._chunk_stretch_count
        data["chunk_relaxed"] = self._chunk_relax_count
        return data

    # -- metrics ----------------------------------------------------------- #

    @staticmethod
    def dbfs(samples: Any) -> float:
        """RMS level in dBFS; -120 for silence."""
        rms = LevelMeter.rms(samples)
        if rms <= _EPSILON:
            return _SILENCE_DBFS
        return max(_SILENCE_DBFS, 20.0 * math.log10(rms))

    @staticmethod
    def zero_crossing_rate(samples: Any) -> float:
        """
        Fraction of adjacent sample pairs that change sign (0.0 - 1.0).

        Reported for tuning only; not used as a hard gate, because game audio
        mixes voices over music where a ZCR rule would misfire.
        """
        data = Resampler.to_mono(samples, 1)
        count = len(data)
        if count < 2:
            return 0.0
        crossings = 0
        previous = float(data[0])
        for index in range(1, count):
            current = float(data[index])
            if (previous < 0.0 <= current) or (previous >= 0.0 > current):
                crossings += 1
            previous = current
        return crossings / float(count - 1)

    # -- gate -------------------------------------------------------------- #

    def _threshold_locked(self) -> float:
        """Effective gate = the louder of the absolute floor and noise + margin."""
        return max(self.config.threshold_db,
                   self._noise_floor_db + self.config.margin_db)

    def _adapt_noise_locked(self, dbfs: float) -> None:
        """Track the noise floor while no speech is active."""
        if not self.config.adaptive:
            return
        if dbfs > self._noise_floor_db:
            rate = self.config.noise_adapt_up
        else:
            rate = self.config.noise_adapt_down
        self._noise_floor_db += (dbfs - self._noise_floor_db) * rate
        # Never let the floor drift above a sane ceiling, or the gate jams shut.
        self._noise_floor_db = min(self._noise_floor_db, self.config.threshold_db - 1.0)

    def is_speech(self, samples: Any) -> bool:
        """One-shot check without touching the streaming state machine."""
        return self.dbfs(samples) >= self._threshold_locked()

    # -- streaming --------------------------------------------------------- #

    def push(self, samples: Any, sample_rate: Optional[int] = None) -> List[SpeechSegment]:
        """
        Feed one capture block; return any segments completed by this block.

        Normally returns an empty list. A non-empty result means one utterance
        (or a forced slice of a long one) is ready for recognition.
        """
        rate = max(1, int(sample_rate or self.sample_rate))
        data = Resampler.to_mono(samples, 1)
        count = len(data)
        if count == 0:
            return []

        block_ms = 1000.0 * count / float(rate)
        dbfs = self.dbfs(data)
        zcr = self.zero_crossing_rate(data)
        completed: List[SpeechSegment] = []
        started = ended = False

        with self._lock:
            self._stats["blocks"] += 1
            self._last_dbfs = dbfs
            self._last_zcr = zcr
            threshold = self._threshold_locked()
            # Silero answers "is this speech?" when it is installed; the energy
            # comparison stays the fallback so the gate never goes blind.
            decision = self._silero_speech_locked(data, rate)
            loud = bool(decision) if decision is not None else (dbfs >= threshold)

            if self._state == SILENCE:
                if loud:
                    self._stats["speech_blocks"] += 1
                    self._loud_ms += block_ms
                    self._append_preroll_locked(data, rate)
                    if self._loud_ms >= self.config.onset_ms:
                        self._state = SPEECH
                        self._segment_started_at = time.monotonic()
                        self._speech = self._preroll
                        self._speech_samples = self._preroll_samples
                        self._preroll = []
                        self._preroll_samples = 0
                        self._quiet_ms = 0.0
                        self._speech.append(data)
                        self._speech_samples += count
                        started = True
                else:
                    self._loud_ms = 0.0
                    self._adapt_noise_locked(dbfs)
                    self._append_preroll_locked(data, rate)

            else:  # SPEECH
                self._speech.append(data)
                self._speech_samples += count
                if loud:
                    self._stats["speech_blocks"] += 1
                    self._quiet_ms = 0.0
                else:
                    self._quiet_ms += block_ms
                    if self._quiet_ms >= self.config.hangover_ms:
                        ended = True
                        completed.append(self._finish_locked(rate, forced=False))

                if not ended:
                    held_seconds = self._speech_samples / float(rate)
                    # Short-chunk slicing (v0.1.18): the adaptive chunk target
                    # is the cut point, and the engine ceiling only caps it.
                    if held_seconds >= self.chunk_limit_seconds:
                        completed.append(self._finish_locked(rate, forced=True))
                        # Keep going: a long monologue continues as a new segment.
                        self._state = SPEECH if loud else SILENCE
                        if self._state == SILENCE:
                            ended = True

        if started:
            self.speech_started.emit()
        if ended:
            self.speech_ended.emit()
        for segment in completed:
            self._stats["segments"] += 1
            self.segment_ready.emit(segment)
        return completed

    def flush(self, sample_rate: Optional[int] = None) -> Optional[SpeechSegment]:
        """
        Force out whatever speech is buffered.

        Called when capture stops or the audio device is lost, so the final
        sentence of a session still reaches the recogniser.
        """
        rate = max(1, int(sample_rate or self.sample_rate))
        with self._lock:
            if self._state != SPEECH or self._speech_samples <= 0:
                self._reset_buffers_locked()
                return None
            segment = self._finish_locked(rate, forced=True)
            self._stats["segments"] += 1
        self.speech_ended.emit()
        self.segment_ready.emit(segment)
        return segment

    def reset(self) -> None:
        """Drop all state, e.g. after switching the capture device."""
        with self._lock:
            self._state = SILENCE
            self._loud_ms = 0.0
            self._quiet_ms = 0.0
            self._noise_floor_db = float(self.config.noise_floor_init_db)
            self._sequence = 0
            self._silero_open = False
            self._silero_pending = []
            if self._silero is not None:
                try:
                    self._silero.reset()
                except Exception:
                    logger.debug("Silero state reset failed", exc_info=True)
            self._reset_buffers_locked()

    # -- internals --------------------------------------------------------- #

    def _preroll_capacity(self, rate: int) -> int:
        return max(1, int(rate * self.config.preroll_ms / 1000.0))

    def _append_preroll_locked(self, data: Any, rate: int) -> None:
        """Keep only the newest ``preroll_ms`` of non-speech audio."""
        if self.config.preroll_ms <= 0:
            return
        capacity = self._preroll_capacity(rate)
        self._preroll.append(data)
        self._preroll_samples += len(data)
        while self._preroll_samples > capacity and self._preroll:
            dropped = self._preroll.pop(0)
            self._preroll_samples -= len(dropped)

    def _min_speech_ms_locked(self) -> int:
        """Effective minimum speech duration; Silero may make it a bit shorter."""
        if self._silero is not None:
            try:
                return max(0, int(getattr(self.config, "silero_min_speech_ms",
                                           self.config.min_speech_ms)))
            except (TypeError, ValueError):
                pass
        return int(self.config.min_speech_ms)

    def _finish_locked(self, rate: int, forced: bool) -> SpeechSegment:
        """Assemble the buffered blocks into a segment and clear the state."""
        samples = self._concat_locked(self._speech)
        duration_ms = 1000.0 * self._speech_samples / float(rate)
        too_short = duration_ms < self._min_speech_ms_locked() and not forced
        if too_short:
            self._stats["discarded"] += 1

        self._sequence += 1
        level = self.dbfs(samples) if not too_short else _SILENCE_DBFS
        # "Silent" means clearly below the speech floor - a full margin under the
        # trigger - so skipping the transcription cannot lose quiet speech; it
        # only skips background noise that the detector let through (item 1).
        quiet = bool(not too_short and
                     level < (self._threshold_locked() - float(self.config.margin_db)))
        segment = SpeechSegment(
            samples=samples,
            sample_rate=rate,
            started_at=self._segment_started_at or time.monotonic(),
            ended_at=time.monotonic(),
            dbfs=level,
            zcr=self.zero_crossing_rate(samples) if not too_short else 0.0,
            is_forced=forced,
            silent=quiet,
            sequence=self._sequence,
        )

        # A forced slice is cut while the speaker is still talking. Keeping its
        # tail as the head of the next slice stops the word on the boundary
        # from being lost to the recogniser.
        overlap: List[Any] = []
        overlap_samples = 0
        if forced and not too_short and self.config.overlap_ms > 0:
            overlap = self._tail_locked(samples, rate, int(self.config.overlap_ms))
            overlap_samples = sum(len(block) for block in overlap)

        self._state = SILENCE
        self._loud_ms = 0.0
        self._quiet_ms = 0.0
        self._reset_buffers_locked()
        if overlap:
            self._speech = overlap
            self._speech_samples = overlap_samples

        if too_short:
            # Return an empty placeholder so callers can treat it as "nothing".
            segment.samples = segment.samples[:0]
        return segment

    def _tail_locked(self, samples: Any, rate: int, milliseconds: int) -> List[Any]:
        """Newest ``milliseconds`` of audio, as a list of one or two blocks."""
        wanted = max(1, int(rate * milliseconds / 1000.0))
        try:
            import numpy as np

            data = np.asarray(samples, dtype="float32").reshape(-1)
            if data.size <= wanted:
                return [data] if data.size else []
            return [data[-wanted:]]
        except Exception:
            try:
                length = len(samples)
            except Exception:
                return []
            if length <= wanted:
                return [samples] if length else []
            return [samples[-wanted:]]

    def _reset_buffers_locked(self) -> None:
        self._speech = []
        self._speech_samples = 0
        self._preroll = []
        self._preroll_samples = 0

    @staticmethod
    def _concat_locked(blocks: List[Any]) -> Any:
        """Join buffered blocks into one contiguous sequence."""
        if not blocks:
            return Resampler.to_mono([], 1)
        if len(blocks) == 1:
            return blocks[0]
        try:
            import numpy as np
            return np.concatenate(blocks)
        except Exception:
            import array
            out = array.array("f")
            for block in blocks:
                out.extend(block)
            return out
