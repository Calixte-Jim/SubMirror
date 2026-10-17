"""Silero voice-activity detection (ONNX) for SubMirror.

Silero VAD is a small neural network that answers "is this frame speech?" with
a probability.  Compared with the built-in energy detector it keeps working
when the game mixes music, gunfire or crowd noise over the voices, which is
the usual reason a line is missed or split.

The detector is strictly optional:

* it runs through ``onnxruntime`` only - no torch and no extra packages;
* the model file (``models/silero_vad.onnx``, ~2 MB) is looked up next to the
  executable and next to this repository, and can also be pointed at
  explicitly through ``audio.vad_silero_path`` or ``SUBMIRROR_SILERO_VAD``;
* when either the runtime or the model is missing the caller keeps using the
  energy detector, so SubMirror never loses voice detection.

The model expects exactly 512 samples at 16 kHz plus 64 samples of context
taken from the previous frame, and carries a small recurrent state between
calls.  This module implements that contract in numpy only.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("submirror.silero_vad")

# The ONNX model only accepts this frame length at 16 kHz.
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64
STATE_SHAPE = (2, 1, 128)
MODEL_FILENAME = "silero_vad.onnx"
MODEL_ENV_VAR = "SUBMIRROR_SILERO_VAD"
# One thread is plenty: a frame costs well under a millisecond and the audio
# callback must never queue behind a wide inference pool.
SESSION_THREADS = 1


def _has_module(name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _model_roots() -> List[str]:
    """Folders that may hold ``models/silero_vad.onnx``, most specific first."""
    roots: List[str] = []
    meipass = str(getattr(sys, "_MEIPASS", "") or "")
    if meipass:
        roots.append(meipass)
    exe = str(getattr(sys, "executable", "") or "")
    if exe:
        roots.append(os.path.dirname(os.path.abspath(exe)))
    here = os.path.dirname(os.path.abspath(__file__))
    # src/audio -> src -> repository root
    roots.append(os.path.dirname(os.path.dirname(here)))
    roots.append(here)
    ordered: List[str] = []
    seen = set()
    for root in roots:
        if root and root not in seen:
            seen.add(root)
            ordered.append(root)
    return ordered


def find_model_path(configured: str = "") -> str:
    """
    Locate the Silero ONNX model.

    Order: the configured path, ``SUBMIRROR_SILERO_VAD``, then
    ``<root>/models/silero_vad.onnx`` for every candidate root.  Returns an
    absolute file path, or "" when no model is installed.
    """
    candidates: List[str] = []
    if configured:
        candidates.append(str(configured))
    env = str(os.environ.get(MODEL_ENV_VAR, "") or "")
    if env:
        candidates.append(env)
    for root in _model_roots():
        candidates.append(os.path.join(root, "models", MODEL_FILENAME))
        candidates.append(os.path.join(root, "assets", "models", MODEL_FILENAME))
    for path in candidates:
        if not path:
            continue
        try:
            if os.path.isfile(path):
                return os.path.abspath(path)
        except OSError:
            continue
    return ""


def is_available(configured: str = "") -> bool:
    """True when both the runtime and a model file are present."""
    return _has_module("onnxruntime") and bool(find_model_path(configured))


class SileroVad:
    """
    Minimal, thread-safe Silero VAD wrapper.

    ``probability(frame)`` takes exactly ``WINDOW_SAMPLES`` mono float samples
    at 16 kHz and returns the speech probability of that frame, keeping the
    recurrent state between calls until ``reset()``.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._path = str(path or "")
        self._session: Any = None
        self._state: Any = None
        self._context: Any = None
        self._error = ""
        self._stats: Dict[str, Any] = {"frames": 0, "total_ms": 0.0, "last_ms": 0.0,
                                       "failures": 0}
        self.load()

    # -- introspection ----------------------------------------------------- #

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._session is not None

    @property
    def error(self) -> str:
        return self._error

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._stats)
        frames = max(1, int(data.get("frames", 0)))
        data["avg_ms"] = round(float(data.get("total_ms", 0.0)) / frames, 3)
        data["path"] = self._path
        data["loaded"] = self.is_loaded
        return data

    # -- lifecycle --------------------------------------------------------- #

    def load(self) -> bool:
        """Create the ONNX session; returns False and records why on failure."""
        with self._lock:
            if self._session is not None:
                return True
            if not self._path:
                self._error = "model-path-empty"
                return False
            try:
                import onnxruntime

                options = onnxruntime.SessionOptions()
                options.inter_op_num_threads = SESSION_THREADS
                options.intra_op_num_threads = SESSION_THREADS
                options.log_severity_level = 3
                session = onnxruntime.InferenceSession(
                    self._path, sess_options=options, providers=["CPUExecutionProvider"])
            except Exception as exc:  # pragma: no cover - depends on the runtime
                self._error = f"{type(exc).__name__}: {exc}"
                logger.warning("Silero VAD could not be loaded (%s); "
                               "falling back to the energy detector", self._error)
                return False
            self._session = session
            self._error = ""
            self.reset()
            logger.info("Silero VAD ready (%s)", self._path)
            return True

    def reset(self) -> None:
        """Drop the recurrent state - call it between utterances/devices."""
        with self._lock:
            try:
                import numpy as np

                self._state = np.zeros(STATE_SHAPE, dtype="float32")
                self._context = np.zeros((1, CONTEXT_SAMPLES), dtype="float32")
            except Exception:
                self._state = None
                self._context = None

    def close(self) -> None:
        with self._lock:
            self._session = None
            self._state = None
            self._context = None

    # -- inference --------------------------------------------------------- #

    def probability(self, frame: Any) -> Optional[float]:
        """
        Speech probability of one ``WINDOW_SAMPLES`` frame (0.0 - 1.0).

        Returns None when the model is unavailable or the frame has the wrong
        length, so callers can fall back instead of guessing.
        """
        try:
            import numpy as np
        except Exception:
            return None
        with self._lock:
            session = self._session
            if session is None:
                return None
            try:
                audio = np.asarray(frame, dtype="float32").reshape(-1)
                if audio.size != WINDOW_SAMPLES:
                    return None
                if self._state is None or self._context is None:
                    self.reset()
                context = self._context
                padded = np.concatenate([context, audio[None, :]], axis=1).astype("float32")
                started = time.monotonic()
                outputs = session.run(None, {
                    "input": padded,
                    "state": self._state,
                    "sr": np.array(16000, dtype="int64"),
                })
                elapsed = (time.monotonic() - started) * 1000.0
                probability, state = outputs[0], outputs[1]
                self._state = np.asarray(state, dtype="float32").reshape(STATE_SHAPE)
                self._context = padded[:, -CONTEXT_SAMPLES:]
                self._stats["frames"] += 1
                self._stats["total_ms"] += elapsed
                self._stats["last_ms"] = elapsed
                return float(np.asarray(probability).reshape(-1)[0])
            except Exception:
                self._stats["failures"] += 1
                logger.exception("Silero VAD inference failed")
                return None
