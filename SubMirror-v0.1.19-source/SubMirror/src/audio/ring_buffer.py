# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Lock-light ring buffer and audio format helpers.

Role in the pipeline
    WASAPI loopback capture thread  ->  AudioRingBuffer  ->  recognition thread

The capture thread must never block: when the buffer is full the oldest samples
are overwritten instead of raising or waiting. Readers always receive an
independent copy, so a slow recogniser cannot corrupt incoming audio.

Formats
    * Internal storage is mono ``float32`` in the range [-1.0, 1.0].
    * ``write()`` accepts raw ``bytes`` (16-bit PCM from WASAPI), numpy arrays,
      ``array.array`` or plain lists, and normalises them on the way in.
    * ``Resampler.convert()`` performs mono down-mix plus resampling to the
      16 kHz rate expected by faster-whisper.

numpy is optional: without it the same API is served by ``array("f")`` storage
at a lower throughput, so the module stays importable everywhere.

Safety: user-mode only. In-memory arithmetic, no hooks, no injection, no
reading of another process's memory.
"""

from __future__ import annotations

import array
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "AudioRingBuffer",
    "Resampler",
    "LevelMeter",
    "AudioFrame",
    "TARGET_SAMPLE_RATE",
    "DEFAULT_RING_SECONDS",
]

logger = logging.getLogger("submirror.audio")

try:  # pragma: no cover - optional dependency
    import numpy as _np
    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    _np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

TARGET_SAMPLE_RATE = 16_000          # faster-whisper input rate
DEFAULT_RING_SECONDS = 30.0
DEFAULT_CAPTURE_RATE = 48_000        # typical WASAPI shared-mode rate
INT16_SCALE = 32768.0

ArrayLike = Union[bytes, bytearray, memoryview, Sequence[float], Any]


# --------------------------------------------------------------------------- #
# Small format helpers (numpy path and array fallback)
# --------------------------------------------------------------------------- #

def _new_floats(count: int):
    """Allocate a zero-filled float32 container of *count* samples."""
    if _HAS_NUMPY:
        return _np.zeros(count, dtype=_np.float32)
    return array.array("f", bytes(4 * count))


def _to_float32(data: ArrayLike):
    """
    Normalise arbitrary audio input to a contiguous mono float32 sequence.

    ``bytes`` / ``bytearray`` / ``memoryview`` are interpreted as 16-bit signed
    little-endian PCM, which is what WASAPI hands us; plain numbers are treated
    as already-normalised floats.
    """
    if data is None:
        return _new_floats(0)

    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if len(raw) % 2:
            raw = raw[:-1]
        if _HAS_NUMPY:
            ints = _np.frombuffer(raw, dtype="<i2")
            return (ints.astype(_np.float32) / INT16_SCALE).copy()
        ints = array.array("h")
        ints.frombytes(raw)
        return array.array("f", [v / INT16_SCALE for v in ints])

    if _HAS_NUMPY:
        arr = _np.asarray(data)
        if arr.dtype == _np.float32 and arr.flags["C_CONTIGUOUS"]:
            return arr.reshape(-1)
        if arr.dtype.kind in "iu":
            return (arr.astype(_np.float32) / INT16_SCALE).reshape(-1)
        return _np.ascontiguousarray(arr, dtype=_np.float32).reshape(-1)

    if isinstance(data, array.array):
        if data.typecode == "f":
            return array.array("f", data)
        return array.array("f", [float(v) for v in data])
    return array.array("f", [float(v) for v in data])


def _zeros_like_sample(dtype_source=None):
    return _new_floats(0)


def _concat(first, second):
    """Concatenate two float sequences of the same storage flavour."""
    if _HAS_NUMPY:
        return _np.concatenate((first, second)) if len(first) and len(second) else \
            (first if len(first) else second)
    out = array.array("f", first)
    out.extend(second)
    return out


def _slice(source, start: int, stop: int):
    """Slice that works for both numpy arrays and array.array."""
    if _HAS_NUMPY:
        return source[start:stop].copy()
    return array.array("f", source[start:stop])


# --------------------------------------------------------------------------- #
# Optional transport container
# --------------------------------------------------------------------------- #

@dataclass
class AudioFrame:
    """A mono float32 block with the metadata the recogniser needs."""

    samples: Any
    sample_rate: int = TARGET_SAMPLE_RATE
    captured_at: float = field(default_factory=time.monotonic)
    rms: float = 0.0

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def duration(self) -> float:
        return self.count / float(self.sample_rate or TARGET_SAMPLE_RATE)

    def is_silent(self, threshold: float = 0.005) -> bool:
        level = self.rms or LevelMeter.rms(self.samples)
        return level < threshold


# --------------------------------------------------------------------------- #
# Ring buffer
# --------------------------------------------------------------------------- #

class AudioRingBuffer:
    """
    Fixed-capacity circular buffer of mono float32 audio.

    Writes overwrite the oldest samples once capacity is reached. Reads return
    copies, so callers may hold a snapshot for as long as they like. All public
    methods are thread-safe.
    """

    def __init__(self, capacity_seconds: float = DEFAULT_RING_SECONDS,
                 sample_rate: int = TARGET_SAMPLE_RATE) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self._capacity = max(1, int(max(0.1, float(capacity_seconds)) * self.sample_rate))
        self._buf = _new_floats(self._capacity)
        self._lock = threading.RLock()
        self._write_pos = 0          # next slot to fill, 0..capacity-1
        self._filled = 0             # valid samples currently stored
        self._total_written = 0      # absolute counter, never reset by wrap

    # -- introspection ----------------------------------------------------- #

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total_written

    @property
    def available_samples(self) -> int:
        with self._lock:
            return self._filled

    @property
    def available_seconds(self) -> float:
        return self.available_samples / float(self.sample_rate)

    def stats(self) -> Dict[str, Any]:
        """Diagnostics for the settings diagnostics page."""
        with self._lock:
            return {
                "sample_rate": self.sample_rate,
                "capacity_samples": self._capacity,
                "capacity_seconds": round(self._capacity / float(self.sample_rate), 3),
                "available_samples": self._filled,
                "available_seconds": round(self._filled / float(self.sample_rate), 3),
                "total_written": self._total_written,
                "fill_ratio": round(self._filled / float(self._capacity), 4),
            }

    # -- writing ----------------------------------------------------------- #

    def write(self, samples: ArrayLike) -> int:
        """
        Append audio, overwriting the oldest data when full.

        Returns the number of samples actually stored.
        """
        mono = _to_float32(samples)
        if mono is None or len(mono) == 0:
            return 0

        # A single oversized write only needs to keep the newest capacity slice.
        if len(mono) > self._capacity:
            mono = _slice(mono, len(mono) - self._capacity, len(mono))

        count = len(mono)
        with self._lock:
            pos = self._write_pos
            first = min(count, self._capacity - pos)
            self._buf[pos:pos + first] = mono[:first]
            if first < count:
                self._buf[0:count - first] = mono[first:]

            self._write_pos = (pos + count) % self._capacity
            self._filled = min(self._capacity, self._filled + count)
            self._total_written += count
        return count

    # -- reading ----------------------------------------------------------- #

    def read_last_samples(self, count: int):
        """
        Copy the newest *count* samples, oldest first.

        When fewer samples exist than requested, everything available is
        returned; when ``count`` exceeds capacity, the whole buffer is returned.
        """
        if count <= 0:
            return _new_floats(0)
        with self._lock:
            take = min(int(count), self._filled, self._capacity)
            if take <= 0:
                return _new_floats(0)
            start = (self._write_pos - take) % self._capacity
            if start + take <= self._capacity:
                return _slice(self._buf, start, start + take)
            head = _slice(self._buf, start, self._capacity)
            tail = _slice(self._buf, 0, take - (self._capacity - start))
            return _concat(head, tail)

    def read_last(self, seconds: float):
        """Copy the newest *seconds* of audio, oldest first."""
        return self.read_last_samples(int(max(0.0, float(seconds)) * self.sample_rate))

    def read_window(self, seconds: float, rate: int = TARGET_SAMPLE_RATE):
        """
        Convenience reader for the recogniser: last *seconds*, resampled.

        Returns an ``AudioFrame`` stamped with the capture time.
        """
        raw = self.read_last(seconds)
        converted = Resampler.resample(raw, self.sample_rate, rate) \
            if self.sample_rate != rate else _to_float32(raw)
        return AudioFrame(samples=converted, sample_rate=rate,
                          rms=LevelMeter.rms(converted))

    def read_range(self, start_absolute: int, end_absolute: int) -> Tuple[Any, int]:
        """
        Read samples by absolute write index, for incremental consumers.

        Returns ``(samples, actual_start)``; *actual_start* is raised when the
        requested start has already been overwritten, letting the caller notice
        dropped audio instead of silently losing it.
        """
        with self._lock:
            latest = self._total_written
            oldest = max(0, latest - self._filled)
            begin = max(int(start_absolute), oldest)
            finish = min(int(end_absolute), latest)
            if finish <= begin:
                return _new_floats(0), max(begin, latest)
            offset = finish - latest                    # negative or zero
            count = finish - begin
            take_from = self._write_pos + offset
            start = take_from % self._capacity
            if start + count <= self._capacity:
                data = _slice(self._buf, start, start + count)
            else:
                head = _slice(self._buf, start, self._capacity)
                tail = _slice(self._buf, 0, count - (self._capacity - start))
                data = _concat(head, tail)
            return data, begin

    # -- housekeeping ------------------------------------------------------ #

    def clear(self) -> None:
        """Wipe stored audio (also zeroed in memory, for privacy)."""
        with self._lock:
            self._buf = _new_floats(self._capacity)
            self._write_pos = 0
            self._filled = 0
            self._total_written = 0

    def level(self, window_seconds: float = 0.1) -> float:
        """Current RMS level of the newest samples (0.0 - 1.0)."""
        return LevelMeter.rms(self.read_last(window_seconds))

    def peak(self, window_seconds: float = 0.1) -> float:
        """Peak absolute amplitude of the newest samples."""
        data = self.read_last(window_seconds)
        if len(data) == 0:
            return 0.0
        if _HAS_NUMPY:
            return float(_np.max(_np.abs(data)))
        return max(abs(float(v)) for v in data)


# --------------------------------------------------------------------------- #
# Format conversion
# --------------------------------------------------------------------------- #

class Resampler:
    """Stateless mono down-mix and sample-rate conversion helpers."""

    @staticmethod
    def to_mono(samples: ArrayLike, channels: int = 1):
        """Average interleaved channels down to a single channel."""
        data = _to_float32(samples)
        channels = max(1, int(channels))
        if channels == 1 or len(data) == 0:
            return data

        usable = (len(data) // channels) * channels
        if _HAS_NUMPY:
            framed = data[:usable].reshape(-1, channels)
            return framed.mean(axis=1, dtype=_np.float32)
        frames = [data[i:i + channels] for i in range(0, usable, channels)]
        return array.array("f", [sum(f) / channels for f in frames])

    @staticmethod
    def resample(samples: ArrayLike, src_rate: int, dst_rate: int):
        """
        Linear-interpolation sample rate conversion.

        Good enough for 48 kHz -> 16 kHz speech input; anti-alias filtering is
        not required because the recogniser band-limits internally and game
        audio is already loudness-normalised.
        """
        data = _to_float32(samples)
        src_rate = max(1, int(src_rate))
        dst_rate = max(1, int(dst_rate))
        if len(data) == 0 or src_rate == dst_rate:
            return data

        out_len = max(1, int(round(len(data) * dst_rate / float(src_rate))))
        if _HAS_NUMPY:
            idx = _np.arange(out_len, dtype=_np.float64) * (src_rate / float(dst_rate))
            idx = _np.clip(idx, 0.0, len(data) - 1.0)
            low = _np.floor(idx).astype(_np.int64)
            high = _np.minimum(low + 1, len(data) - 1)
            frac = (idx - low).astype(_np.float32)
            return (data[low] * (1.0 - frac) + data[high] * frac).astype(_np.float32)

        ratio = src_rate / float(dst_rate)
        out = array.array("f", bytes(4 * out_len))
        for i in range(out_len):
            pos = min(i * ratio, len(data) - 1.0)
            low = int(pos)
            high = min(low + 1, len(data) - 1)
            frac = pos - low
            out[i] = float(data[low]) * (1.0 - frac) + float(data[high]) * frac
        return out

    @classmethod
    def convert(cls, samples: ArrayLike, src_rate: int, src_channels: int = 1,
                dst_rate: int = TARGET_SAMPLE_RATE):
        """Down-mix to mono and resample to *dst_rate* in one call."""
        mono = cls.to_mono(samples, src_channels)
        return cls.resample(mono, src_rate, dst_rate)


# --------------------------------------------------------------------------- #
# Level metering (overlay status indicator)
# --------------------------------------------------------------------------- #

class LevelMeter:
    """
    Smoothed RMS meter with separate attack and release times.

    Used by the overlay status dot: a fast attack makes speech visible
    immediately, a slow release avoids flicker between words.
    """

    SILENCE_THRESHOLD = 0.005

    def __init__(self, attack: float = 0.4, release: float = 0.08,
                 gain: float = 6.0) -> None:
        self.attack = min(1.0, max(0.0, float(attack)))
        self.release = min(1.0, max(0.0, float(release)))
        self.gain = max(0.1, float(gain))
        self._level = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def rms(samples: ArrayLike) -> float:
        """Root-mean-square amplitude of a block (0.0 for empty input)."""
        data = _to_float32(samples)
        if len(data) == 0:
            return 0.0
        if _HAS_NUMPY:
            mean_square = float(_np.mean(_np.square(data, dtype=_np.float32)))
        else:
            mean_square = sum(float(v) * float(v) for v in data) / len(data)
        return math.sqrt(max(0.0, mean_square))

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    def update(self, samples: ArrayLike) -> float:
        """Feed a block, return the new smoothed level clamped to 0.0-1.0."""
        measurement = min(1.0, self.rms(samples) * self.gain)
        with self._lock:
            coefficient = self.attack if measurement > self._level else self.release
            self._level += (measurement - self._level) * coefficient
            self._level = min(1.0, max(0.0, self._level))
            return self._level

    def reset(self) -> None:
        with self._lock:
            self._level = 0.0

    def is_silent(self, threshold: float = SILENCE_THRESHOLD) -> bool:
        """True when recent audio looks like silence (skip recognition)."""
        with self._lock:
            return self._level < threshold
