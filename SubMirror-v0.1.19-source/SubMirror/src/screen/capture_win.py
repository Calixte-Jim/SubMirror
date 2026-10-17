# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Screen capture for the OCR chain (official entry).

Scope
    * full-screen snapshot of the PRIMARY monitor (v0.1: main display only;
      multi-monitor is deferred to v0.2)
    * arbitrary region grabs, used by the streaming area OCR

Backends, tried in this order
    mss         fastest; plain GDI/BitBlt under the hood, gets a BGRA buffer
    GDI/ctypes  dependency-free Win32 BitBlt + GetDIBits, always present on
                Windows and needs no Qt event loop
    Qt          QScreen.grabWindow(0, ...); requires a live QGuiApplication

Hard safety red lines honoured here
    * ordinary desktop capture APIs only - BitBlt / GetDIBits / QScreen / mss
    * NO DXGI desktop duplication, NO graphics hooks, NO process injection
    * no capture of individual windows from other processes' memory

Robustness
    Every grab returns None on failure and logs once, so a bad frame is simply
    dropped (spec: capture/OCR exceptions must never crash or block the app).
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The region value object is defined here on purpose: v0.1 has no OCR package
# of its own any more (Windows does the OCR), so the screen layer owns its
# geometry type instead of depending on a module that no longer exists.
@dataclass
class ImageRegion:
    """A rectangle in virtual screen coordinates."""

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    def is_valid(self, minimum: int = 8) -> bool:
        return self.width >= minimum and self.height >= minimum

    def clamp(self, screen_width: int, screen_height: int) -> "ImageRegion":
        """Clip the rectangle to a screen so no capture goes out of bounds."""
        left = max(0, min(int(self.left), int(screen_width)))
        top = max(0, min(int(self.top), int(screen_height)))
        right = max(left, min(self.right, int(screen_width)))
        bottom = max(top, min(self.bottom, int(screen_height)))
        return ImageRegion(left, top, right - left, bottom - top)

    def as_dict(self) -> Dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    @classmethod
    def from_tuple(cls, value: Sequence[int]) -> "ImageRegion":
        items = list(value or [])[:4] + [0, 0, 0, 0]
        return cls(int(items[0]), int(items[1]), int(items[2]), int(items[3]))

try:  # pragma: no cover - optional acceleration
    import numpy as np
    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:  # pragma: no cover - optional fast backend
    import mss as _mss
    _HAS_MSS = True
except Exception:  # pragma: no cover
    _mss = None  # type: ignore[assignment]
    _HAS_MSS = False

__all__ = [
    "ScreenFrame",
    "MonitorInfo",
    "LatestFrameSlot",
    "ScreenCapturer",
    "list_monitors",
    "primary_monitor",
    "FULLSCREEN_MIN_INTERVAL_MS",
]

logger = logging.getLogger("submirror.screen")

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
gdi32 = ctypes.windll.gdi32 if hasattr(ctypes, "windll") else None

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
FULLSCREEN_MIN_INTERVAL_MS = 800          # spec: full-screen OCR trigger floor
AREA_MAX_FPS_SAFE = 0.5
AREA_MAX_FPS_NORMAL = 5.0


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #

@dataclass
class MonitorInfo:
    """One display as reported by the platform."""

    index: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: str = ""
    scale: float = 1.0

    @property
    def region(self) -> ImageRegion:
        return ImageRegion(self.left, self.top, self.width, self.height)

    def as_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "name": self.name, "primary": self.is_primary,
                "left": self.left, "top": self.top, "width": self.width,
                "height": self.height, "scale": self.scale}


@dataclass
class ScreenFrame:
    """
    One captured frame plus everything the OCR layer needs to place results.

    ``image`` is BGR uint8 (H, W, 3) - the convention every OCR backend accepts.
    When numpy is unavailable the raw BGRA bytes are kept instead and ``image``
    is None; the OCR layer then reports "numpy 缺失" instead of crashing.
    """

    image: Any = None
    raw: Optional[bytes] = None
    width: int = 0
    height: int = 0
    region: Optional[ImageRegion] = None
    monitor_index: int = 0
    backend: str = ""
    captured_at: float = field(default_factory=time.monotonic)
    elapsed_ms: float = 0.0

    @property
    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0 and (self.image is not None or self.raw is not None)

    def as_dict(self) -> Dict[str, Any]:
        return {"backend": self.backend, "width": self.width, "height": self.height,
                "monitor": self.monitor_index, "elapsed_ms": round(self.elapsed_ms, 2),
                "region": self.region.as_tuple if isinstance(self.region, ImageRegion) else None}


# --------------------------------------------------------------------------- #
# Monitor enumeration
# --------------------------------------------------------------------------- #

def _qt_monitors() -> List[MonitorInfo]:
    """Enumerate screens through Qt, which understands DPI scaling."""
    try:
        from PySide6.QtGui import QGuiApplication
    except Exception:
        return []
    app = QGuiApplication.instance()
    if app is None:
        return []
    monitors: List[MonitorInfo] = []
    primary = QGuiApplication.primaryScreen()
    for index, screen in enumerate(QGuiApplication.screens()):
        geometry = screen.geometry()
        try:
            scale = float(screen.devicePixelRatio())
        except Exception:
            scale = 1.0
        monitors.append(MonitorInfo(
            index=index, left=geometry.x(), top=geometry.y(),
            width=geometry.width(), height=geometry.height(),
            is_primary=(screen is primary), name=str(screen.name() or ""),
            scale=scale))
    return monitors


def _gdi_primary_monitor() -> Optional[MonitorInfo]:
    """Primary monitor geometry from Win32; None off Windows."""
    if user32 is None:
        return None
    try:
        width = int(user32.GetSystemMetrics(0))     # SM_CXSCREEN
        height = int(user32.GetSystemMetrics(1))    # SM_CYSCREEN
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return MonitorInfo(index=0, left=0, top=0, width=width, height=height,
                       is_primary=True, name="Primary")


def _mss_monitors() -> List[MonitorInfo]:
    """Enumerate through mss (index 0 of its list is the virtual desktop)."""
    if not _HAS_MSS:
        return []
    try:
        with _mss.mss() as sct:
            raw = list(sct.monitors)[1:]           # drop the "all monitors" entry
            monitors = []
            for index, entry in enumerate(raw):
                monitors.append(MonitorInfo(
                    index=index, left=int(entry["left"]), top=int(entry["top"]),
                    width=int(entry["width"]), height=int(entry["height"]),
                    is_primary=(index == 0) or bool(entry.get("is_primary")),
                    name="Monitor %d" % (index + 1)))
            return monitors
    except Exception:
        logger.debug("mss monitor enumeration failed", exc_info=True)
        return []


def list_monitors() -> List[MonitorInfo]:
    """
    Every display, primary first.

    Qt is preferred (DPI aware); mss and GDI then fill the gaps, so the function
    still answers in a headless or dependency-light environment.
    """
    monitors = _qt_monitors()
    if not monitors:
        monitors = _mss_monitors()
    if not monitors:
        fallback = _gdi_primary_monitor()
        monitors = [fallback] if fallback else []
    if not monitors:
        return []
    primary = next((m for m in monitors if m.is_primary), monitors[0])
    if not primary.is_primary:
        primary.is_primary = True
    ordered = [primary] + [m for m in monitors if m is not primary]
    for position, monitor in enumerate(ordered):
        monitor.index = position
    return ordered


def primary_monitor() -> Optional[MonitorInfo]:
    """The main display, or None when nothing could be enumerated."""
    monitors = list_monitors()
    return monitors[0] if monitors else None


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class _MssBackend:
    """mss/GDI backend - the fast path."""

    name = "mss"

    @staticmethod
    def available() -> bool:
        return _HAS_MSS

    def grab(self, region: ImageRegion) -> Optional[ScreenFrame]:
        if not _HAS_MSS:
            return None
        started = time.monotonic()
        with _mss.mss() as sct:
            shot = sct.grab({"left": int(region.left), "top": int(region.top),
                             "width": int(region.width), "height": int(region.height)})
            raw = bytes(shot.raw)
            width, height = int(shot.width), int(shot.height)
        return _make_frame(raw, width, height, region, self.name, started)


class _GdiBackend:
    """
    Win32 BitBlt + GetDIBits backend.

    Kept as an always-available fallback: it needs no third-party package and no
    Qt event loop. Still a plain desktop capture call - no duplication API.
    """

    name = "gdi"

    @staticmethod
    def available() -> bool:
        return user32 is not None and gdi32 is not None

    def grab(self, region: ImageRegion) -> Optional[ScreenFrame]:
        if not self.available():
            return None
        started = time.monotonic()
        width = max(1, int(region.width))
        height = max(1, int(region.height))
        left = int(region.left)
        top = int(region.top)

        screen_dc = user32.GetDC(0)
        if not screen_dc:
            raise RuntimeError("GetDC(0) failed")
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            user32.ReleaseDC(0, screen_dc)
            raise RuntimeError("CreateCompatibleDC failed")
        bitmap = None
        try:
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not bitmap:
                raise RuntimeError("CreateCompatibleBitmap failed")
            gdi32.SelectObject(memory_dc, bitmap)
            if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY):
                raise RuntimeError("BitBlt failed")

            buffer_size = width * height * 4
            buffer = ctypes.create_string_buffer(buffer_size)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            info = BITMAPINFOHEADER()
            info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.biWidth = width
            info.biHeight = -height          # negative => top-down rows
            info.biPlanes = 1
            info.biBitCount = 32
            info.biCompression = 0           # BI_RGB

            copied = gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer,
                                     ctypes.byref(info), DIB_RGB_COLORS)
            if not copied:
                raise RuntimeError("GetDIBits failed")
            return _make_frame(buffer.raw[:buffer_size], width, height,
                               region, self.name, started)
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(0, screen_dc)


class _QtBackend:
    """QScreen.grabWindow(0, ...) - needs a running QGuiApplication."""

    name = "qt"

    @staticmethod
    def available() -> bool:
        try:
            from PySide6.QtGui import QGuiApplication
        except Exception:
            return False
        return QGuiApplication.instance() is not None

    def grab(self, region: ImageRegion) -> Optional[ScreenFrame]:
        from PySide6.QtGui import QGuiApplication, QImage
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        started = time.monotonic()
        pixmap = screen.grabWindow(0, int(region.left), int(region.top),
                                   int(region.width), int(region.height))
        if pixmap is None or pixmap.isNull():
            return None
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        width, height = image.width(), image.height()
        stride = image.bytesPerLine()
        ptr = image.constBits()
        # PySide6 exposes constBits() as a memoryview already sized to the image.
        raw = bytes(ptr) if not isinstance(ptr, memoryview) else ptr.tobytes()
        if len(raw) >= stride * height:
            raw = raw[: stride * height]
        return _make_frame(raw, width, height, region, self.name, started,
                           bytes_per_line=stride)


def _make_frame(raw: bytes, width: int, height: int, region: ImageRegion,
                backend: str, started: float, bytes_per_line: int = 0) -> ScreenFrame:
    """
    Convert a BGRA buffer into a BGR uint8 array.

    Buffer layout is BGRA (32-bit) for all three backends; dropping the alpha
    channel is what the OCR engines expect. Without numpy the raw bytes are kept
    so the caller can still report a diagnosable failure.
    """
    elapsed = (time.monotonic() - started) * 1000.0
    frame = ScreenFrame(raw=raw if not _HAS_NUMPY else None, width=width,
                        height=height, region=region, backend=backend,
                        elapsed_ms=elapsed)
    if not _HAS_NUMPY:
        return frame
    try:
        stride = bytes_per_line or width * 4
        buffer = np.frombuffer(raw, dtype=np.uint8)
        if buffer.size < stride * height:
            raise ValueError("short capture buffer")
        rows = buffer[: stride * height].reshape(height, stride)
        bgra = rows[:, : width * 4].reshape(height, width, 4)
        # BGRA -> BGR, contiguous so OCR engines get a clean array.
        frame.image = np.ascontiguousarray(bgra[:, :, :3])
        frame.raw = None
    except Exception:
        logger.exception("frame conversion failed (backend=%s)", backend)
        frame.image = None
        frame.raw = raw
    return frame


# --------------------------------------------------------------------------- #
# Latest-frame slot (backlog policy)
# --------------------------------------------------------------------------- #

class LatestFrameSlot:
    """
    A one-deep buffer that always keeps the newest frame.

    Spec: an OCR backlog above one frame must drop the OLD frame and process
    only the newest capture. Producers never block; the counter lets the
    diagnostics page show how much backlog was discarded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[ScreenFrame] = None
        self._dropped = 0
        self._stored = 0

    def put(self, frame: ScreenFrame) -> int:
        """Store *frame*, discarding the previous one; returns drops so far."""
        with self._lock:
            if self._frame is not None:
                self._dropped += 1
            self._frame = frame
            self._stored += 1
            return self._dropped

    def take(self) -> Optional[ScreenFrame]:
        """Consume the newest frame (None when empty)."""
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    def peek(self) -> Optional[ScreenFrame]:
        with self._lock:
            return self._frame

    def clear(self) -> None:
        with self._lock:
            self._frame = None

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return self._frame is None

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"stored": self._stored, "dropped": self._dropped,
                    "pending": 0 if self._frame is None else 1}


# --------------------------------------------------------------------------- #
# Capturer
# --------------------------------------------------------------------------- #

class ScreenCapturer:
    """
    Frame source for the OCR chain: primary-monitor snapshots and region grabs.

    Signals are avoided here on purpose - the capturer runs on worker threads and
    hands frames to the caller, which owns the Qt side. That keeps this module
    importable without a QApplication.

    Throttling
        ``try_grab_primary()`` enforces the full-screen trigger floor (800 ms by
        spec). The area-OCR interval follows the FPS-safe-mode setting: 2000 ms
        at 0.5 fps, 200 ms at 5 fps.
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._backend: Optional[Any] = None
        self._backend_name = ""
        self._last_full_grab = 0.0
        self._last_area_grab = 0.0
        self.fullscreen_min_interval_ms = FULLSCREEN_MIN_INTERVAL_MS
        self.area_interval_ms = 200
        self._stats: Dict[str, Any] = {"grabs": 0, "failures": 0, "throttled": 0,
                                       "total_ms": 0.0, "last_ms": 0.0,
                                       "last_backend": ""}
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        """
        Re-read intervals from config.

        FPS safe mode pins the area OCR rate to 0.5 fps and the full-screen floor
        at 800 ms; normal mode allows up to 5 fps for the streaming area OCR.
        """
        config = self._config
        safe_mode = bool(self._get("fps_safe_mode", False))
        try:
            area_fps = float(self._get(
                "ocr.area_fps_safe" if safe_mode else "ocr.area_fps_normal",
                AREA_MAX_FPS_SAFE if safe_mode else AREA_MAX_FPS_NORMAL))
        except (TypeError, ValueError):
            area_fps = AREA_MAX_FPS_SAFE if safe_mode else AREA_MAX_FPS_NORMAL
        area_fps = max(0.1, min(10.0, area_fps))
        try:
            floor_ms = int(self._get("ocr.fullscreen_min_interval_ms",
                                     FULLSCREEN_MIN_INTERVAL_MS))
        except (TypeError, ValueError):
            floor_ms = FULLSCREEN_MIN_INTERVAL_MS
        with self._lock:
            self.area_interval_ms = max(1, int(round(1000.0 / area_fps)))
            self.fullscreen_min_interval_ms = max(100, floor_ms)

    def _get(self, key: str, default: Any) -> Any:
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    # -- backend selection ------------------------------------------------- #

    @property
    def backend_name(self) -> str:
        with self._lock:
            if self._backend is None:
                self._select_backend()
            return self._backend_name

    def _select_backend(self) -> Optional[Any]:
        """Pick the first usable backend; remember it until it fails."""
        with self._lock:
            if self._backend is not None:
                return self._backend
            for candidate in (_MssBackend(), _GdiBackend(), _QtBackend()):
                try:
                    if candidate.available():
                        self._backend = candidate
                        self._backend_name = candidate.name
                        logger.info("screen capture backend: %s", candidate.name)
                        return self._backend
                except Exception:
                    logger.debug("backend %s probe failed", getattr(candidate, "name", "?"),
                                 exc_info=True)
            logger.error("no usable screen capture backend available")
            self._backend = None
            self._backend_name = ""
            return None

    def reset_backend(self) -> None:
        """Force a fresh backend probe (one-click task reset / after failure)."""
        with self._lock:
            self._backend = None
            self._backend_name = ""

    # -- capture ----------------------------------------------------------- #

    def grab_primary(self, force: bool = False) -> Optional[ScreenFrame]:
        """
        Snapshot the primary monitor.

        Throttled to ``fullscreen_min_interval_ms`` unless *force* is set; when
        throttled it returns None and the caller simply skips this trigger.
        """
        if not force and not self._due("full"):
            with self._lock:
                self._stats["throttled"] += 1
            return None
        monitor = primary_monitor()
        if monitor is None:
            self._note_failure("未检测到显示器")
            return None
        return self.grab_region(monitor.region, monitor_index=monitor.index)

    def grab_region(self, region: ImageRegion, monitor_index: int = 0,
                    mark_area: bool = False) -> Optional[ScreenFrame]:
        """
        Capture one region.

        Exceptions are logged and swallowed (spec: a bad frame is dropped, never
        fatal, never blocking). Returns None on any failure.
        """
        try:
            if not isinstance(region, ImageRegion):
                region = ImageRegion.from_tuple(region)
            if not region.is_valid():
                logger.debug("region too small, skipping capture: %r",
                             region.as_tuple if hasattr(region, "as_tuple") else region)
                return None
            if mark_area:
                self._last_area_grab = time.monotonic()

            backend = self._select_backend()
            if backend is None:
                self._note_failure("无可用截图后端")
                return None
            try:
                frame = backend.grab(region)
            except Exception:
                logger.exception("backend %s failed, switching backend", self._backend_name)
                self.reset_backend()
                frame = None
            if frame is None or not frame.is_valid:
                self._note_failure("截图返回空帧")
                return None

            with self._lock:
                self._stats["grabs"] += 1
                self._stats["total_ms"] += float(frame.elapsed_ms)
                self._stats["last_ms"] = float(frame.elapsed_ms)
                self._stats["last_backend"] = frame.backend
            frame.monitor_index = int(monitor_index)
            return frame
        except Exception:
            self._note_failure("截图异常")
            logger.exception("screen capture failed")
            return None

    def try_grab_area(self, region: ImageRegion, monitor_index: int = 0) -> Optional[ScreenFrame]:
        """Area OCR grab honouring the FPS-safe-mode interval."""
        if not self._due("area"):
            with self._lock:
                self._stats["throttled"] += 1
            return None
        return self.grab_region(region, monitor_index=monitor_index, mark_area=True)

    # -- throttling -------------------------------------------------------- #

    def _due(self, kind: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if kind == "full":
                interval = self.fullscreen_min_interval_ms / 1000.0
                return (now - self._last_full_grab) >= interval
            interval = self.area_interval_ms / 1000.0
            return (now - self._last_area_grab) >= interval

    def mark_fullscreen(self) -> None:
        """Record that a full-screen trigger was accepted (called by the OCR task)."""
        with self._lock:
            self._last_full_grab = time.monotonic()

    def remaining_fullscreen_ms(self) -> int:
        """Milliseconds left before the next full-screen trigger is allowed."""
        with self._lock:
            elapsed = (time.monotonic() - self._last_full_grab) * 1000.0
            return int(max(0.0, self.fullscreen_min_interval_ms - elapsed))

    def remaining_area_ms(self) -> int:
        with self._lock:
            elapsed = (time.monotonic() - self._last_area_grab) * 1000.0
            return int(max(0.0, self.area_interval_ms - elapsed))

    # -- diagnostics ------------------------------------------------------- #

    def _note_failure(self, reason: str) -> None:
        with self._lock:
            self._stats["failures"] += 1
            failures = self._stats["failures"]
        # Log the first failures loudly, then throttle to every 50th.
        if failures <= 3 or failures % 50 == 0:
            logger.warning("screen capture failure #%d: %s", failures, reason)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._stats)
        grabs = max(1, int(data.get("grabs", 0)))
        data["avg_ms"] = round(float(data.get("total_ms", 0.0)) / grabs, 2)
        data["backend"] = self._backend_name
        data["area_interval_ms"] = self.area_interval_ms
        data["fullscreen_floor_ms"] = self.fullscreen_min_interval_ms
        return data
