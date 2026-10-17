# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Floating subtitle overlay (official entry 14).

Layout (spec: three rows)
    row 1  ASR  - always visible while the audio pipeline produces subtitles
    row 2  TTS  - RESERVED placeholder, hidden by default (no TTS playback in
                  v0.1); it renders the configured placeholder string only
    row 3  OCR  - the region/full-screen OCR line, shown while the area OCR
                  hotkey is held

Window behaviour
    * a normal frameless translucent top-level window: no DLL injection, no
      external overlay process, no hooks of any kind
    * always-on-top + mouse click-through, or draggable when click-through is
      off; the Windows WS_EX_TRANSPARENT behaviour comes from the plain Qt flag
      ``Qt.WindowTransparentForInput``
    * ``WA_ShowWithoutActivating`` so showing the overlay never steals the
      keyboard focus from the game
    * dragging persists overlay.x / overlay.y into config.json

Robustness
    * every paint() call is wrapped: a drawing failure is logged and swallowed so
      Qt's event loop is never taken down
    * the font size shrinks step by step (down to min_font_size) until all three
      blocks fit inside the mask; the queue already truncated the text to 200
      characters, so nothing overflows the window
    * the "translation service unavailable" message uses a separate notice
      banner, so the reserved TTS row stays a placeholder

Threading
    SubtitleManager / translator signals are connected to slots on this QObject;
    Qt queues them onto the GUI thread. No worker thread ever touches a widget.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - optional dependency
    from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication,
                               QPainter, QPainterPath)
    from PySide6.QtWidgets import QWidget
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - headless / Qt missing
    _QT_AVAILABLE = False

    class _FallbackSignal:
        """Minimal stand-in so the module imports without PySide6."""

        def __init__(self, *args, **kwargs) -> None:
            self._slots: List[Any] = []

        def connect(self, slot) -> None:
            if callable(slot):
                self._slots.append(slot)

        def disconnect(self, slot=None) -> None:
            if slot is None:
                self._slots = []
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args, **kwargs) -> None:
            for slot in list(self._slots):
                try:
                    slot(*args, **kwargs)
                except Exception:
                    logging.getLogger("submirror.ui.overlay").exception(
                        "fallback signal slot failed")

    def Signal(*args, **kwargs):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            pass

    class QWidget:  # type: ignore[no-redef]
        pass

    QRect = QRectF = QTimer = Qt = QColor = QFont = QFontMetrics = None  # type: ignore
    QGuiApplication = QPainter = QPainterPath = None  # type: ignore

__all__ = [
    "STATUS_OK",
    "STATUS_BUSY",
    "STATUS_ERROR",
    "STATUS_UNKNOWN",
    "OverlaySettings",
    "SubtitleOverlay",
]

logger = logging.getLogger("submirror.ui.overlay")

STATUS_OK = "ok"              # green  - audio pipeline healthy
STATUS_BUSY = "busy"          # yellow - recognised speech is being processed
STATUS_ERROR = "error"        # red    - capture/ASR failure
STATUS_UNKNOWN = "unknown"    # grey   - not started yet

STATUS_COLORS = {
    STATUS_OK: "#35C759",
    STATUS_BUSY: "#FFCC00",
    STATUS_ERROR: "#FF3B30",
    STATUS_UNKNOWN: "#9AA0A6",
}

DEFAULT_BOTTOM_MARGIN = 140
MIN_VISIBLE_PX = 48            # keep at least this much of the window on screen
STATUS_FONT_SIZE = 10
NOTICE_FONT_SIZE = 10
MAX_VISUAL_LINES = 3           # per subtitle item, before clipping

# Panel size presets (item 3): name -> (width, height, corner_radius). The
# "default" entry is also the reference width used by
# ``OverlaySettings.effective_font_size`` when the text follows the panel.
SIZE_PRESETS: Dict[str, Tuple[int, int, int]] = {
    "default": (900, 220, 14),   # 默认圆角长方形
    "compact": (660, 120, 10),   # 紧凑窄长矩形
    "square": (600, 600, 12),    # 正方形
}
DEFAULT_SIZE_PRESET = "default"


def preset_geometry(name: str) -> Tuple[int, int, int]:
    """(width, height, corner_radius) of one panel preset, default fallback."""
    return SIZE_PRESETS.get(str(name or "").strip().lower(),
                            SIZE_PRESETS[DEFAULT_SIZE_PRESET])


def outline_offsets(width: int) -> Tuple[Tuple[int, int], ...]:
    """
    Pixel offsets that fake a text outline of ``width`` pixels.

    The outline is a cheap visual weight: the glyph is drawn again around
    itself, one ring of +-r offsets per pixel of thickness. A width of 1 (the
    long-standing default) produces exactly the original four offsets.
    """
    radius = max(1, min(8, int(width)))
    offsets: List[Tuple[int, int]] = []
    for step in range(1, radius + 1):
        offsets.extend(((-step, 0), (step, 0), (0, -step), (0, step)))
    return tuple(offsets)

# QQ-Music-style subtitle transition. The fade is pure-Python state advanced by
# a GUI-thread QTimer; no thread ever touches the widget.
FADE_TICK_MS = 16              # ~60 fps: smooth enough, cheap enough
FADE_CHANNELS = ("asr", "tts", "ocr")

# -- performance guards ----------------------------------------------------- #
# The fade timer is the only thing that repaints the overlay on its own, so its
# interval is what caps the refresh rate. A fullscreen foreground window (a
# game) means the GPU is already busy, so the animation is slowed down there.
# The Windows probe is user-mode only (GetForegroundWindow/GetWindowRect) and
# cached, so a paint-heavy loop never calls into the API per frame.
FULLSCREEN_PROBE_CACHE_S = 0.5

_fullscreen_cache: Tuple[float, bool] = (0.0, False)

try:  # pragma: no cover - platform guard
    import ctypes
    from ctypes import wintypes

    _user32_probe = ctypes.windll.user32
    _IS_WINDOWS_PROBE = True
except Exception:  # pragma: no cover - non-Windows or a hardened Python
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    _user32_probe = None
    _IS_WINDOWS_PROBE = False


def _foreground_is_fullscreen_uncached() -> bool:
    """True when the foreground window covers its monitor (user-mode probe)."""
    if not _IS_WINDOWS_PROBE or _user32_probe is None:
        return False
    try:
        hwnd = _user32_probe.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not _user32_probe.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return False
        # MONITOR_DEFAULTTONEAREST = 2
        monitor = _user32_probe.MonitorFromWindow(hwnd, 2)
        if not monitor:
            return False

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32_probe.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        mon_w = int(info.rcMonitor.right - info.rcMonitor.left)
        mon_h = int(info.rcMonitor.bottom - info.rcMonitor.top)
        if mon_w <= 0 or mon_h <= 0:
            return False
        # 98% allows for a borderless-windowed fullscreen game.
        return width >= int(mon_w * 0.98) and height >= int(mon_h * 0.98)
    except Exception:
        return False


def foreground_is_fullscreen(max_age_s: float = FULLSCREEN_PROBE_CACHE_S) -> bool:
    """
    Cached "is a fullscreen window in front?" probe.

    ``max_age_s`` is intentionally a parameter so a headless test can patch this
    function (or pass 0.0) and never touch the Windows API.
    """
    global _fullscreen_cache
    now = time.monotonic()
    cached = _fullscreen_cache
    if cached:
        stamp, value = cached
        if stamp and (now - stamp) < max(0.0, float(max_age_s)):
            return bool(value)
    value = bool(_foreground_is_fullscreen_uncached())
    _fullscreen_cache = (now, value)
    return value

_ZH_TEXT = {
    "status_ok": "音频正常",
    "status_busy": "处理中",
    "status_error": "音频异常",
    "status_unknown": "未启动",
    "tts_placeholder": "TTS（v0.2 预留）",
    "notice_translate_unavailable": "翻译服务不可用",
    "notice_ocr_unavailable": "屏幕 OCR 不可用",
    "notice_audio_unavailable": "音频捕获不可用",
    "notice_click_through_on": "已开启鼠标穿透",
    "notice_click_through_off": "已关闭鼠标穿透，可拖动窗口",
}
_EN_TEXT = {
    "status_ok": "Audio OK",
    "status_busy": "Processing",
    "status_error": "Audio error",
    "status_unknown": "Not started",
    "tts_placeholder": "TTS (reserved for v0.2)",
    "notice_translate_unavailable": "Translation service unavailable",
    "notice_ocr_unavailable": "Screen OCR unavailable",
    "notice_audio_unavailable": "Audio capture unavailable",
    "notice_click_through_on": "Click-through enabled",
    "notice_click_through_off": "Click-through disabled - the window can be dragged",
}


def _overlay_texts(config: Any) -> Dict[str, str]:
    """UI strings for the overlay; only the UI language is consulted."""
    language = "zh-CN"
    if config is not None:
        try:
            language = str(config.get("ui_language", language) or language)
        except Exception:
            pass
    table = dict(_EN_TEXT) if language.lower().startswith("en") else dict(_ZH_TEXT)
    translator = getattr(config, "t", None) if config is not None else None
    if callable(translator):
        for key in list(table.keys()):
            lang_key = "overlay." + key
            try:
                value = translator(lang_key)
            except Exception:
                continue
            if value and value != lang_key:
                table[key] = value
    return table


def _qcolor(value: Any, default) -> Any:
    """Parse '#RGB' / '#RRGGBB' / '#AARRGGBB'; fall back to a default QColor."""
    if not _QT_AVAILABLE:
        return None
    if value is None:
        return default
    text = str(value).strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 8:                      # AARRGGBB -> RRGGBB + alpha
        try:
            alpha = int(text[0:2], 16)
            color = QColor(int(text[2:4], 16), int(text[4:6], 16), int(text[6:8], 16))
            color.setAlpha(alpha)
            return color
        except Exception:
            return default
    color = QColor("#" + text) if text else None
    return color if color is not None and color.isValid() else default


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@dataclass
class OverlaySettings:
    """Everything the overlay draws with, read from config.json."""

    font_family: str = "Microsoft YaHei"
    font_size: int = 22
    min_font_size: int = 12
    text_opacity: float = 1.0
    mask_opacity: float = 0.35
    text_color: str = "#FFFFFF"
    mask_color: str = "#000000"
    outline_color: str = "#101010"
    outline: bool = True
    always_on_top: bool = True
    click_through: bool = True
    lock_position: bool = False
    show_original: bool = False
    show_tts_line: bool = False
    show_status_indicator: bool = True
    show_status_text: bool = False
    status_follows_translate: bool = False
    width: int = 900
    height: int = 220
    x: int = -1
    y: int = -1
    corner_radius: int = 14
    padding: int = 18
    line_spacing: int = 4
    max_lines_per_channel: int = 2
    notice_seconds: float = 6.0
    # Panel size preset (item 3). The preset is a shortcut that writes
    # width/height/corner_radius; the geometry itself always comes from the
    # individual keys, so a hand-edited config is never overridden at runtime.
    size_preset: str = "default"
    # "text follows the panel": scale the glyph size with the panel width.
    font_scale_with_panel: bool = False
    # Outline thickness in pixels; 0 turns the outline off entirely.
    outline_width: int = 2
    # Fade animation (both 0 disables the transition entirely).
    fade_in_ms: int = 180
    fade_out_ms: int = 220
    fade_slide_px: int = 6
    # Performance (item 2): the fade timer repaints the widget, so it is the
    # knob that caps the refresh rate. While a fullscreen game is in front the
    # animation is slowed down further to leave the GPU alone.
    max_repaint_fps: int = 30
    fade_fullscreen_slowdown: bool = True
    fade_fullscreen_scale: float = 0.5
    # v0.1.18 item 5: the fade timer is also the only periodic repaint of the
    # subtitle widget, so its interval is the "minimum refresh time" of a line.
    # 400 ms keeps a busy stream from repainting dozens of times per second
    # while still animating smoothly enough for text.
    min_repaint_interval_ms: int = 400

    def frame_interval_ms(self, fullscreen: bool = False) -> int:
        """
        Timer interval of the fade animation, capped by ``max_repaint_fps``.

        ``fullscreen`` doubles the interval (``fade_fullscreen_scale``) so a game
        in the background is not fighting the overlay for the GPU. The floor is
        4 ms, i.e. an effective ceiling of 250 fps for a bizarre setting. This is
        the *animation* tick: a static line is never repainted, because
        ``_on_fade_tick`` skips ``update()`` while nothing changes.
        """
        fps = max(1, int(self.max_repaint_fps))
        interval = 1000.0 / float(fps)
        if fullscreen and self.fade_fullscreen_slowdown:
            scale = min(1.0, max(0.1, float(self.fade_fullscreen_scale)))
            interval = interval / scale
        return int(max(4, round(interval)))

    def repaint_interval_ms(self, fullscreen: bool = False) -> int:
        """
        Shortest spacing between two *content* repaints (v0.1.18 item 5).

        A worker thread pushes a new line as soon as it exists; this floor keeps
        a burst of updates ("字幕最少 400ms 刷新") from turning into a burst of
        paints, which is where the GPU time actually goes. It never shortens the
        fade animation, only coalesces content updates.
        """
        try:
            minimum = max(0, int(self.min_repaint_interval_ms))
        except (TypeError, ValueError):
            minimum = 0
        return int(max(self.frame_interval_ms(fullscreen), minimum))

    @property
    def effective_font_size(self) -> int:
        """
        Base glyph size actually used for layout.

        With ``subtitle.font_scale_with_panel`` on, the size follows the panel
        width relative to the reference preset (``SIZE_PRESETS["default"]``),
        which keeps the text-to-box ratio when the panel is resized by hand.
        """
        if not self.font_scale_with_panel:
            return int(self.font_size)
        reference = float(SIZE_PRESETS["default"][0] or 900)
        scaled = float(self.font_size) * (float(max(1, self.width)) / reference)
        return int(max(self.min_font_size, min(96, round(scaled))))

    @classmethod
    def from_config(cls, config: Any = None) -> "OverlaySettings":
        data = cls()

        def read(key: str, default: Any) -> Any:
            if config is None:
                return default
            try:
                value = config.get(key, default)
            except Exception:
                return default
            return default if value is None else value

        def as_int(key: str, default: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(read(key, default))))
            except (TypeError, ValueError):
                return default

        def as_float(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(read(key, default))))
            except (TypeError, ValueError):
                return default

        data.font_family = str(read("subtitle.font_family", "Microsoft YaHei")
                               or "Microsoft YaHei")
        data.font_size = as_int("subtitle.font_size", 22, 10, 96)
        data.text_opacity = as_float("subtitle.text_opacity", 1.0, 0.05, 1.0)
        data.mask_opacity = as_float("subtitle.mask_opacity", 0.35, 0.0, 1.0)
        data.text_color = str(read("overlay.text_color", "#FFFFFF"))
        data.mask_color = str(read("overlay.mask_color", "#000000"))
        data.outline_color = str(read("overlay.outline_color", "#101010"))
        data.outline_width = as_int("overlay.outline_width", 2, 0, 8)
        # A zero thickness is the same thing as switching the outline off.
        data.outline = bool(read("overlay.outline", True)) and data.outline_width > 0
        data.always_on_top = bool(read("overlay.always_on_top", True))
        data.click_through = bool(read("overlay.click_through", True))
        data.lock_position = bool(read("overlay.lock_position", False))
        # "show original" lives on the subtitle page, translate.* is the alias.
        data.show_original = bool(read("subtitle.show_original",
                                      read("translate.show_original", False)))
        data.show_tts_line = bool(read("subtitle.show_tts_line", False))
        data.show_status_indicator = bool(read("overlay.show_status", True))
        data.show_status_text = bool(read("overlay.show_status_text", False))
        data.status_follows_translate = bool(
            read("overlay.status_follows_translate", False))
        data.width = as_int("overlay.width", 900, 320, 4096)
        data.height = as_int("overlay.height", 220, 80, 1024)
        data.x = as_int("overlay.x", -1, -1, 20000)
        data.y = as_int("overlay.y", -1, -1, 20000)
        data.corner_radius = as_int("overlay.corner_radius", 14, 0, 48)
        data.padding = as_int("overlay.padding", 18, 4, 64)
        data.line_spacing = as_int("overlay.line_spacing", 4, 0, 32)
        data.max_lines_per_channel = as_int("subtitle.max_lines_per_channel", 2, 1, 4)
        data.notice_seconds = as_float("overlay.notice_seconds", 6.0, 1.0, 60.0)
        data.fade_in_ms = as_int("subtitle.fade_in_ms", 180, 0, 2000)
        data.fade_out_ms = as_int("subtitle.fade_out_ms", 220, 0, 2000)
        data.fade_slide_px = as_int("subtitle.fade_slide_px", 6, 0, 64)
        preset = str(read("overlay.size_preset", DEFAULT_SIZE_PRESET)
                     or DEFAULT_SIZE_PRESET).strip().lower()
        data.size_preset = (preset if preset in SIZE_PRESETS
                            else DEFAULT_SIZE_PRESET)
        data.font_scale_with_panel = bool(
            read("subtitle.font_scale_with_panel", False))
        # Performance guards (perf.*): the fade timer is the only repaint source
        # that runs on its own, so its interval is what caps the frame rate.
        data.max_repaint_fps = as_int("perf.overlay_max_fps", 30, 1, 240)
        data.min_repaint_interval_ms = as_int("perf.overlay_min_interval_ms",
                                              400, 0, 5000)
        data.fade_fullscreen_slowdown = bool(
            read("perf.overlay_fullscreen_slowdown", True))
        data.fade_fullscreen_scale = as_float(
            "perf.overlay_fullscreen_scale", 0.5, 0.1, 1.0)
        return data

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- #
# Fade state
# --------------------------------------------------------------------------- #

class _ChannelFade:
    """
    Opacity state for one subtitle channel (``asr`` / ``tts`` / ``ocr``).

    Two layers are tracked so a new line cross-fades with the previous one:
    ``lines`` fades in from 0 to 1 over ``subtitle.fade_in_ms`` when it appears
    (or changes), and the old ``out_lines`` fades 1 to 0 over
    ``subtitle.fade_out_ms``. All methods are pure Python: they work whether or
    not PySide6 is installed, which keeps the surface testable headless.
    """

    __slots__ = ("lines", "alpha", "out_lines", "out_alpha")

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.alpha: float = 0.0
        self.out_lines: List[str] = []
        self.out_alpha: float = 0.0

    def set_lines(self, lines: Sequence[str], fade_in_ms: int,
                  fade_out_ms: int) -> bool:
        """Adopt a new line set; returns True when the content actually changed."""
        new_lines = [str(item) for item in (lines or []) if str(item).strip()]
        if new_lines == self.lines:
            return False
        # The visible layer becomes the outgoing layer so it can fade away.
        if self.lines and self.alpha > 0.0 and fade_out_ms > 0:
            self.out_lines = list(self.lines)
            self.out_alpha = self.alpha
        elif not (self.out_lines and self.out_alpha > 0.0 and fade_out_ms > 0):
            # No live outgoing fade: drop it rather than reusing a stale one.
            self.out_lines = []
            self.out_alpha = 0.0
        self.lines = new_lines
        # With fade-in disabled the new line is simply on screen at once.
        self.alpha = 0.0 if (new_lines and fade_in_ms > 0) else \
            (1.0 if new_lines else 0.0)
        return True

    def settle(self, fade_in_ms: int, fade_out_ms: int) -> None:
        """Apply a config change mid-flight without restarting the animation."""
        if fade_in_ms <= 0:
            self.alpha = 1.0 if self.lines else 0.0
        if fade_out_ms <= 0:
            self.out_lines = []
            self.out_alpha = 0.0

    def advance(self, dt_ms: float, fade_in_ms: int, fade_out_ms: int) -> None:
        """Advance both layers by ``dt_ms`` milliseconds."""
        if dt_ms <= 0:
            return
        if self.lines:
            if fade_in_ms > 0:
                self.alpha = min(1.0, self.alpha + float(dt_ms) / fade_in_ms)
            else:
                self.alpha = 1.0
        else:
            self.alpha = 0.0
        if self.out_lines:
            if fade_out_ms > 0:
                self.out_alpha -= float(dt_ms) / fade_out_ms
                if self.out_alpha <= 0.0:
                    self.out_alpha = 0.0
                    self.out_lines = []
            else:
                self.out_alpha = 0.0
                self.out_lines = []

    def running(self) -> bool:
        """True while either layer is still moving."""
        if self.lines and self.alpha < 1.0:
            return True
        return bool(self.out_lines and self.out_alpha > 0.0)

    def visible_lines(self) -> List[str]:
        """The lines the channel shows right now (current, else outgoing)."""
        return list(self.lines) if self.lines else list(self.out_lines)

    def visible_alpha(self) -> float:
        """Opacity of the line the channel is currently showing."""
        alpha = self.alpha if self.lines else self.out_alpha
        return float(max(0.0, min(1.0, alpha)))


# --------------------------------------------------------------------------- #
# Overlay widget
# --------------------------------------------------------------------------- #

class SubtitleOverlay(QWidget if _QT_AVAILABLE else object):  # type: ignore[misc]
    """
    Translucent, click-through floating subtitle window.

    Usable with a SubtitleManager (``attach_subtitle_manager``) or stand-alone by
    pushing lines through ``set_asr_lines`` / ``set_ocr_lines``.
    """

    geometry_changed = Signal(int, int)
    visibility_changed = Signal(bool)
    closed = Signal()
    failed = Signal(str)

    def __init__(self, config: Any = None, subtitle_manager: Any = None,
                 parent=None) -> None:
        if _QT_AVAILABLE:
            super().__init__(parent)
        else:                                             # pragma: no cover
            super().__init__()
        self._config = config
        self._manager: Any = None
        self._settings = OverlaySettings.from_config(config)
        self._texts = _overlay_texts(config)
        self._status = STATUS_UNKNOWN
        self._status_message = ""
        self._notice = ""
        self._notice_deadline = 0.0
        self._tts_text = ""
        self._asr_lines: List[str] = []
        self._ocr_lines: List[str] = []
        self._dragging = False
        self._drag_offset = None
        self._closed = False
        self._last_font_size = self._settings.effective_font_size
        self._notice_timer: Any = None
        # Per-channel fade state and its GUI-thread animation timer.
        self._fades: Dict[str, _ChannelFade] = {
            kind: _ChannelFade() for kind in FADE_CHANNELS}
        self._fade_timer: Any = None
        self._last_fade_ts = 0.0
        # Repaint accounting: the counters make the frame cap measurable from
        # the diagnostics dump instead of being a claim in a comment.
        self._paint_count = 0
        self._paint_skips = 0
        self._last_paint_ts = 0.0
        self._last_signature: Tuple[Any, ...] = ()
        self._fullscreen_active = False

        if not _QT_AVAILABLE:
            logger.error("subtitle overlay unavailable: PySide6 is not installed")
            self.failed.emit("qt-unavailable")
            return

        self.setWindowTitle("SubMirror Overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # Never steal the keyboard focus from the game.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._on_notice_timeout)
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(self._settings.frame_interval_ms(False))
        self._fade_timer.timeout.connect(self._on_fade_tick)

        self._apply_window_flags()
        self._apply_geometry()
        if subtitle_manager is not None:
            self.attach_subtitle_manager(subtitle_manager)

    # -- configuration ----------------------------------------------------- #

    @property
    def is_available(self) -> bool:
        return _QT_AVAILABLE

    @property
    def settings(self) -> OverlaySettings:
        return self._settings

    def apply_config(self, config: Any = None) -> None:
        """Re-read config.json; safe while the overlay is visible."""
        if config is not None:
            self._config = config
        self._settings = OverlaySettings.from_config(self._config)
        self._texts = _overlay_texts(self._config)
        if not _QT_AVAILABLE:
            return
        self._apply_window_flags()
        self._apply_geometry()
        self._apply_fade_interval(self._fullscreen_now())
        self._settle_fades()
        self.refresh()

    def _apply_window_flags(self) -> None:
        if not _QT_AVAILABLE:
            return
        try:
            flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
            if self._settings.always_on_top:
                flags |= Qt.WindowType.WindowStaysOnTopHint
            if self._settings.click_through:
                flags |= Qt.WindowType.WindowTransparentForInput
            was_visible = self.isVisible()
            self.setWindowFlags(flags)
            # Belt and braces: the widget attribute covers the same behaviour.
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                              bool(self._settings.click_through))
            if was_visible:
                self.show()          # Qt requires a re-show for flag changes
        except Exception:
            logger.exception("could not apply overlay window flags")

    def _primary_geometry(self) -> Any:
        try:
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                return screen.availableGeometry()
        except Exception:
            logger.debug("primary screen geometry unavailable", exc_info=True)
        return None

    def _default_position(self) -> Tuple[int, int]:
        geometry = self._primary_geometry()
        if geometry is None:
            return (100, 400)
        x = geometry.x() + (geometry.width() - self._settings.width) // 2
        y = geometry.y() + geometry.height() - self._settings.height - \
            DEFAULT_BOTTOM_MARGIN
        return (int(x), int(max(geometry.y(), y)))

    def _clamp_to_screen(self, x: int, y: int, width: int, height: int) -> Tuple[int, int]:
        geometry = self._primary_geometry()
        if geometry is None:
            return (int(x), int(y))
        left, top = geometry.x(), geometry.y()
        right, bottom = left + geometry.width(), top + geometry.height()
        x = max(left - width + MIN_VISIBLE_PX,
                min(int(x), right - MIN_VISIBLE_PX))
        y = max(top, min(int(y), bottom - MIN_VISIBLE_PX))
        return (int(x), int(y))

    def _apply_geometry(self) -> None:
        if not _QT_AVAILABLE:
            return
        try:
            x, y = self._settings.x, self._settings.y
            if x < 0 or y < 0:
                x, y = self._default_position()
            x, y = self._clamp_to_screen(x, y, self._settings.width,
                                         self._settings.height)
            self.setGeometry(x, y, self._settings.width, self._settings.height)
        except Exception:
            logger.exception("could not apply overlay geometry")

    def reset_position(self) -> None:
        """Move back to the default spot and persist it."""
        if not _QT_AVAILABLE:
            return
        x, y = self._default_position()
        self._settings.x, self._settings.y = x, y
        self.setGeometry(x, y, self._settings.width, self._settings.height)
        self._persist_geometry()
        self.refresh()

    def _persist_geometry(self) -> None:
        """Store the dragged position in config.json (best effort)."""
        if self._config is None or not _QT_AVAILABLE:
            return
        try:
            top_left = self.frameGeometry().topLeft()
            self._settings.x, self._settings.y = top_left.x(), top_left.y()
            for key, value in (("overlay.x", top_left.x()),
                               ("overlay.y", top_left.y()),
                               ("overlay.width", self.width()),
                               ("overlay.height", self.height())):
                try:
                    self._config.set(key, int(value), save=False)
                except TypeError:
                    self._config.set(key, int(value))
                except Exception:
                    logger.debug("config.set(%s) failed", key, exc_info=True)
            saver = getattr(self._config, "save", None)
            if callable(saver):
                saver()
        except Exception:
            logger.debug("could not persist overlay geometry", exc_info=True)

    # -- subtitle sources -------------------------------------------------- #

    def attach_subtitle_manager(self, manager: Any) -> None:
        """Connect SubtitleManager.asr_changed / ocr_changed to this overlay."""
        self.detach_subtitle_manager()
        self._manager = manager
        if manager is None:
            return
        for name in ("asr_changed", "ocr_changed"):
            signal = getattr(manager, name, None)
            connect = getattr(signal, "connect", None)
            if callable(connect):
                try:
                    connect(self.on_subtitles_changed)
                except Exception:
                    logger.exception("could not connect %s", name)
        self.refresh()

    def detach_subtitle_manager(self) -> None:
        manager = self._manager
        self._manager = None
        if manager is None:
            return
        for name in ("asr_changed", "ocr_changed"):
            signal = getattr(manager, name, None)
            disconnect = getattr(signal, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect(self.on_subtitles_changed)
                except Exception:
                    logger.debug("could not disconnect %s", name, exc_info=True)

    def set_asr_lines(self, lines: Optional[Sequence[str]]) -> None:
        """Push ASR text directly (used when no SubtitleManager is attached)."""
        self._asr_lines = [str(item) for item in (lines or []) if str(item).strip()]
        self.refresh()

    def set_ocr_lines(self, lines: Optional[Sequence[str]]) -> None:
        self._ocr_lines = [str(item) for item in (lines or []) if str(item).strip()]
        self.refresh()

    def set_tts_text(self, text: Any) -> None:
        """TTS row content - a pure placeholder in v0.1 (no playback exists)."""
        self._tts_text = str(text or "")
        self.refresh()

    def _snapshot(self) -> Tuple[List[str], List[str]]:
        if self._manager is not None:
            try:
                asr, ocr = self._manager.display_texts(self._settings.show_original)
                return [str(x) for x in asr], [str(x) for x in ocr]
            except Exception:
                logger.debug("subtitle snapshot failed", exc_info=True)
        return list(self._asr_lines), list(self._ocr_lines)

    # -- state ------------------------------------------------------------- #

    def set_status(self, state: str, message: str = "") -> None:
        """Set the red/yellow/green indicator (green=ok, yellow=busy, red=error)."""
        if state not in STATUS_COLORS:
            state = STATUS_UNKNOWN
        self._status = state
        self._status_message = str(message or "")
        self.refresh()

    def set_notice(self, text: Any, timeout_s: Optional[float] = None) -> None:
        """Show a transient banner (e.g. 'translation service unavailable')."""
        self._notice = str(text or "")
        seconds = self._settings.notice_seconds if timeout_s is None \
            else max(0.5, float(timeout_s))
        self._notice_deadline = time.monotonic() + seconds
        if _QT_AVAILABLE and self._notice_timer is not None:
            try:
                self._notice_timer.start(int(seconds * 1000))
            except Exception:
                logger.debug("notice timer start failed", exc_info=True)
        self.refresh()

    def clear_notice(self) -> None:
        self._notice = ""
        self._notice_deadline = 0.0
        self.refresh()

    def _on_notice_timeout(self) -> None:
        self._notice = ""
        self.refresh()

    def toggle_click_through(self) -> bool:
        """Flip mouse click-through; persists overlay.click_through."""
        self._settings.click_through = not self._settings.click_through
        if self._config is not None:
            try:
                self._config.set("overlay.click_through",
                                 bool(self._settings.click_through), save=True)
            except TypeError:
                try:
                    self._config.set("overlay.click_through",
                                     bool(self._settings.click_through))
                    self._config.save()
                except Exception:
                    logger.debug("could not persist click-through", exc_info=True)
            except Exception:
                logger.debug("could not persist click-through", exc_info=True)
        self._apply_window_flags()
        self.set_notice(self._texts["notice_click_through_on"
                                    if self._settings.click_through
                                    else "notice_click_through_off"])
        return self._settings.click_through

    @property
    def is_click_through(self) -> bool:
        return bool(self._settings.click_through)

    @property
    def status(self) -> str:
        return self._status

    # -- slots (safe to call from queued connections) ----------------------- #

    def on_subtitles_changed(self) -> None:
        self.refresh()

    def on_status_changed(self, state: str, message: str = "") -> None:
        self.set_status(state, message)

    def on_notice(self, text: str, timeout_s: Optional[float] = None) -> None:
        if text:
            self.set_notice(text, timeout_s)
        else:
            self.clear_notice()

    def on_translate_state(self, state: str, message: str = "") -> None:
        """
        React to translator state ('ok' / 'failed' / 'circuit-open' / ...).

        The message goes to the notice banner; the indicator only turns red when
        overlay.status_follows_translate is enabled.
        """
        state = str(state or "").lower()
        if state in ("failure", "failed", "unavailable", "circuit-open", "error"):
            self.set_notice(self._texts["notice_translate_unavailable"])
            if self._settings.status_follows_translate:
                self.set_status(STATUS_ERROR, message)
        elif state in ("ok", "success", "recovered") and self._settings.\
                status_follows_translate:
            self.set_status(STATUS_OK, message)

    def on_audio_state(self, state: str, message: str = "") -> None:
        """Map audio capture states onto the indicator."""
        mapping = {"running": STATUS_OK, "started": STATUS_OK, "ok": STATUS_OK,
                   "busy": STATUS_BUSY, "processing": STATUS_BUSY,
                   "reconnecting": STATUS_BUSY, "restarting": STATUS_BUSY,
                   "error": STATUS_ERROR, "failed": STATUS_ERROR,
                   "stopped": STATUS_UNKNOWN, "unavailable": STATUS_ERROR}
        if state in (STATUS_OK, STATUS_BUSY, STATUS_ERROR, STATUS_UNKNOWN):
            self.set_status(state, message)
        else:
            self.set_status(mapping.get(str(state or "").lower(), STATUS_UNKNOWN),
                            message)
        if self._status == STATUS_ERROR:
            self.set_notice(self._texts["notice_audio_unavailable"])

    # -- subtitle transition (fade in / out) ------------------------------- #

    def _sync_fades(self) -> None:
        """Push the current channel text into the fade state."""
        if not self._fades:
            return
        settings = self._settings
        asr_lines, ocr_lines = self._snapshot()
        tts_lines: List[str] = []
        if settings.show_tts_line:
            tts_lines = [self._tts_text or self._texts.get("tts_placeholder", "")]
        wanted = {"asr": list(asr_lines), "tts": tts_lines, "ocr": list(ocr_lines)}
        changed = False
        for kind, fade in self._fades.items():
            fade.settle(settings.fade_in_ms, settings.fade_out_ms)
            if fade.set_lines(wanted.get(kind, []), settings.fade_in_ms,
                              settings.fade_out_ms):
                changed = True
        if changed or self._fade_running():
            self._ensure_fade_timer()

    def _settle_fades(self) -> None:
        """Apply the current durations without restarting a running fade."""
        settings = self._settings
        for fade in self._fades.values():
            fade.settle(settings.fade_in_ms, settings.fade_out_ms)
        if not self._fade_running():
            self._stop_fade_timer()

    def _fade_running(self) -> bool:
        return any(fade.running() for fade in self._fades.values())

    def _fullscreen_now(self) -> bool:
        """
        Whether a fullscreen window is in front, throttled by the probe cache.

        Cached deliberately: ``_on_fade_tick`` runs up to ``max_repaint_fps``
        times per second and must never do a Win32 round trip per frame.
        """
        self._fullscreen_active = bool(foreground_is_fullscreen())
        return self._fullscreen_active

    def _apply_fade_interval(self, fullscreen: bool = False) -> int:
        """Bring the fade timer in line with the current frame cap."""
        interval = self._settings.frame_interval_ms(bool(fullscreen))
        timer = self._fade_timer
        if timer is None:
            return interval
        try:
            if int(timer.interval()) != interval:
                timer.setInterval(interval)
        except Exception:
            logger.debug("fade timer interval could not be set", exc_info=True)
        return interval

    def _fade_signature(self) -> Tuple[Any, ...]:
        """Visible state of every channel, coarse enough to spot a no-op tick."""
        return tuple(
            (kind, round(fade.visible_alpha(), 2), round(fade.out_alpha, 2),
             tuple(fade.visible_lines()))
            for kind, fade in self._fades.items()
        )

    def _ensure_fade_timer(self) -> None:
        """Start the GUI-thread animation timer when a fade is in progress."""
        if not _QT_AVAILABLE or self._fade_timer is None:
            return
        try:
            if self._fade_running() and not self._fade_timer.isActive():
                self._last_fade_ts = time.monotonic()
                self._apply_fade_interval(self._fullscreen_now())
                self._last_signature = self._fade_signature()
                self._fade_timer.start()
        except Exception:
            logger.debug("fade timer start failed", exc_info=True)

    def _stop_fade_timer(self) -> None:
        if not _QT_AVAILABLE or self._fade_timer is None:
            return
        try:
            if self._fade_timer.isActive():
                self._fade_timer.stop()
        except Exception:
            logger.debug("fade timer stop failed", exc_info=True)

    def _on_fade_tick(self) -> None:
        """QTimer callback: advance the fade by the elapsed wall time."""
        now = time.monotonic()
        dt_ms = max(0.0, (now - self._last_fade_ts) * 1000.0)
        self._last_fade_ts = now
        still_running = self.advance_fade(dt_ms)
        # Only repaint when the animation actually changed something: a tick
        # that lands on an unchanged frame (a saturated fade, a very short dt)
        # is counted as a skip instead of costing a paint.
        signature = self._fade_signature()
        if signature != self._last_signature:
            self._last_signature = signature
            try:
                self.update()
            except Exception:
                logger.debug("overlay update failed", exc_info=True)
        else:
            self._paint_skips += 1
        self._apply_fade_interval(self._fullscreen_now())
        if not still_running:
            self._stop_fade_timer()

    def advance_fade(self, dt_ms: float) -> bool:
        """
        Advance every channel animation by ``dt_ms`` milliseconds.

        Public so a headless test can step the animation without an event loop.
        Returns True while any channel is still animating.
        """
        settings = self._settings
        for fade in self._fades.values():
            fade.advance(float(dt_ms), settings.fade_in_ms, settings.fade_out_ms)
        return self._fade_running()

    def fade_alpha(self, channel: str) -> float:
        """Visible opacity (0..1) of one channel's currently displayed line."""
        fade = self._fades.get(str(channel).lower())
        return fade.visible_alpha() if fade is not None else 0.0

    def fade_out_alpha(self, channel: str) -> float:
        """Opacity (0..1) of the outgoing line still cross-fading away."""
        fade = self._fades.get(str(channel).lower())
        if fade is None:
            return 0.0
        return float(max(0.0, min(1.0, fade.out_alpha)))

    def fade_text(self, channel: str) -> str:
        """The text the channel shows right now (current, else outgoing)."""
        fade = self._fades.get(str(channel).lower())
        return "\n".join(fade.visible_lines()) if fade is not None else ""

    @property
    def fade_active(self) -> bool:
        return self._fade_running()

    def fade_stats(self) -> Dict[str, Any]:
        return {
            "fade_in_ms": self._settings.fade_in_ms,
            "fade_out_ms": self._settings.fade_out_ms,
            "fade_slide_px": self._settings.fade_slide_px,
            "running": self._fade_running(),
            "max_repaint_fps": self._settings.max_repaint_fps,
            "min_repaint_interval_ms": self._settings.min_repaint_interval_ms,
            "frame_interval_ms": self._settings.frame_interval_ms(
                self._fullscreen_active),
            "fullscreen": bool(self._fullscreen_active),
            "repaints": self._paint_count,
            "repaint_skips": self._paint_skips,
            "channels": {
                kind: {"alpha": self.fade_alpha(kind),
                       "out_alpha": self.fade_out_alpha(kind),
                       "text": self.fade_text(kind)}
                for kind in self._fades
            },
        }

    # -- visibility -------------------------------------------------------- #

    def refresh(self) -> None:
        """Request a repaint; callable from any thread (Qt queues the paint)."""
        self._sync_fades()
        if not _QT_AVAILABLE:
            return
        # While a fade is running its timer repaints within one frame interval
        # anyway, so a burst of worker-driven refreshes does not each need a
        # paint of its own. The check uses pure-Python state only: no QTimer or
        # widget call is made from the calling (possibly worker) thread.
        if self._fade_running():
            gap = float(self._settings.frame_interval_ms(self._fullscreen_active)) / 1000.0
            now = time.monotonic()
            if (now - self._last_paint_ts) < gap:
                self._paint_skips += 1
                return
        try:
            self.update()
        except Exception:
            logger.debug("overlay update failed", exc_info=True)

    def show_overlay(self) -> None:
        if not _QT_AVAILABLE:
            self.failed.emit("qt-unavailable")
            return
        try:
            self._apply_window_flags()
            self._apply_geometry()
            self.show()
            self.raise_()
            self.refresh()
        except Exception:
            logger.exception("could not show the overlay")
            self.failed.emit("show-failed")
            return
        self.visibility_changed.emit(True)

    def hide_overlay(self) -> None:
        if not _QT_AVAILABLE:
            return
        try:
            self.hide()
        except Exception:
            logger.debug("overlay hide failed", exc_info=True)
        self.visibility_changed.emit(False)

    def toggle_visible(self) -> bool:
        if not _QT_AVAILABLE:
            return False
        try:
            if self.isVisible():
                self.hide_overlay()
            else:
                self.show_overlay()
        except Exception:
            logger.debug("overlay toggle failed", exc_info=True)
        return bool(_QT_AVAILABLE and self.isVisible())

    @property
    def is_closed(self) -> bool:
        return self._closed

    def shutdown(self) -> None:
        """Tear the overlay down during application exit."""
        if not _QT_AVAILABLE:
            self._closed = True
            return
        try:
            if self._notice_timer is not None:
                self._notice_timer.stop()
        except Exception:
            logger.debug("notice timer stop failed", exc_info=True)
        self._stop_fade_timer()
        try:
            self.hide()
        except Exception:
            logger.debug("overlay hide failed", exc_info=True)
        self._closed = True
        self.closed.emit()

    # -- events ------------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._paint_count += 1
        self._last_paint_ts = time.monotonic()
        try:
            self._paint()
        except Exception:
            # A paint failure must never reach Qt's event loop.
            logger.exception("overlay paint failed")

    def _paint(self) -> None:
        settings = self._settings
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            bounds = QRect(0, 0, max(1, self.width()), max(1, self.height()))

            mask = _qcolor(settings.mask_color, QColor(0, 0, 0))
            mask.setAlphaF(max(0.0, min(1.0, settings.mask_opacity)))
            path = QPainterPath()
            path.addRoundedRect(QRectF(bounds), float(settings.corner_radius),
                                float(settings.corner_radius))
            painter.fillPath(path, mask)

            pad = settings.padding
            content = bounds.adjusted(pad, pad, -pad, -pad)
            if content.width() <= 20 or content.height() <= 10:
                return

            header = 16 if settings.show_status_indicator else 0
            footer = 18 if self._notice else 0
            body = content.adjusted(0, header, 0, -footer)
            if body.height() <= 10:
                body = content

            # Keep the fade state in step even for system-triggered repaints.
            self._sync_fades()
            blocks: List[Tuple[str, List[str]]] = []
            for kind in FADE_CHANNELS:
                fade = self._fades.get(kind)
                if fade is None:
                    continue
                lines = fade.visible_lines()
                if lines:
                    blocks.append((kind, lines))

            if blocks:
                font_size, layout = self._fit_blocks(blocks, body)
                self._last_font_size = font_size
                self._draw_blocks(painter, layout, body, font_size)
            self._draw_status(painter, content)
            self._draw_notice(painter, content)
        finally:
            painter.end()

    def _make_font(self, size: int):
        font = QFont(self._settings.font_family)
        try:
            font.setPointSize(int(size))
            font.setWeight(QFont.Weight.DemiBold)
        except Exception:
            logger.debug("font setup degraded", exc_info=True)
        return font

    @staticmethod
    def _text_flags() -> int:
        """Compose Qt text flags as ints (cross-enum OR is unsafe in PySide6)."""
        return (int(Qt.TextFlag.TextWordWrap)
                | int(Qt.AlignmentFlag.AlignHCenter)
                | int(Qt.AlignmentFlag.AlignTop))

    def _fit_blocks(self, blocks: List[Tuple[str, List[str]]],
                    body) -> Tuple[int, List[Tuple[str, List[str], List[int]]]]:
        """
        Choose the largest font size (from the configured one downwards) that
        fits every block into ``body``.
        """
        settings = self._settings
        limit = settings.max_lines_per_channel
        # ``effective_font_size`` honours subtitle.font_scale_with_panel.
        ceiling = int(settings.effective_font_size)
        layout: List[Tuple[str, List[str], List[int]]] = []
        for size in range(ceiling, settings.min_font_size - 1, -1):
            font = self._make_font(size)
            metrics = QFontMetrics(font)
            line_height = max(1, metrics.lineSpacing())
            layout = []
            total = 0
            for kind, lines in blocks:
                visible = lines[-limit:] if limit > 0 else list(lines)
                heights: List[int] = []
                for text in visible:
                    try:
                        box = metrics.boundingRect(
                            QRect(0, 0, max(20, body.width()), 100000),
                            self._text_flags(), text)
                        height = min(int(box.height()),
                                     line_height * MAX_VISUAL_LINES)
                    except Exception:
                        height = line_height
                    heights.append(max(line_height, height))
                layout.append((kind, visible, heights))
                total += sum(heights) + settings.line_spacing * len(heights)
            if total <= body.height() or size <= settings.min_font_size:
                return size, layout
        return settings.min_font_size, layout

    def _draw_blocks(self, painter, layout, body, font_size: int) -> None:
        settings = self._settings
        painter.setFont(self._make_font(font_size))
        text_color = _qcolor(settings.text_color, QColor(255, 255, 255))
        text_color.setAlphaF(max(0.05, min(1.0, settings.text_opacity)))
        outline = _qcolor(settings.outline_color, QColor(16, 16, 16))
        outline.setAlphaF(max(0.05, min(1.0, settings.text_opacity * 0.9)))
        flags = self._text_flags()
        y = body.top()
        for kind, lines, heights in layout:
            fade = self._fades.get(kind)
            if fade is None:
                current_alpha, out_alpha, out_lines = 1.0, 0.0, []
            else:
                current_alpha = fade.alpha
                out_alpha = fade.out_alpha
                out_lines = list(fade.out_lines)
            region_h = sum(heights) + settings.line_spacing * max(0, len(heights) - 1)
            region = QRect(body.left(), y, body.width(), max(1, region_h))
            if fade is not None and not fade.lines:
                # The channel is empty: the layout lines ARE the outgoing lines.
                self._paint_text_item(painter, region, "\n".join(lines), flags,
                                      out_alpha, settings, text_color, outline)
            else:
                # Old line underneath (if any), then the incoming line on top.
                if out_alpha > 0.0 and out_lines and out_lines != lines:
                    self._paint_text_item(painter, region, "\n".join(out_lines),
                                          flags, out_alpha, settings, text_color,
                                          outline)
                if current_alpha > 0.0:
                    item_y = y
                    for text, height in zip(lines, heights):
                        item = QRect(body.left(), item_y, body.width(), height)
                        self._paint_text_item(painter, item, text, flags,
                                              current_alpha, settings, text_color,
                                              outline)
                        item_y += height + settings.line_spacing
            y += region_h + settings.line_spacing

    def _paint_text_item(self, painter, rect, text: str, flags: int, alpha: float,
                         settings, text_color, outline_color) -> None:
        """
        Draw one text item at ``alpha`` opacity, drifted by
        ``fade_slide_px * (1 - alpha)`` pixels for the QQ-Music-style rise.
        """
        alpha = max(0.0, min(1.0, float(alpha)))
        if alpha <= 0.004 or not text:
            return
        offset = int(round(max(0, int(settings.fade_slide_px)) * (1.0 - alpha)))
        painter.save()
        try:
            painter.setClipRect(rect)
            target = rect.translated(0, offset)
            if settings.outline:
                line = QColor(outline_color)
                line.setAlphaF(line.alphaF() * alpha)
                painter.setPen(line)
                for dx, dy in outline_offsets(settings.outline_width):
                    painter.drawText(target.translated(dx, dy), flags, text)
            color = QColor(text_color)
            color.setAlphaF(color.alphaF() * alpha)
            painter.setPen(color)
            painter.drawText(target, flags, text)
        finally:
            painter.restore()

    def _status_text(self) -> str:
        keys = {STATUS_OK: "status_ok", STATUS_BUSY: "status_busy",
                STATUS_ERROR: "status_error", STATUS_UNKNOWN: "status_unknown"}
        base = self._texts.get(keys.get(self._status, "status_unknown"), "")
        if self._status_message:
            return f"{base}: {self._status_message}"
        return base

    def _draw_status(self, painter, content) -> None:
        settings = self._settings
        if not settings.show_status_indicator:
            return
        color = _qcolor(STATUS_COLORS.get(self._status, STATUS_COLORS[STATUS_UNKNOWN]),
                        QColor(154, 160, 166))
        diameter = 10
        dot = QRect(content.right() - diameter, content.top(), diameter, diameter)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(dot)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if settings.show_status_text:
            font = QFont(settings.font_family)
            font.setPointSize(STATUS_FONT_SIZE)
            painter.setFont(font)
            painter.setPen(QColor(235, 235, 235))
            label = QRect(content.left(), content.top(),
                          max(10, content.width() - diameter - 6), 16)
            painter.drawText(label,
                             int(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter),
                             self._status_text())
        painter.restore()

    def _draw_notice(self, painter, content) -> None:
        if not self._notice:
            return
        if time.monotonic() >= self._notice_deadline:
            self._notice = ""
            return
        settings = self._settings
        font = QFont(settings.font_family)
        font.setPointSize(NOTICE_FONT_SIZE)
        painter.save()
        painter.setFont(font)
        painter.setPen(QColor("#FFD479"))
        metrics = QFontMetrics(font)
        text = metrics.elidedText(self._notice, Qt.TextElideMode.ElideRight,
                                  max(20, content.width()))
        rect = QRect(content.left(), content.bottom() - 14, content.width(), 14)
        painter.drawText(rect,
                         int(Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter), text)
        painter.restore()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        # BL-1: overlay.lock_position pins the window where the user put it;
        # click-through already makes the window unreachable in the same way.
        if self._settings.click_through or self._settings.lock_position:
            event.ignore()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            try:
                self._drag_offset = (event.globalPosition().toPoint()
                                     - self.frameGeometry().topLeft())
            except Exception:
                self._drag_offset = None
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._dragging or self._drag_offset is None:
            return
        try:
            point = event.globalPosition().toPoint() - self._drag_offset
            self.move(point)
            self.geometry_changed.emit(point.x(), point.y())
        except Exception:
            logger.debug("overlay drag failed", exc_info=True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._dragging:
            return
        self._dragging = False
        self._persist_geometry()
        self.refresh()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Double click toggles click-through (the window is unreachable when on)."""
        if self._settings.click_through:
            event.ignore()
            return
        self.toggle_click_through()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.hide_overlay()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._closed = True
        try:
            super().closeEvent(event)
        except Exception:
            logger.debug("overlay closeEvent failed", exc_info=True)
        self.closed.emit()

    # -- diagnostics ------------------------------------------------------- #

    def stats(self) -> Dict[str, Any]:
        asr_lines, ocr_lines = self._snapshot()
        data: Dict[str, Any] = {
            "qt_available": _QT_AVAILABLE,
            "status": self._status,
            "notice": self._notice,
            "asr_lines": len(asr_lines),
            "ocr_lines": len(ocr_lines),
            "tts_placeholder": True,
            "show_tts_line": self._settings.show_tts_line,
            "font_size": self._settings.font_size,
            "last_font_size": self._last_font_size,
            "text_opacity": self._settings.text_opacity,
            "mask_opacity": self._settings.mask_opacity,
            "click_through": self._settings.click_through,
            "always_on_top": self._settings.always_on_top,
            "fade": self.fade_stats(),
            "repaints": self._paint_count,
            "repaint_skips": self._paint_skips,
            "frame_interval_ms": self._settings.frame_interval_ms(
                self._fullscreen_active),
            "fullscreen_slowdown": bool(self._fullscreen_active),
            "settings": self._settings.as_dict(),
        }
        if _QT_AVAILABLE:
            try:
                geometry = self.geometry()
                data.update({"visible": bool(self.isVisible()),
                             "x": geometry.x(), "y": geometry.y(),
                             "width": geometry.width(),
                             "height": geometry.height()})
            except Exception:
                logger.debug("overlay geometry read failed", exc_info=True)
        return data
