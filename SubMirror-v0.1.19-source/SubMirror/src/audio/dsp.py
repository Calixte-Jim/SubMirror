# -*- coding: utf-8 -*-
"""
SubMirror v0.1.19 - capture-domain conditioning used by the work modes.

Two small, dependency-light stages sit between the raw device stream and the
VAD:

``BurstFilter``
    Gunshots, claps and keyboard bumps are *short* and *loud*: they stand tens
    of dB above the running baseline for no more than a few tens of
    milliseconds. The filter scans each block for runs that clear the baseline
    by ``margin_db`` and are no longer than ``max_ms``, and mutes exactly those
    samples. Sustained loudness (music, an explosion tail) raises the baseline
    and is therefore never treated as a burst, which is what keeps speech
    intact.

``NoiseSuppressor``
    A time-domain noise gate with a one-pole high-pass stage. The gate tracks
    the running noise floor and attenuates blocks that sit ``reduction_db``
    below it, so room tone, fan rumble and microphone self-noise stop holding
    the VAD open. It is deliberately *not* a spectral subtractor: no FFT, no
    latency, and no speech-shaped artefacts; the trade-off (steady broadband
    noise only) is documented in the changelog.

Both stages are pure numpy/list math, never raise, and are safe to call from
the capture thread. numpy is a normal runtime dependency, but the fallback
paths keep the module importable (and the suites runnable) without it.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "BurstFilter",
    "NoiseSuppressor",
    "BURST_MAX_MS",
    "BURST_MARGIN_DB",
    "NOISE_REDUCTION_DB",
    "NOISE_HIGHPASS_HZ",
]

logger = logging.getLogger("submirror.audio.dsp")

BURST_MAX_MS = 40.0            # a gunshot is far shorter than a syllable
BURST_MARGIN_DB = 12.0         # how far above the baseline a burst must sit
BURST_MIN_HISTORY = 4          # blocks of context before anything is muted
NOISE_REDUCTION_DB = 15.0      # gate depth below the tracked noise floor
NOISE_HIGHPASS_HZ = 120.0      # kills desk rumble and DC offset
BASELINE_WINDOW = 24           # blocks feeding the burst baseline median
MIN_DBFS = -90.0

try:  # pragma: no cover - exercised implicitly
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _as_list(samples: Any) -> List[float]:
    """Cheap float view without forcing numpy."""
    if samples is None:
        return []
    if _np is not None and isinstance(samples, _np.ndarray):
        return [float(v) for v in samples.reshape(-1).tolist()]
    if hasattr(samples, "tolist"):
        return [float(v) for v in samples.tolist()]
    return [float(v) for v in samples]


def _rebuild(values: Sequence[float], like: Any) -> Any:
    """Return *values* shaped like the input block."""
    if _np is not None and isinstance(like, _np.ndarray):
        dtype = like.dtype if like.dtype.kind == "f" else _np.float32
        return _np.asarray(values, dtype=dtype).reshape(like.shape)
    if isinstance(like, (bytes, bytearray)):
        return like
    return list(values)


def _dbfs(values: Sequence[float]) -> float:
    """Level of a block on the project's log-amplitude scale."""
    if not values:
        return MIN_DBFS
    total = 0.0
    for value in values:
        total += float(value) * float(value)
    rms = math.sqrt(total / len(values))
    if rms <= 1e-7:
        return MIN_DBFS
    return max(MIN_DBFS, 20.0 * math.log10(rms))


def _runs_above(values: Sequence[float], gate: float) -> List[Tuple[int, int]]:
    """Half-open index ranges whose ``|value|`` stays above *gate*."""
    runs: List[Tuple[int, int]] = []
    start = -1
    for index, value in enumerate(values):
        loud = abs(float(value)) > gate
        if loud and start < 0:
            start = index
        elif not loud and start >= 0:
            runs.append((start, index))
            start = -1
    if start >= 0:
        runs.append((start, len(values)))
    return runs


# --------------------------------------------------------------------------- #
# Burst (gunshot) filter
# --------------------------------------------------------------------------- #

class BurstFilter:
    """
    Mute short, loud runs that sit far above the running noise baseline.

    The running baseline is the median level of the last ``BASELINE_WINDOW``
    blocks, so continuous loud material lifts it and is preserved: only
    transients shorter than ``max_ms`` are silenced. Muted samples are zeroed
    in place, keeping the timeline aligned for the VAD and Whisper.
    """

    def __init__(self, sample_rate: int = 16_000, max_ms: float = BURST_MAX_MS,
                 margin_db: float = BURST_MARGIN_DB, enabled: bool = False,
                 min_history: int = BURST_MIN_HISTORY) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.max_ms = max(0.5, float(max_ms))
        self.margin_db = max(0.0, float(margin_db))
        self.enabled = bool(enabled)
        self.min_history = max(1, int(min_history))
        self._lock = threading.RLock()
        self._history: deque = deque(maxlen=BASELINE_WINDOW)
        self._baseline_db = MIN_DBFS
        self._bursts = 0
        self._bursts_unbounded = 0
        self._muted_samples = 0

    # -- state -------------------------------------------------------------- #

    @property
    def baseline_db(self) -> float:
        with self._lock:
            return self._baseline_db

    def configure(self, sample_rate: Optional[int] = None, max_ms: Optional[float] = None,
                  margin_db: Optional[float] = None, enabled: Optional[bool] = None) -> None:
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
            return {"bursts": self._bursts, "burst_muted_samples": self._muted_samples,
                    "burst_unbounded_runs": self._bursts_unbounded,
                    "burst_baseline_db": round(self._baseline_db, 2),
                    "burst_enabled": bool(self.enabled)}

    # -- processing --------------------------------------------------------- #

    def process(self, samples: Any) -> Tuple[Any, int]:
        """
        Return ``(maybe_filtered_block, muted_sample_count)``; never raises.

        A run is only treated as a burst when it is *bounded by quieter audio
        inside the block*: the first and last samples of a block are never
        candidates. A sustained tone, music or an explosion therefore fills the
        block edge to edge and is left alone, while a gunshot - a short island
        of energy inside a quieter block - is removed. The baseline also needs
        ``min_history`` blocks of context before anything is judged, so the
        first ~120 ms after a cold start are never muted.
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
                history_len = len(self._history)
                baseline = statistics.median(self._history) if self._history else level_db
                self._baseline_db = float(baseline)
                margin = self.margin_db
                max_span = int(self.sample_rate * self.max_ms / 1000.0)

            if not enabled or max_span < 1 or history_len < self.min_history:
                return samples, 0

            gate_linear = 10.0 ** ((baseline + margin) / 20.0)
            if max(abs(v) for v in values) <= gate_linear:
                return samples, 0

            muted = 0
            unbounded = 0
            last = len(values)
            for start, end in _runs_above(values, gate_linear):
                if (end - start) <= max_span:
                    if start == 0 or end >= last:
                        unbounded += 1
                        continue
                    for index in range(start, end):
                        values[index] = 0.0
                    muted += end - start

            if unbounded:
                with self._lock:
                    self._bursts_unbounded += unbounded
            if not muted:
                return samples, 0
            with self._lock:
                self._bursts += 1
                self._muted_samples += muted
            return _rebuild(values, samples), muted
        except Exception:
            logger.exception("burst filter failed; passing the block through")
            return samples, 0


# --------------------------------------------------------------------------- #
# Noise suppressor
# --------------------------------------------------------------------------- #

class NoiseSuppressor:
    """
    High-pass plus a soft noise gate driven by the tracked noise floor.

    ``process`` is stateful across blocks (the floor and the high-pass memory
    carry over), which is what makes it usable on 30 ms frames: a per-block
    estimate would be far too noisy to gate on.
    """

    def __init__(self, sample_rate: int = 16_000, reduction_db: float = NOISE_REDUCTION_DB,
                 enabled: bool = False, highpass_hz: float = NOISE_HIGHPASS_HZ,
                 floor_rise: float = 0.05) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.reduction_db = max(3.0, min(40.0, float(reduction_db)))
        self.enabled = bool(enabled)
        self.highpass_hz = max(20.0, float(highpass_hz))
        self.floor_rise = max(0.001, min(0.5, float(floor_rise)))
        self._lock = threading.RLock()
        self._floor_db = MIN_DBFS
        self._previous_in = 0.0
        self._previous_out = 0.0
        self._gated_blocks = 0
        self._min_gain = 0.05

    # -- state -------------------------------------------------------------- #

    @property
    def noise_floor_db(self) -> float:
        with self._lock:
            return self._floor_db

    def configure(self, sample_rate: Optional[int] = None,
                  reduction_db: Optional[float] = None, enabled: Optional[bool] = None,
                  highpass_hz: Optional[float] = None) -> None:
        with self._lock:
            if sample_rate:
                self.sample_rate = max(1, int(sample_rate))
            if reduction_db is not None:
                self.reduction_db = max(3.0, min(40.0, float(reduction_db)))
            if enabled is not None:
                self.enabled = bool(enabled)
            if highpass_hz is not None:
                self.highpass_hz = max(20.0, float(highpass_hz))

    def reset(self) -> None:
        with self._lock:
            self._floor_db = MIN_DBFS
            self._previous_in = 0.0
            self._previous_out = 0.0

    def stats(self) -> Dict[str, float]:
        with self._lock:
            return {"noise_floor_db": round(self._floor_db, 2),
                    "noise_gated_blocks": self._gated_blocks,
                    "noise_suppression": bool(self.enabled)}

    # -- processing --------------------------------------------------------- #

    def _highpass(self, values: List[float]) -> List[float]:
        """One-pole high-pass; removes DC offset and desk rumble."""
        with self._lock:
            cutoff = self.highpass_hz
            alpha = 1.0 / (1.0 + 2.0 * math.pi * cutoff / float(self.sample_rate))
            previous_in = self._previous_in
            previous_out = self._previous_out
        out: List[float] = []
        for value in values:
            current = alpha * (previous_out + value - previous_in)
            previous_in = value
            previous_out = current
            out.append(current)
        with self._lock:
            self._previous_in = previous_in
            self._previous_out = previous_out
        return out

    def process(self, samples: Any) -> Any:
        """Return the conditioned block (same shape/type as the input)."""
        try:
            values = _as_list(samples)
            if not values:
                return samples

            filtered = self._highpass(values) if self.enabled else values
            level_db = _dbfs(filtered)

            with self._lock:
                enabled = self.enabled
                floor = self._floor_db
                reduction = self.reduction_db
                # The floor drops instantly to a quieter block and creeps up
                # slowly, so a loud passage never drags the gate with it.
                if floor <= MIN_DBFS:
                    floor = level_db
                elif level_db < floor:
                    floor = level_db
                else:
                    floor = floor + (level_db - floor) * self.floor_rise
                self._floor_db = floor

            if not enabled:
                return _rebuild(filtered, samples)

            threshold = floor + reduction
            if level_db >= threshold:
                return _rebuild(filtered, samples)

            # Soft attenuation instead of a hard cut: the deeper below the
            # threshold, the quieter the block, floored at -40 dB.
            gain = max(self._min_gain, 10.0 ** ((level_db - threshold) / 20.0))
            with self._lock:
                self._gated_blocks += 1
            return _rebuild([value * gain for value in filtered], samples)
        except Exception:
            logger.exception("noise suppressor failed; passing the block through")
            return samples
