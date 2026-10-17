"""
Change detection for the fixed-region OCR loop (v0.1.13).

The recurring region loop must not hammer the OCR engine with an unchanged
picture, and it must not re-translate a line that is already on screen.  Both
checks live here so they can be reasoned about - and unit tested - without Qt,
without a screen and without an OCR engine.

This module never captures anything.  It only turns one already-captured frame
into a small luminance fingerprint, compares two fingerprints, and compares two
recognised strings.
"""

import logging
import re
from typing import Any, Optional, Sequence, Tuple

__all__ = [
    "FINGERPRINT_SIZE",
    "DEFAULT_TOLERANCE",
    "MIN_CHANGED_CELLS",
    "parse_region",
    "format_region",
    "fingerprint",
    "is_changed",
    "normalize_text",
    "text_changed",
]

logger = logging.getLogger("submirror.region_watch")

# The picture is reduced to a SIZE x SIZE grid of average luminance values, so
# one comparison costs SIZE * SIZE cheap samples instead of a full region scan.
FINGERPRINT_SIZE = 16

# A cell counts as "different" when its luminance moved by more than this many
# levels (0-255).  Antialiasing and video noise stay well below it, a new line
# of text - or any real content change - moves far past it.
DEFAULT_TOLERANCE = 8

# How many differing cells are needed before the picture counts as changed.  A
# couple of cells may wobble on their own (a blinking cursor, one glyph), a new
# subtitle line rewrites most of the grid.
MIN_CHANGED_CELLS = 4

# "left,top,width,height"
_REGION_RE = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Region text <-> tuple
# --------------------------------------------------------------------------- #

def parse_region(text: Any) -> Optional[Tuple[int, int, int, int]]:
    """
    Parse a stored ``"left,top,width,height"`` string into a tuple.

    Returns None for anything unusable (empty, malformed, zero-sized), so a
    hand-edited config can never produce a region that breaks the loop.
    """
    if text is None:
        return None
    if isinstance(text, (tuple, list)) and len(text) >= 4:
        try:
            left, top, width, height = (int(text[0]), int(text[1]),
                                        int(text[2]), int(text[3]))
        except (TypeError, ValueError):
            return None
        return (left, top, width, height) if width > 0 and height > 0 else None
    match = _REGION_RE.match(str(text))
    if not match:
        return None
    left, top, width, height = (int(match.group(1)), int(match.group(2)),
                                int(match.group(3)), int(match.group(4)))
    if width <= 0 or height <= 0:
        return None
    return left, top, width, height


def format_region(region: Any) -> str:
    """Serialise a region (tuple, list or ImageRegion-like) back to text."""
    if region is None:
        return ""
    values = None
    if isinstance(region, (tuple, list)):
        values = list(region)
    else:
        getter = getattr(region, "as_tuple", None)
        if callable(getter):
            try:
                values = list(getter())
            except Exception:
                values = None
        if values is None:
            values = [getattr(region, name, None)
                      for name in ("left", "top", "width", "height")]
    if values is None or len(values) < 4 or any(value is None for value in values[:4]):
        return ""
    try:
        left, top, width, height = (int(values[0]), int(values[1]),
                                    int(values[2]), int(values[3]))
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0:
        return ""
    return "%d,%d,%d,%d" % (left, top, width, height)


# --------------------------------------------------------------------------- #
# Picture fingerprint
# --------------------------------------------------------------------------- #

def _luminance(b: int, g: int, r: int) -> int:
    """Integer BT.601 luma; the coefficients stay within 8-bit arithmetic."""
    return (b * 29 + g * 150 + r * 77) >> 8


def _fingerprint_image(image: Any, size: int) -> Optional[Tuple[int, ...]]:
    """Sample a numpy-like BGR array (H, W, 3) on a size x size grid."""
    try:
        shape = tuple(getattr(image, "shape", ()) or ())
    except Exception:
        return None
    if len(shape) < 2:
        return None
    height, width = int(shape[0]), int(shape[1])
    if height <= 0 or width <= 0:
        return None
    cells = []
    try:
        for row in range(size):
            y = min(height - 1, ((row * 2 + 1) * height) // (size * 2))
            for column in range(size):
                x = min(width - 1, ((column * 2 + 1) * width) // (size * 2))
                pixel = image[y, x]
                channels = list(pixel)
                if len(channels) >= 3:
                    cells.append(_luminance(int(channels[0]), int(channels[1]),
                                            int(channels[2])))
                elif channels:
                    cells.append(int(channels[0]))
                else:
                    return None
    except Exception:
        logger.debug("frame could not be sampled for the region fingerprint",
                     exc_info=True)
        return None
    return tuple(cells) if len(cells) == size * size else None


def _fingerprint_raw(raw: Any, width: int, height: int, size: int) -> Optional[Tuple[int, ...]]:
    """Sample a tightly packed BGRA byte buffer when numpy is unavailable."""
    if not raw or width <= 0 or height <= 0:
        return None
    stride = width * 4
    if len(raw) < stride * height:
        return None
    cells = []
    try:
        for row in range(size):
            y = min(height - 1, ((row * 2 + 1) * height) // (size * 2))
            base = y * stride
            for column in range(size):
                x = min(width - 1, ((column * 2 + 1) * width) // (size * 2))
                offset = base + x * 4
                cells.append(_luminance(raw[offset], raw[offset + 1], raw[offset + 2]))
    except Exception:
        logger.debug("raw frame could not be sampled for the region fingerprint",
                     exc_info=True)
        return None
    return tuple(cells) if len(cells) == size * size else None


def fingerprint(frame: Any, size: int = FINGERPRINT_SIZE) -> Optional[Tuple[int, ...]]:
    """
    Reduce one captured frame to a ``size * size`` luminance grid.

    ``frame`` may be a ``ScreenFrame`` (its ``image`` is preferred, its raw BGRA
    buffer is the fallback) or a bare image object.  Returns None when nothing
    can be sampled - the caller then treats every frame as changed, which is the
    safe direction: a missing fingerprint must never silence the loop.
    """
    if frame is None or size <= 0:
        return None
    image = getattr(frame, "image", None)
    if image is None and not hasattr(frame, "shape"):
        image = None
    if image is None and hasattr(frame, "shape"):
        image = frame
    if image is not None:
        sampled = _fingerprint_image(image, size)
        if sampled is not None:
            return sampled
        if hasattr(frame, "shape"):
            return None
    raw = getattr(frame, "raw", None)
    if raw is not None:
        return _fingerprint_raw(raw, int(getattr(frame, "width", 0) or 0),
                                int(getattr(frame, "height", 0) or 0), size)
    return None


def is_changed(previous: Optional[Sequence[int]], current: Optional[Sequence[int]],
               tolerance: int = DEFAULT_TOLERANCE) -> bool:
    """
    True when *current* differs from *previous* enough to be worth an OCR pass.

    A missing fingerprint on either side counts as "changed": the first frame of
    a loop always runs, and a frame that could not be sampled is never silently
    dropped.
    """
    if previous is None or current is None:
        return True
    if len(previous) != len(current):
        return True
    try:
        threshold = max(0, int(tolerance))
    except (TypeError, ValueError):
        threshold = DEFAULT_TOLERANCE
    big = 0
    total = 0
    for old, new in zip(previous, current):
        difference = abs(int(old) - int(new))
        total += difference
        if difference > threshold:
            big += 1
            if big >= MIN_CHANGED_CELLS:
                return True
    if not total:
        return False
    return (total / float(len(previous))) > (threshold * 2)


# --------------------------------------------------------------------------- #
# Recognised text comparison
# --------------------------------------------------------------------------- #

def normalize_text(text: Any) -> str:
    """Collapse whitespace and case so cosmetic jitter is not a "change"."""
    if text is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(text)).strip().casefold()


def text_changed(previous: Any, current: Any) -> bool:
    """True when the recognised line differs from the one already pushed."""
    normalized = normalize_text(current)
    if not normalized:
        return False
    return normalized != normalize_text(previous)
