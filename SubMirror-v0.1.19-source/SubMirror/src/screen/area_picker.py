# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Region picker overlay for the streaming area OCR (official entry).

What it does
    A full-screen, frameless, translucent overlay shown while the area-OCR hotkey
    is held. The user drags a rectangle; the selected area stays completely
    unpainted (only the four surrounding bands are dimmed), so the pixels inside
    the selection are exactly the pixels the capture backend will read.

Interaction contract (v0.1 spec)
    hotkey KEYDOWN   -> overlay appears
    press + drag     -> select a rectangle; streaming OCR may start as soon as
                        the rectangle becomes valid
    mouse release    -> commit the rectangle (does NOT stop the stream)
    hotkey KEYUP     -> finished(region, "hotkey-release"); caller stops the
                        stream and clears the OCR subtitle line
    Esc / right click-> cancel; caller clears the OCR subtitle line as well

Safety red lines honoured here
    * an ordinary top-level Windows window with a translucent background
    * NO DLL injection, NO overlay hooking, NO access to another process
    * NO global keyboard or mouse hooks - this widget only sees the mouse events
      the user delivers to it while it is the active window

Threading
    Every widget call happens on the GUI thread. begin()/end() refuse to run on a
    worker thread (they emit ``failed`` and log) instead of touching Qt from a
    non-GUI thread, which is what the spec forbids.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

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

try:  # pragma: no cover - optional dependency
    from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import QWidget
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - headless / Qt missing
    _QT_AVAILABLE = False

    class _FallbackSignal:
        """Minimal stand-in so the module imports without PySide6."""

        def __init__(self, *args, **kwargs) -> None:
            self._slots = []

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
                    logging.getLogger("submirror.screen.area").exception(
                        "fallback signal slot failed")

    def Signal(*args, **kwargs):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            pass

    class QWidget:  # type: ignore[no-redef]
        pass

    QPoint = QRect = Qt = QColor = QFont = QPainter = QPen = QGuiApplication = None  # type: ignore

__all__ = [
    "MASK_ALPHA_DEFAULT",
    "MIN_SELECTION_PX",
    "AreaPicker",
]

logger = logging.getLogger("submirror.screen.area")

MASK_ALPHA_DEFAULT = 0.35        # dim strength of the four surrounding bands
MIN_SELECTION_PX = 8             # below this a drag counts as "no selection"
BORDER_WIDTH = 2                 # drawn OUTSIDE the selection, never inside it
BORDER_EXPAND = BORDER_WIDTH * 2
REGION_EMIT_INTERVAL_S = 0.05    # throttle for region_changed during a drag
LABEL_FONT_SIZE = 13
LABEL_PADDING = 6

_ZH_TEXT = {
    "hint": "按住拖拽框选区域；松开左键确认，Esc 结束",
    "size": "{w} × {h}",
    "streaming": "区域 OCR 识别中…",
}
_EN_TEXT = {
    "hint": "Drag to select a region; release the mouse to confirm, Esc to finish",
    "size": "{w} × {h}",
    "streaming": "Area OCR running…",
}


def _localized_texts(config: Any) -> Dict[str, str]:
    """
    Pick the overlay strings from the UI language.

    config.t() is used when available, but built-in zh-CN / en strings guarantee
    that the overlay is never shown in the wrong language even if the language
    table is missing those keys.
    """
    language = "zh-CN"
    if config is not None:
        try:
            language = str(config.get("ui_language", language) or language)
        except Exception:
            pass
    table = dict(_EN_TEXT) if language.lower().startswith("en") else dict(_ZH_TEXT)
    translator = getattr(config, "t", None) if config is not None else None
    if callable(translator):
        for key, lang_key in (("hint", "overlay.region_hint"),
                              ("size", "overlay.region_size")):
            try:
                value = translator(lang_key)
            except Exception:
                continue
            if value and value != lang_key:
                table[key] = value
    return table


# --------------------------------------------------------------------------- #
# Overlay window
# --------------------------------------------------------------------------- #

class _PickerWindow(QWidget if _QT_AVAILABLE else object):  # type: ignore[misc]
    """
    Full-screen translucent selection overlay.

    The mask is painted as four bands around the selection, so the selection
    itself is left untouched. That is deliberate: a composited-but-unpainted area
    is transparent to the desktop capture APIs, so the OCR frame contains no
    overlay pixels and the window does not need to be hidden for every sample.
    """

    if _QT_AVAILABLE:
        dragged = Signal(object)      # (left, top, width, height) in global coords
        committed = Signal(object)
        cancelled = Signal(str)

    def __init__(self, origin: Tuple[int, int], size: Tuple[int, int],
                 mask_alpha: float = MASK_ALPHA_DEFAULT,
                 min_size: int = MIN_SELECTION_PX,
                 texts: Optional[Dict[str, str]] = None,
                 parent=None) -> None:
        if not _QT_AVAILABLE:  # pragma: no cover - guarded by AreaPicker
            raise RuntimeError("Qt is not available")
        super().__init__(parent)
        self._origin = QPoint(int(origin[0]), int(origin[1]))
        self._min_size = max(int(min_size), 4)
        self._mask_alpha = float(min(max(mask_alpha, 0.0), 0.95))
        self._texts = dict(texts or _ZH_TEXT)
        self._anchor: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self._rect = QRect()
        self._last_emit = 0.0
        self._closed = False
        # Drag state: only a held left button may resize the selection. Without
        # this flag the rectangle keeps following the cursor after the release
        # (the anchor stays set until the session ends), which looks like the
        # picker "never leaving" draw mode.
        self._dragging = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowTitle("SubMirror Region Picker")
        self.setGeometry(int(origin[0]), int(origin[1]), int(size[0]), int(size[1]))

    # -- geometry ---------------------------------------------------------- #

    @property
    def selection(self) -> Tuple[int, int, int, int]:
        """Current selection in GLOBAL screen coordinates (0,0,0,0 when empty)."""
        rect = self._rect
        if rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
            return (0, 0, 0, 0)
        top_left = self.mapToGlobal(rect.topLeft())
        return (top_left.x(), top_left.y(), rect.width(), rect.height())

    @property
    def has_selection(self) -> bool:
        rect = self._rect
        return (rect.width() >= self._min_size and rect.height() >= self._min_size)

    @property
    def is_dragging(self) -> bool:
        """True while the left button is held down on the overlay."""
        return self._dragging

    def set_selection(self, left: int, top: int, width: int, height: int) -> None:
        """Set the selection from GLOBAL coordinates (used by set_region)."""
        anchor = self.mapFromGlobal(QPoint(int(left), int(top)))
        self._anchor = anchor
        self._current = QPoint(anchor.x() + int(width), anchor.y() + int(height))
        self._rect = QRect(self._anchor, self._current).normalized()
        self._dragging = False
        self.update()

    # -- selection logic --------------------------------------------------- #

    def _update_selection(self, point: QPoint) -> bool:
        """Recompute the selection; returns True when the geometry changed."""
        if self._anchor is None:
            return False
        self._current = point
        new_rect = QRect(self._anchor, point).normalized()
        if new_rect == self._rect:
            return False
        self._rect = new_rect
        return True

    def _emit_drag(self, force: bool = False) -> None:
        if not self.has_selection:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < REGION_EMIT_INTERVAL_S:
            return
        self._last_emit = now
        self.dragged.emit(self.selection)

    # -- painting ---------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            width, height = self.width(), self.height()
            color = QColor(0, 0, 0, int(255 * self._mask_alpha))
            rect = self._rect if self.has_selection else QRect()
            if rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
                painter.fillRect(QRect(0, 0, width, height), color)
            else:
                # Four bands: the inside of the selection is never painted.
                painter.fillRect(QRect(0, 0, width, rect.top()), color)
                painter.fillRect(QRect(0, rect.bottom() + 1, width,
                                       height - rect.bottom() - 1), color)
                painter.fillRect(QRect(0, rect.top(), rect.left(), rect.height()), color)
                painter.fillRect(QRect(rect.right() + 1, rect.top(),
                                       width - rect.right() - 1, rect.height()), color)
                # Outline OUTSIDE the selection so no border pixel lands in frame.
                outer = rect.adjusted(-BORDER_EXPAND, -BORDER_EXPAND,
                                      BORDER_EXPAND, BORDER_EXPAND)
                painter.setPen(QPen(QColor(0, 200, 255), BORDER_WIDTH))
                painter.drawRect(outer)

            font = QFont()
            font.setPointSize(LABEL_FONT_SIZE)
            painter.setFont(font)
            painter.setPen(QPen(QColor(240, 240, 240)))

            hint_rect = painter.fontMetrics().boundingRect(self._texts.get("hint", ""))
            hint_x = max(8, (width - hint_rect.width()) // 2)
            painter.fillRect(QRect(hint_x - LABEL_PADDING, 12,
                                   hint_rect.width() + LABEL_PADDING * 2,
                                   hint_rect.height() + LABEL_PADDING * 2),
                             QColor(0, 0, 0, 150))
            painter.drawText(hint_x, 12 + LABEL_PADDING + hint_rect.height() - 2,
                             self._texts.get("hint", ""))

            if self.has_selection:
                label = self._texts.get("size", "{w} × {h}").format(
                    w=rect.width(), h=rect.height())
                metrics = painter.fontMetrics()
                text_width = metrics.horizontalAdvance(label)
                text_height = metrics.height()
                label_x = max(4, rect.left() - BORDER_EXPAND)
                label_y = rect.top() - BORDER_EXPAND - text_height - LABEL_PADDING
                if label_y < 4:                     # no room above: go below
                    label_y = min(height - text_height - LABEL_PADDING,
                                  rect.bottom() + BORDER_EXPAND + LABEL_PADDING)
                painter.fillRect(QRect(label_x - 2, label_y,
                                       text_width + LABEL_PADDING,
                                       text_height + 2), QColor(0, 0, 0, 170))
                painter.drawText(label_x + 2, label_y + text_height - 2, label)
            painter.end()
        except Exception:
            # A painting failure must never take the application down.
            logger.exception("region picker paint failed")

    # -- events ------------------------------------------------------------ #

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            self._dragging = False
            self.cancelled.emit("right-click")
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._anchor = event.position().toPoint()
            self._current = self._anchor
            self._rect = QRect(self._anchor, self._anchor)
            self._last_emit = 0.0
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        # A hover (button not held) must never move the box: after the release
        # the selection stays exactly where the user left it.
        if not self._dragging:
            return
        if self._update_selection(event.position().toPoint()):
            self.update()
            self._emit_drag()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._dragging:
            # A release without a matching press (or a duplicate release).
            return
        self._dragging = False
        self._update_selection(event.position().toPoint())
        self.update()
        if self.has_selection:
            self._emit_drag(force=True)
            self.committed.emit(self.selection)
        else:
            self.cancelled.emit("too-small")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self._dragging = False
            self.cancelled.emit("escape")
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._closed = True
        self._dragging = False
        self.cancelled.emit("closed")
        super().closeEvent(event)

    # -- visibility -------------------------------------------------------- #

    def show_overlay(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.repaint()

    def hide_overlay(self) -> None:
        self.hide()

    @property
    def is_closed(self) -> bool:
        return self._closed


# --------------------------------------------------------------------------- #
# Picker controller
# --------------------------------------------------------------------------- #

class AreaPicker(QObject):
    """
    Drives the overlay and turns raw drags into ``ImageRegion`` values.

    The caller (the OCR service) only needs to react to these signals:

        region_ready(ImageRegion)    first valid rectangle of the current drag
                                     -> start the streaming area OCR
        region_changed(ImageRegion)  rectangle moved/resized (throttled)
        committed(ImageRegion)       mouse released on a valid rectangle
        finished(region | None, reason)
                                     hotkey released (or a cancel)
                                     -> stop the stream, clear the OCR line
        failed(reason)               Qt missing / wrong thread / no screen
    """

    if _QT_AVAILABLE:
        region_ready = Signal(object)
        region_changed = Signal(object)
        committed = Signal(object)
        finished = Signal(object, str)
        failed = Signal(str)
    else:  # pragma: no cover - fallback wiring
        region_ready = Signal(object)
        region_changed = Signal(object)
        committed = Signal(object)
        finished = Signal(object, str)
        failed = Signal(str)

    def __init__(self, config: Any = None, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._gui_thread = threading.get_ident()
        self._window: Optional[_PickerWindow] = None
        self._active = False
        self._region: Optional[ImageRegion] = None
        self._ready_sent = False
        self._suspend_depth = 0
        self._resume_visible = False
        self._texts: Dict[str, str] = _localized_texts(config)
        self.mask_alpha = MASK_ALPHA_DEFAULT
        self.min_selection_px = MIN_SELECTION_PX
        self.primary_only = True          # v0.1: main display only, like OCR
        self.last_region: Optional[ImageRegion] = None
        self.auto_hide_for_capture = False
        self._stats: Dict[str, Any] = {"activations": 0, "sessions": 0,
                                       "completed": 0, "cancelled": 0,
                                       "emissions": 0, "last_reason": ""}
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        """Re-read the overlay options from config (safe on a running session)."""
        self._texts = _localized_texts(self._config)
        try:
            self.mask_alpha = float(self._get("ocr.picker_mask_opacity",
                                              MASK_ALPHA_DEFAULT))
        except (TypeError, ValueError):
            self.mask_alpha = MASK_ALPHA_DEFAULT
        self.mask_alpha = min(max(self.mask_alpha, 0.0), 0.95)
        try:
            self.min_selection_px = int(self._get("ocr.min_region_size",
                                                  MIN_SELECTION_PX))
        except (TypeError, ValueError):
            self.min_selection_px = MIN_SELECTION_PX
        self.min_selection_px = max(4, min(self.min_selection_px, 256))
        if self._window is not None:
            self._window._mask_alpha = self.mask_alpha
            self._window._min_size = self.min_selection_px
            self._window._texts = dict(self._texts)
            self._window.update()

    def _get(self, key: str, default: Any) -> Any:
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    # -- state ------------------------------------------------------------- #

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def region(self) -> Optional[ImageRegion]:
        return self._region

    def _is_gui_thread(self) -> bool:
        return threading.get_ident() == self._gui_thread

    def _overlay_geometry(self) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]:
        """Primary-screen geometry as ((x, y), (width, height))."""
        if not _QT_AVAILABLE:
            return None
        try:
            screen = QGuiApplication.primaryScreen()
        except Exception:
            screen = None
        if screen is None:
            return None
        geometry = screen.geometry()
        if geometry.width() <= 0 or geometry.height() <= 0:
            return None
        return ((geometry.x(), geometry.y()), (geometry.width(), geometry.height()))

    # -- lifecycle --------------------------------------------------------- #

    def begin(self) -> bool:
        """
        Show the overlay. Returns True when it became active.

        Called on the area-OCR hotkey KEYDOWN; refuses to run off the GUI thread.
        """
        if self._active:
            return True
        if not _QT_AVAILABLE:
            logger.error("area picker unavailable: PySide6 is not installed")
            self.failed.emit("qt-unavailable")
            return False
        if not self._is_gui_thread():
            logger.error("area picker must be driven from the GUI thread")
            self.failed.emit("wrong-thread")
            return False
        geometry = self._overlay_geometry()
        if geometry is None:
            logger.error("area picker unavailable: no primary screen geometry")
            self.failed.emit("no-screen")
            return False

        origin, size = geometry
        self._texts = _localized_texts(self._config)
        try:
            window = _PickerWindow(origin, size, mask_alpha=self.mask_alpha,
                                   min_size=self.min_selection_px,
                                   texts=self._texts)
            window.dragged.connect(self._on_dragged)
            window.committed.connect(self._on_committed)
            window.cancelled.connect(self._on_cancelled)
            self._window = window
            window.show_overlay()
        except Exception:
            logger.exception("area picker failed to start")
            self._window = None
            self.failed.emit("start-failed")
            return False

        self._active = True
        self._region = None
        self._ready_sent = False
        self._suspend_depth = 0
        self._stats["activations"] += 1
        logger.info("area picker shown on primary screen %sx%s", size[0], size[1])
        return True

    def end(self, reason: str = "hotkey-release") -> Optional[ImageRegion]:
        """
        Hide the overlay and report the session result.

        The caller stops the OCR stream and clears the OCR subtitle line here.
        """
        region = self._region
        if self._window is not None:
            try:
                self._window.hide_overlay()
                self._window.deleteLater()
            except Exception:
                logger.debug("picker window teardown failed", exc_info=True)
            self._window = None
        was_active = self._active
        self._active = False
        self._region = None
        self._ready_sent = False
        self._suspend_depth = 0
        if not was_active and region is None:
            # A release without a matching press: report it, but do not count it.
            self._stats["last_reason"] = reason
            self.finished.emit(None, reason)
            return None
        self._stats["sessions"] += 1
        self._stats["last_reason"] = reason
        if region is not None:
            self._stats["completed"] += 1
        else:
            self._stats["cancelled"] += 1
        if region is not None:
            self.last_region = region
        logger.info("area picker closed (%s), region=%s", reason,
                    region.as_tuple if region is not None else None)
        self.finished.emit(region, reason)
        return region

    def cancel(self, reason: str = "escape") -> None:
        """Abort the current selection (Esc / right click / shutdown)."""
        if self._active:
            self.end(reason)
        else:
            self._stats["cancelled"] += 1
            self._stats["last_reason"] = reason
            self.finished.emit(None, reason)

    def shutdown(self) -> None:
        """Release everything; safe to call during application exit."""
        try:
            if self._active:
                self.end("shutdown")
            elif self._window is not None:
                self._window.hide_overlay()
                self._window.deleteLater()
                self._window = None
        except Exception:
            logger.debug("area picker shutdown failed", exc_info=True)

    # -- hotkey slots ------------------------------------------------------ #

    def on_region_pressed(self) -> None:
        """Connect to GlobalHotkeyManager.region_pressed."""
        self.begin()

    def on_region_released(self) -> None:
        """Connect to GlobalHotkeyManager.region_released."""
        if self._active:
            self.end("hotkey-release")

    # -- window callbacks -------------------------------------------------- #

    def _on_dragged(self, payload) -> None:
        region = self._to_region(payload)
        if region is None:
            return
        self._region = region
        self._stats["emissions"] += 1
        if not self._ready_sent:
            self._ready_sent = True
            self.region_ready.emit(region)
        self.region_changed.emit(region)

    def _on_committed(self, payload) -> None:
        region = self._to_region(payload)
        if region is None:
            return
        self._region = region
        self.last_region = region
        self.stat_note_commit()
        self.committed.emit(region)

    def stat_note_commit(self) -> None:
        self._stats["commits"] = int(self._stats.get("commits", 0)) + 1

    def _on_cancelled(self, reason: str) -> None:
        # "too-small" keeps the overlay alive so the user can simply drag again.
        if reason == "too-small":
            logger.debug("area picker: selection below minimum, ignored")
            return
        self.cancel(reason or "cancelled")

    def _to_region(self, payload) -> Optional[ImageRegion]:
        try:
            if isinstance(payload, ImageRegion):
                return payload
            values = list(payload)[:4] if payload is not None else []
            if len(values) < 4:
                return None
            region = ImageRegion(int(values[0]), int(values[1]),
                                 int(values[2]), int(values[3]))
            return region if region.is_valid(self.min_selection_px) else None
        except Exception:
            logger.exception("area picker could not build a region")
            return None

    # -- programmatic selection -------------------------------------------- #

    def set_region(self, region: ImageRegion) -> bool:
        """Apply a region without a drag (v0.2 preset hook; safe no-op if hidden)."""
        if self._window is None or not self._active:
            return False
        try:
            self._window.set_selection(region.left, region.top,
                                       region.width, region.height)
            self._region = region
            self._ready_sent = True
            self.region_changed.emit(region)
            return True
        except Exception:
            logger.exception("area picker set_region failed")
            return False

    # -- capture cooperation ----------------------------------------------- #

    def suspend_for_capture(self) -> bool:
        """
        Temporarily hide the overlay, e.g. before a forced snapshot.

        Normally unnecessary: the selection is unpainted, so the capture APIs
        already see clean pixels. Kept for callers that want zero overlay
        contribution at all.
        """
        self._suspend_depth += 1
        if not self.auto_hide_for_capture:
            return False
        if self._window is None or self._suspend_depth > 1:
            return False
        try:
            self._resume_visible = self._window.isVisible()
            if self._resume_visible:
                self._window.hide_overlay()
            return self._resume_visible
        except Exception:
            logger.debug("suspend_for_capture failed", exc_info=True)
            return False

    def resume_after_capture(self) -> None:
        self._suspend_depth = max(0, self._suspend_depth - 1)
        if not self.auto_hide_for_capture or self._suspend_depth > 0:
            return
        if self._window is not None and self._resume_visible:
            try:
                self._window.show_overlay()
            except Exception:
                logger.debug("resume_after_capture failed", exc_info=True)
        self._resume_visible = False

    @contextmanager
    def capture_guard(self):
        """Context manager form of suspend/resume."""
        self.suspend_for_capture()
        try:
            yield
        finally:
            self.resume_after_capture()

    # -- diagnostics ------------------------------------------------------- #

    def stats(self) -> Dict[str, Any]:
        data = dict(self._stats)
        data["active"] = self._active
        data["mask_alpha"] = round(self.mask_alpha, 3)
        data["min_selection_px"] = self.min_selection_px
        data["region"] = (self._region.as_tuple
                          if isinstance(self._region, ImageRegion) else None)
        data["last_region"] = (self.last_region.as_tuple
                               if isinstance(self.last_region, ImageRegion) else None)
        data["qt_available"] = _QT_AVAILABLE
        return data
