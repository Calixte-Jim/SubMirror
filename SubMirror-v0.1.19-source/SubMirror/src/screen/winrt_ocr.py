# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - WinRT OCR engine (Windows.Media.Ocr).

Why this file exists
    v0.1 ships exactly one OCR engine: the OCR component that is part of
    Windows itself. That choice removes PaddleOCR/RapidOCR (and their ~500 MB of
    wheels) from the dependency list, removes a whole class of model-download
    failures, and keeps every recognised pixel inside the operating system:
    the text never has to be handed to a third party library.

What it does
    * Wraps ``Windows.Media.Ocr.OcrEngine`` (reached through the ``winsdk``
      projection, with ``winrt`` accepted as a fallback import name).
    * Converts the capture buffer (BGR / BGRA / grey) into the BGRA8
      ``SoftwareBitmap`` that WinRT expects, using numpy.
    * Maps WinRT word rectangles back into **full screen** coordinates so the
      caller can overlay them on the original frame.
    * Never raises: a failure is returned as an ``WinrtOcrResult`` with
      ``ok=False``, exactly like the rest of the pipeline expects.

Deliberate limits
    * ``OcrEngine`` is not thread safe. Every recognition is serialised behind
      an ``RLock``, and if an engine instance reports an error we drop it and
      build a fresh one for the retry.
    * WinRT exposes no per-line confidence. Lines therefore carry
      ``confidence=0.0``, so the optional confidence threshold can only ever
      apply to engines that *do* score their lines; every Windows OCR line is
      treated as "unknown", never as "useless".
    * Line filtering is done with the height / character-count pair instead
      (``filter_lines()``), and the best line is always kept, so a filter can
      never delete every line and turn a good recognition into "no text".
    * Large frames are downscaled and small frames are upscaled before the
      call: Windows OCR needs a sane text height and rejects anything above
      ``OcrEngine.max_image_dimension``. Both scale factors are undone on the
      way out, so callers only ever see original-frame coordinates.

Safety: this module reads an in-memory bitmap that SubMirror captured itself.
It never injects into a process, never hooks anything and never reads another
program's memory.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "WinrtOcrError",
    "WinrtUnavailableError",
    "WinrtLanguageError",
    "WinrtOcrTimeoutError",
    "WinrtOcrLine",
    "WinrtOcrResult",
    "WinrtOcrEngine",
    "create_engine",
    "ocr_availability",
    "available_languages",
    "COMMON_LANGUAGE_TAGS",
]

logger = logging.getLogger("submirror.screen.winrt_ocr")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ENGINE_NAME = "winrt"
ENGINE_DISPLAY_NAME = "Windows.Media.Ocr"

# Import names tried in order: the modern PyWinRT projection, then the older one.
PROJECTION_ROOTS: Tuple[str, ...] = ("winsdk", "winrt")

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MIN_TEXT_HEIGHT = 32     # upscale crops smaller than this
MAX_UPSCALE = 4
POLL_INTERVAL_S = 0.01

# Line filtering (BL-3, option B).
#   Windows OCR cannot report a per-line confidence, so the "confidence
#   threshold" the checklist asks for cannot be the thing that decides.  The
#   real filter is the pair (minimum line height, minimum character count):
#   a line is dropped when it is *either* too flat *or* too short.  Because
#   that pair can in principle reject every line - which would silently turn
#   a perfect recognition into "no text" - the best line is always kept as a
#   floor.  ``KEEP_BEST_LINE`` is not a config option on purpose: it is a
#   guarantee, and a guarantee that can be switched off is not one.
KEEP_BEST_LINE = True

# Common Windows OCR language tags, used by the settings panel dropdown.
COMMON_LANGUAGE_TAGS: Tuple[str, ...] = (
    "en-US", "zh-Hans-CN", "zh-Hant-TW", "ja-JP", "ko-KR", "ru-RU",
)

# AsyncStatus values from Windows.Foundation.
_ASYNC_STARTED = 0
_ASYNC_COMPLETED = 1
_ASYNC_CANCELED = 2


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class WinrtOcrError(Exception):
    """Base class for every WinRT OCR problem."""


class WinrtUnavailableError(WinrtOcrError):
    """The WinRT projection or the OCR component is not usable here."""


class WinrtLanguageError(WinrtOcrError):
    """No OCR language pack is installed for the requested language."""


class WinrtOcrTimeoutError(WinrtOcrError):
    """recognize_async did not complete inside the timeout."""


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #

Box = Tuple[Tuple[float, float], ...]


@dataclass
class WinrtOcrLine:
    """One recognised text line, in full frame coordinates."""

    text: str = ""
    confidence: float = 0.0          # Windows OCR does not report one
    box: Optional[Box] = None        # 4 points, clockwise from top-left

    @property
    def rect(self) -> Optional[Tuple[float, float, float, float]]:
        if not self.box:
            return None
        xs = [point[0] for point in self.box]
        ys = [point[1] for point in self.box]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    @property
    def center(self) -> Optional[Tuple[float, float]]:
        rect = self.rect
        if rect is None:
            return None
        return (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)

    @property
    def height(self) -> float:
        rect = self.rect
        return float(rect[3]) if rect else 0.0

    @property
    def has_confidence(self) -> bool:
        return self.confidence > 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "confidence": round(self.confidence, 4),
                "rect": self.rect}


@dataclass
class WinrtOcrResult:
    """The outcome of one recognition, success or failure."""

    lines: List[WinrtOcrLine] = field(default_factory=list)
    engine: str = ENGINE_NAME
    language: str = ""
    region: Any = None
    offset: Tuple[int, int] = (0, 0)
    elapsed_ms: float = 0.0
    ok: bool = True
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def text(self) -> str:
        return self.joined()

    @property
    def mean_confidence(self) -> float:
        scored = [line.confidence for line in self.lines if line.has_confidence]
        if not scored:
            return 0.0
        return sum(scored) / len(scored)

    def sorted_lines(self) -> List[WinrtOcrLine]:
        """Reading order: rows first, left to right inside a row."""
        def sort_key(line: WinrtOcrLine) -> Tuple[int, float]:
            rect = line.rect
            if rect is None:
                return (0, 0.0)
            row = int(rect[1] // max(8.0, line.height / 2.0)) if line.height else 0
            return (row, rect[0])

        return sorted(self.lines, key=sort_key)

    def joined(self, separator: str = " ", limit: int = 0) -> str:
        parts = [line.text.strip() for line in self.sorted_lines()
                 if line.text and line.text.strip()]
        text = separator.join(parts)
        if limit and len(text) > limit:
            return text[: max(0, limit - 1)].rstrip() + "\u2026"
        return text

    def filter_lines(self, min_height: float = 0.0, min_chars: int = 0,
                     min_confidence: float = 0.0,
                     keep_best: bool = KEEP_BEST_LINE) -> List[WinrtOcrLine]:
        """
        Drop lines that are too flat or too short (BL-3, option B).

        A line is rejected when *either* filter rejects it:

        * ``min_height`` - its pixel height is below the threshold.  A line
          with no box has an unknown height and is **not** rejected on this
          test: unknown is not the same as too small.
        * ``min_chars`` - its stripped text is shorter than the threshold.
        * ``min_confidence`` - only applies to lines that actually carry a
          score (``has_confidence``).  Windows OCR lines are exempt.

        A threshold of ``0`` disables its test.  When every threshold is
        disabled the whole list is returned untouched.

        ``keep_best`` is the floor: if the three tests together would leave
        nothing, the single most substantial line (tallest, then longest) is
        kept instead of returning an empty result.  Losing a perfectly good
        recognition to an over-eager filter is a bug, not a feature.
        """
        lines = list(self.lines)
        if not lines:
            return lines
        if min_height <= 0.0 and min_chars <= 0 and min_confidence <= 0.0:
            return lines

        kept: List[WinrtOcrLine] = []
        for line in lines:
            if (min_confidence > 0.0 and line.has_confidence
                    and line.confidence < min_confidence):
                continue
            if min_height > 0.0 and line.height and line.height < min_height:
                continue
            text = (line.text or "").strip()
            if min_chars > 0 and len(text) < min_chars:
                continue
            kept.append(line)

        if kept or not keep_best:
            return kept
        return [self._best_line(lines)]

    @staticmethod
    def _best_line(lines: Sequence[WinrtOcrLine]) -> WinrtOcrLine:
        """The line most likely to be real text: tallest, then longest."""

        def rank(line: WinrtOcrLine) -> Tuple[float, int]:
            return (line.height, len((line.text or "").strip()))

        return max(lines, key=rank)

    def filter_by_confidence(self, minimum: float) -> List[WinrtOcrLine]:
        """Confidence-only filter, kept for callers written before BL-3."""
        return self.filter_lines(min_confidence=minimum)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "error": self.error, "engine": self.engine,
                "language": self.language, "elapsed_ms": round(self.elapsed_ms, 2),
                "offset": list(self.offset),
                "lines": [line.as_dict() for line in self.lines]}

    @classmethod
    def failure(cls, error: str, region: Any = None,
                offset: Tuple[int, int] = (0, 0),
                elapsed_ms: float = 0.0, language: str = "") -> "WinrtOcrResult":
        return cls(lines=[], language=language, region=region, offset=offset,
                   elapsed_ms=elapsed_ms, ok=False, error=str(error))


# --------------------------------------------------------------------------- #
# Projection discovery (cheap, no import side effects)
# --------------------------------------------------------------------------- #

def _projection_name() -> str:
    """Name of the installed WinRT projection, or '' when there is none."""
    for name in PROJECTION_ROOTS:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except (ImportError, ValueError):
            continue
    return ""


def ocr_availability() -> Tuple[bool, str]:
    """
    Cheap probe: is a WinRT projection importable? No import, no I/O.

    Returns (available, reason). This is what main.py reports at startup so a
    missing projection only disables the OCR line instead of crashing.
    """
    name = _projection_name()
    if not name:
        return False, "the WinRT projection (winsdk) is not installed"
    return True, f"WinRT projection '{name}' is available"


class _Projection:
    """Holds the WinRT types this module needs, imported once on demand."""

    def __init__(self) -> None:
        self.root = ""
        self.ocr_engine: Any = None
        self.language: Any = None
        self.software_bitmap: Any = None
        self.bitmap_pixel_format: Any = None
        self.bitmap_alpha_mode: Any = None
        self.crypto_buffer: Any = None
        self.in_memory_stream: Any = None
        self.data_writer: Any = None
        self.bitmap_decoder: Any = None
        self.error = ""

    @property
    def ready(self) -> bool:
        return bool(self.ocr_engine and self.language and self.software_bitmap)

    def load(self) -> bool:
        if self.ready:
            return True
        name = _projection_name()
        if not name:
            self.error = "the WinRT projection (winsdk) is not installed"
            return False
        try:
            media_ocr = importlib.import_module(f"{name}.windows.media.ocr")
            globalization = importlib.import_module(f"{name}.windows.globalization")
            imaging = importlib.import_module(f"{name}.windows.graphics.imaging")
            self.ocr_engine = media_ocr.OcrEngine
            self.language = globalization.Language
            self.software_bitmap = imaging.SoftwareBitmap
            self.bitmap_pixel_format = imaging.BitmapPixelFormat
            self.bitmap_alpha_mode = imaging.BitmapAlphaMode
            self.root = name
        except Exception as exc:                            # noqa: BLE001
            self.error = f"could not import the WinRT OCR types: {exc}"
            logger.warning("WinRT OCR types are unavailable: %s", exc)
            return False
        # Optional helpers for building the IBuffer; both are import guarded so
        # a projection that lacks them still works through the other path.
        try:
            streams = importlib.import_module(f"{name}.windows.storage.streams")
            self.in_memory_stream = getattr(streams, "InMemoryRandomAccessStream", None)
            self.data_writer = getattr(streams, "DataWriter", None)
            self.bitmap_decoder = getattr(imaging, "BitmapDecoder", None)
        except Exception:                                   # noqa: BLE001
            self.in_memory_stream = self.data_writer = self.bitmap_decoder = None
        try:
            crypto = importlib.import_module(f"{name}.windows.security.cryptography")
            self.crypto_buffer = getattr(crypto, "CryptographicBuffer", None)
        except Exception:                                   # noqa: BLE001
            self.crypto_buffer = None
        return self.ready


_PROJECTION = _Projection()
_PROJECTION_LOCK = threading.Lock()


def _projection() -> _Projection:
    """Shared projection handle; imports happen at most once."""
    if _PROJECTION.ready:
        return _PROJECTION
    with _PROJECTION_LOCK:
        _PROJECTION.load()
    return _PROJECTION


def _enum_member(enum_class: Any, *names: str) -> Any:
    """
    Return the first member of a WinRT enum that actually exists, else None.

    The two supported projections spell members differently: winsdk upper-cases
    them (``BGRA8`` / ``PREMULTIPLIED``) while the metadata spelling used by the
    other one is ``Bgra8`` / ``Premultiplied``. Resolving by name -- instead of
    hard-coding one spelling -- is what keeps bitmap creation working on both.
    """
    for name in names:
        member = getattr(enum_class, name, None)
        if member is not None:
            return member
    lowered = {str(attr).lower(): attr for attr in dir(enum_class)
               if not str(attr).startswith("_")}
    for name in names:
        actual = lowered.get(name.lower())
        if actual is not None:
            return getattr(enum_class, actual, None)
    return None


# --------------------------------------------------------------------------- #
# Image conversion
# --------------------------------------------------------------------------- #

def _load_numpy() -> Any:
    """numpy is a required dependency for OCR; return None when missing."""
    try:
        import numpy  # type: ignore[import-not-found]

        return numpy
    except Exception:                                       # noqa: BLE001
        return None


def _to_bgra_array(image: Any) -> Tuple[Any, int, int, float, float]:
    """
    Convert a captured frame into a contiguous BGRA8 array.

    Accepts (H, W, 3) BGR, (H, W, 4) BGRA, (H, W) grey, or a ScreenFrame-like
    object exposing ``image`` / ``raw`` + ``width`` + ``height``.

    Returns (array, width, height, scale_x, scale_y) where the scale factors
    map the array back to the original crop. Raises WinrtOcrError when the
    input cannot be interpreted.
    """
    numpy = _load_numpy()
    if numpy is None:
        raise WinrtUnavailableError("numpy is required for OCR image conversion")

    array = image
    if not hasattr(array, "shape"):
        array = _array_from_frame(image, numpy)

    if array is None or not hasattr(array, "shape"):
        raise WinrtOcrError("unsupported image object")

    if array.ndim == 2:
        height, width = array.shape
        array = numpy.repeat(array[:, :, None], 3, axis=2)
    elif array.ndim == 3:
        height, width = array.shape[0], array.shape[1]
        channels = array.shape[2]
        if channels == 1:
            array = numpy.repeat(array, 3, axis=2)
        elif channels not in (3, 4):
            raise WinrtOcrError(f"unsupported channel count: {channels}")
    else:
        raise WinrtOcrError(f"unsupported image shape: {array.shape}")

    if array.dtype != numpy.uint8:
        array = numpy.clip(array, 0, 255).astype(numpy.uint8)

    # WinRT needs BGRA8; add an opaque alpha channel to BGR input.
    if array.shape[2] == 3:
        alpha = numpy.full((height, width, 1), 255, dtype=numpy.uint8)
        array = numpy.concatenate((array, alpha), axis=2)
    array = numpy.ascontiguousarray(array)

    scale_x = scale_y = 1.0
    target_w, target_h = int(width), int(height)

    # Scale up very small crops: Windows OCR needs a minimum text height.
    if target_h and target_h < DEFAULT_MIN_TEXT_HEIGHT:
        factor = min(MAX_UPSCALE,
                     max(1, int(round(DEFAULT_MIN_TEXT_HEIGHT / float(target_h)))))
        if factor > 1:
            array = numpy.repeat(numpy.repeat(array, factor, axis=0), factor, axis=1)
            scale_x = scale_y = float(factor)
            target_w, target_h = int(array.shape[1]), int(array.shape[0])

    # Downscale anything above the engine limit so recognize_async accepts it.
    limit = _max_image_dimension()
    longest = max(target_w, target_h)
    if limit and longest > limit:
        step = int(longest // limit) + 1
        array = numpy.ascontiguousarray(array[::step, ::step])
        scale_x *= 1.0 / step
        scale_y *= 1.0 / step
        target_w, target_h = int(array.shape[1]), int(array.shape[0])

    del width, height
    return array, target_w, target_h, scale_x, scale_y


def _array_from_frame(frame: Any, numpy: Any) -> Any:
    """Rebuild an array from a ScreenFrame-like object's raw buffer."""
    image = getattr(frame, "image", None)
    if image is not None and hasattr(image, "shape"):
        return image
    raw = getattr(frame, "raw", None)
    width = int(getattr(frame, "width", 0) or 0)
    height = int(getattr(frame, "height", 0) or 0)
    if raw is None or width <= 0 or height <= 0:
        return None
    data = bytes(raw)
    pixels = width * height
    if len(data) >= pixels * 4:
        return numpy.frombuffer(data[: pixels * 4], dtype=numpy.uint8
                                ).reshape(height, width, 4)
    if len(data) >= pixels * 3:
        return numpy.frombuffer(data[: pixels * 3], dtype=numpy.uint8
                                ).reshape(height, width, 3)
    raise WinrtOcrError("the raw frame is smaller than its declared size")


def _max_image_dimension() -> int:
    engine = _PROJECTION.ocr_engine
    try:
        value = int(getattr(engine, "max_image_dimension", 0) or 0)
    except Exception:                                       # noqa: BLE001
        return 0
    return value if value > 0 else 0


def _as_float(value: Any, default: float,
              low: float = 0.0, high: float = 1.0) -> float:
    """Coerce a config value to a clamped float; never raises."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:                                    # NaN
        return float(default)
    return min(max(number, low), high)


def _as_int(value: Any, default: int, low: int = 0, high: int = 1000) -> int:
    """Coerce a config value to a clamped int; never raises."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return min(max(number, low), high)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class WinrtOcrEngine:
    """
    Single-engine OCR service. ``recognize()`` never raises; failures come back
    as ``WinrtOcrResult(ok=False, error=...)`` so one bad frame can never stop
    the OCR lane.
    """

    name = ENGINE_NAME
    display_name = ENGINE_DISPLAY_NAME
    supports_gpu = False
    priority = 10
    default_timeout_s = DEFAULT_TIMEOUT_S

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._engine: Any = None
        self._language_tag = ""
        self._loaded = False
        self._error = ""
        self._stats: Dict[str, Any] = {
            "calls": 0, "failures": 0, "timeouts": 0, "reloads": 0,
            "total_ms": 0.0, "last_ms": 0.0, "lines": 0, "filtered": 0,
        }
        # Line filter thresholds (BL-3); apply_config() overwrites them.
        self._min_confidence = 0.0
        self._min_line_height = 0.0
        self._min_line_chars = 0
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def _get(self, key: str, default: Any = None) -> Any:
        getter = getattr(self._config, "get", None)
        if not callable(getter):
            return default
        try:
            value = getter(key, default)
        except Exception:                                   # noqa: BLE001
            return default
        return default if value is None else value

    def apply_config(self) -> None:
        """Re-read the preferences that affect engine creation."""
        auto = self._get("ocr.winrt_language_auto", True)
        self._auto_language = bool(auto) if isinstance(auto, bool) else (
            str(auto).strip().lower() in ("1", "true", "yes", "on"))
        self._preferred_language = str(self._get("ocr.winrt_language", "") or "").strip()
        self._timeout_s = DEFAULT_TIMEOUT_S
        try:
            self._timeout_s = max(1.0, float(
                self._get("ocr.timeout_seconds", DEFAULT_TIMEOUT_S)))
        except (TypeError, ValueError):
            self._timeout_s = DEFAULT_TIMEOUT_S
        requested = (self._preferred_language
                     if not self._auto_language else "")
        if requested and requested != self._language_tag:
            self.unload()                                   # language changed

        # Line filtering (BL-3). Defaults are all "off", so an existing
        # config.json behaves exactly as it did before this change.
        self._min_confidence = _as_float(
            self._get("ocr.confidence_min", 0.0), 0.0, 0.0, 1.0)
        self._min_line_height = _as_float(
            self._get("ocr.min_line_height", 0.0), 0.0, 0.0, 500.0)
        self._min_line_chars = _as_int(
            self._get("ocr.min_line_chars", 0), 0, 0, 500)

    # -- properties -------------------------------------------------------- #

    @property
    def is_loaded(self) -> bool:
        return self._loaded and self._engine is not None

    @property
    def language(self) -> str:
        return self._language_tag

    @property
    def error(self) -> str:
        return self._error

    # -- availability ------------------------------------------------------ #

    @classmethod
    def is_available(cls) -> Tuple[bool, str]:
        return ocr_availability()

    def available_languages(self) -> List[str]:
        """Installed OCR language tags; [] when the projection is missing."""
        projection = _projection()
        if not projection.ready:
            return []
        try:
            languages = getattr(projection.ocr_engine,
                                "available_recognizer_languages", None)
            tags: List[str] = []
            for language in list(languages or []):
                tag = str(getattr(language, "language_tag", "") or "")
                if tag:
                    tags.append(tag)
            return tags
        except Exception:                                   # noqa: BLE001
            logger.debug("could not list the OCR languages", exc_info=True)
            return []

    # -- lifecycle --------------------------------------------------------- #

    def load(self) -> bool:
        """Create an OcrEngine for the configured (or first available) language."""
        with self._lock:
            if self.is_loaded:
                return True
            projection = _projection()
            if not projection.ready:
                self._error = projection.error or "the WinRT OCR types are unavailable"
                self._loaded = False
                return False
            engine = None
            tag = ""
            candidates: List[str] = []
            if self._preferred_language and not self._auto_language:
                candidates.append(self._preferred_language)
            installed = self.available_languages()
            if self._preferred_language and self._auto_language:
                candidates.append(self._preferred_language)
            candidates.extend(installed)
            if not candidates:
                candidates.append("")                        # profile languages
            for candidate in candidates:
                try:
                    if candidate:
                        engine = projection.ocr_engine.try_create_from_language(
                            projection.language(candidate))
                    else:
                        engine = projection.ocr_engine.try_create_from_user_profile_languages()
                except Exception:                           # noqa: BLE001
                    logger.debug("OCR engine creation for %s failed",
                                 candidate or "<profile>", exc_info=True)
                    engine = None
                if engine is not None:
                    tag = candidate
                    break
            if engine is None:
                self._error = ("no Windows OCR language pack is installed; add one "
                               "under Settings > Time & language > Language")
                self._loaded = False
                logger.warning("WinRT OCR could not create an engine: %s", self._error)
                return False
            self._engine = engine
            self._language_tag = tag or self._installed_first(installed)
            self._loaded = True
            self._error = ""
            logger.info("WinRT OCR engine ready (language=%s)",
                        self._language_tag or "system profile")
            return True

    @staticmethod
    def _installed_first(installed: Sequence[str]) -> str:
        return str(installed[0]) if installed else ""

    def unload(self) -> None:
        with self._lock:
            self._engine = None
            self._loaded = False
            self._language_tag = ""

    def warmup(self) -> bool:
        return self.load()

    def close(self) -> None:
        self.unload()

    # -- recognition ------------------------------------------------------- #

    def recognize(self, image: Any, region: Any = None,
                  offset: Optional[Tuple[int, int]] = None,
                  timeout_s: Optional[float] = None) -> WinrtOcrResult:
        """
        Recognise one captured frame. Never raises.

        ``region`` is carried into the result for logging only; ``offset`` is the
        full screen position of the crop's top-left corner and is added to every
        returned rectangle so the caller can draw over the original frame.
        """
        started = time.perf_counter()
        origin = self._offset_of(region, offset)
        try:
            with self._lock:
                with self._stats_lock():
                    self._stats["calls"] += 1
                if not self.load():
                    return self._finish(
                        WinrtOcrResult.failure(self._error or "engine unavailable",
                                               region, origin, language=self._language_tag),
                        started)
                array, width, height, scale_x, scale_y = _to_bgra_array(image)
                bitmap = self._make_bitmap(array, width, height)
                if bitmap is None:
                    return self._finish(
                        WinrtOcrResult.failure(
                            "the frame could not be converted to a SoftwareBitmap",
                            region, origin, language=self._language_tag), started)
                raw_lines = self._recognize_once(
                    bitmap, timeout_s or self._timeout_s)
                lines = self._map_lines(raw_lines, origin, scale_x, scale_y)
                with self._stats_lock():
                    self._stats["lines"] += len(lines)
                lines = self._filter(lines)
                return self._finish(
                    WinrtOcrResult(lines=lines, language=self._language_tag,
                                   region=region, offset=origin),
                    started)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("WinRT OCR failed: %s", exc)
            with self._stats_lock():
                self._stats["failures"] += 1
            return self._finish(
                WinrtOcrResult.failure(f"{exc.__class__.__name__}: {exc}",
                                       region, origin, language=self._language_tag),
                started)

    def recognize_frame(self, frame: Any, region: Any = None,
                        offset: Optional[Tuple[int, int]] = None,
                        timeout_s: Optional[float] = None) -> WinrtOcrResult:
        """Convenience wrapper for a ScreenFrame-like object."""
        if region is None:
            region = getattr(frame, "region", None)
        return self.recognize(frame, region=region, offset=offset,
                              timeout_s=timeout_s)

    # -- internals --------------------------------------------------------- #

    def _filter(self, lines: List[WinrtOcrLine]) -> List[WinrtOcrLine]:
        """Apply the panel's line filter to a freshly mapped line list."""
        if not lines:
            return lines
        result = WinrtOcrResult(lines=lines)
        kept = result.filter_lines(min_height=self._min_line_height,
                                   min_chars=self._min_line_chars,
                                   min_confidence=self._min_confidence)
        dropped = len(lines) - len(kept)
        if dropped > 0:
            with self._stats_lock():
                self._stats["filtered"] = self._stats.get("filtered", 0) + dropped
            logger.debug("line filter dropped %d/%d line(s)", dropped, len(lines))
        return kept

    @staticmethod
    def _stats_lock() -> threading.RLock:
        return _STATS_LOCK

    @staticmethod
    def _offset_of(region: Any, offset: Optional[Tuple[int, int]]) -> Tuple[int, int]:
        if offset is not None:
            try:
                return (int(offset[0]), int(offset[1]))
            except (TypeError, ValueError, IndexError):
                return (0, 0)
        left = int(getattr(region, "left", 0) or 0)
        top = int(getattr(region, "top", 0) or 0)
        return (left, top)

    def _finish(self, result: WinrtOcrResult, started: float) -> WinrtOcrResult:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result.elapsed_ms = elapsed_ms
        with self._stats_lock():
            self._stats["last_ms"] = elapsed_ms
            self._stats["total_ms"] += elapsed_ms
        return result

    def _make_bitmap(self, array: Any, width: int, height: int) -> Any:
        """Build a BGRA8 SoftwareBitmap through whichever path is available."""
        projection = _projection()
        payload = array.tobytes()
        buffer = self._make_buffer(payload)
        if buffer is None:
            return None
        pixel_format = _enum_member(projection.bitmap_pixel_format, "Bgra8", "BGRA8")
        alpha_mode = _enum_member(projection.bitmap_alpha_mode,
                                  "Premultiplied", "PREMULTIPLIED")
        if pixel_format is None or alpha_mode is None:
            logger.warning(
                "the WinRT projection exposes no BGRA8/premultiplied enum members; "
                "OCR bitmaps cannot be built")
            return None
        try:
            return projection.software_bitmap.create_copy_from_buffer(
                buffer,
                pixel_format,
                int(width),
                int(height),
                alpha_mode)
        except Exception as exc:                            # noqa: BLE001
            logger.warning(
                "create_copy_from_buffer failed, falling back to the decoder: %s", exc)
        return self._bitmap_from_stream(payload)

    def _make_buffer(self, payload: bytes) -> Any:
        """Wrap raw BGRA bytes in an IBuffer (two possible projection paths)."""
        projection = _projection()
        crypto = projection.crypto_buffer
        if crypto is not None:
            for attempt in (payload, memoryview(payload), list(payload)):
                try:
                    return crypto.create_from_byte_array(attempt)
                except Exception:                           # noqa: BLE001
                    continue
        return None

    def _bitmap_from_stream(self, payload: bytes) -> Any:
        """Fallback: decode a BMP-less raw dump through DataWriter + BitmapDecoder."""
        projection = _projection()
        if not (projection.in_memory_stream and projection.data_writer
                and projection.bitmap_decoder):
            return None
        try:
            stream = projection.in_memory_stream()
            writer = projection.data_writer(stream.get_output_stream_at(0))
            writer.write_bytes(payload)
            writer.store_async().get()
            stream.seek(0)
            decoder = projection.bitmap_decoder.create_async(stream).get()
            return decoder.get_software_bitmap_async().get()
        except Exception:                                   # noqa: BLE001
            logger.debug("the stream based bitmap path failed", exc_info=True)
            return None

    def _recognize_once(self, bitmap: Any, timeout_s: float) -> Any:
        """Run recognize_async, retrying once with a fresh engine on error."""
        for attempt in (0, 1):
            try:
                operation = self._engine.recognize_async(bitmap)
                return self._await(operation, timeout_s)
            except WinrtOcrTimeoutError:
                raise
            except Exception as exc:                        # noqa: BLE001
                if attempt == 1:
                    raise
                logger.debug("recognize_async failed, rebuilding the engine: %s",
                             exc, exc_info=True)
                with self._stats_lock():
                    self._stats["reloads"] += 1
                self.unload()
                if not self.load():
                    raise WinrtUnavailableError(
                        self._error or "the WinRT OCR engine could not be rebuilt")
        return None

    @staticmethod
    def _await(operation: Any, timeout_s: float) -> Any:
        """Block for an IAsyncOperation with a real timeout and no hang."""
        if operation is None:
            raise WinrtOcrError("recognize_async returned nothing")
        status = getattr(operation, "status", None)
        if status is None:                                  # no status: just wait
            getter = getattr(operation, "get", None)
            if callable(getter):
                return getter()
            raise WinrtOcrError("the async operation cannot be awaited")
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        while int(getattr(operation, "status", _ASYNC_COMPLETED)) == _ASYNC_STARTED:
            if time.monotonic() > deadline:
                raise WinrtOcrTimeoutError(
                    f"recognize_async did not finish in {timeout_s:.1f}s")
            time.sleep(POLL_INTERVAL_S)
        state = int(getattr(operation, "status", _ASYNC_COMPLETED))
        if state == _ASYNC_COMPLETED:
            results = getattr(operation, "get_results", None)
            if callable(results):
                return results()
            getter = getattr(operation, "get", None)
            if callable(getter):
                return getter()
            raise WinrtOcrError("the async operation exposes no results")
        if state == _ASYNC_CANCELED:
            raise WinrtOcrError("recognize_async was cancelled")
        raise WinrtOcrError(f"recognize_async failed (status={state})")

    def _map_lines(self, result: Any, origin: Tuple[int, int],
                   scale_x: float, scale_y: float) -> List[WinrtOcrLine]:
        """Turn WinRT lines/words into full frame coordinates."""
        lines: List[WinrtOcrLine] = []
        for raw_line in list(getattr(result, "lines", None) or []):
            text = str(getattr(raw_line, "text", "") or "").strip()
            if not text:
                continue
            box = self._line_box(raw_line)
            if box is not None:
                box = tuple(
                    ((point[0] / scale_x) + origin[0],
                     (point[1] / scale_y) + origin[1]) for point in box)
            lines.append(WinrtOcrLine(text=text, confidence=0.0, box=box))
        return lines

    @staticmethod
    def _line_box(raw_line: Any) -> Optional[Box]:
        """Union of the word rectangles of one WinRT line."""
        left = top = None
        right = bottom = None
        for word in list(getattr(raw_line, "words", None) or []):
            rect = getattr(word, "bounding_rect", None)
            if rect is None:
                continue
            x0 = float(getattr(rect, "x", 0.0))
            y0 = float(getattr(rect, "y", 0.0))
            width = float(getattr(rect, "width", 0.0))
            height = float(getattr(rect, "height", 0.0))
            left = x0 if left is None else min(left, x0)
            top = y0 if top is None else min(top, y0)
            right = x0 + width if right is None else max(right, x0 + width)
            bottom = y0 + height if bottom is None else max(bottom, y0 + height)
        if left is None or top is None or right is None or bottom is None:
            return None
        return ((left, top), (right, top), (right, bottom), (left, bottom))

    # -- diagnostics ------------------------------------------------------- #

    def stats(self) -> Dict[str, Any]:
        with self._lock, _STATS_LOCK:
            data = dict(self._stats)
        calls = max(1, int(data.get("calls", 0)))
        data["engine"] = self.name
        data["language"] = self._language_tag
        data["loaded"] = self.is_loaded
        data["error"] = self._error
        data["projection"] = _PROJECTION.root
        data["avg_ms"] = round(float(data.get("total_ms", 0.0)) / calls, 2)
        return data

    def __enter__(self) -> "WinrtOcrEngine":
        self.load()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.unload()
        return False


_STATS_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Module level helpers
# --------------------------------------------------------------------------- #

def create_engine(config: Any = None) -> WinrtOcrEngine:
    """Build the v0.1 OCR engine (the only one; no registry, no switching)."""
    return WinrtOcrEngine(config)


def available_languages() -> List[str]:
    """Installed Windows OCR language tags (empty when unavailable)."""
    return WinrtOcrEngine().available_languages()
