# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Settings panel, hotkey capture, disclaimer gate (item 15).

What this module owns
    * ``SettingsDialog``  - the whole settings panel. It is the only place that
      writes user chosen values back into ``config.json``, and it always writes
      through ``ConfigManager.update()`` so the schema in src/core/config.py
      clamps every value (no hand written JSON, no broken types on disk).
    * ``HotkeyEdit``      - a read-only field that records a key combination
      when you press it. It normalises through ``normalize_hotkey()`` so the two
      OCR hotkeys use the exact same spelling the hotkey manager expects.
    * ``validate_model_dir`` - pre-flight check for a Faster-Whisper folder.
    * ``DisclaimerDialog`` - the startup gate. The overlay and the settings panel
      are only loaded after the box is ticked (spec: 勾选同意才可加载).
    * ``notify_config_reset`` - tells the user when a damaged config.json was
      repaired instead of failing at startup.

Design decisions worth remembering
    * Live language switching without a restart: the language combo writes
      ``ui_language`` immediately and then re-applies every string from
      ``ConfigManager.t()``. That is why the panel keeps a binding list instead
      of hard coding text at construction time. Because the change is applied at
      once it also survives a later "Cancel" - a display preference should not
      need a Save click (documented behaviour, spec item: 实时切换).
    * Hotkey conflict = no write at all. ``apply()`` validates first, shows the
      localised ``dialog.hotkey_conflict.*`` message box and returns False
      *before* touching the config file.
    * OCR group keeps WinRT only. v0.1 has exactly one OCR engine
      (Windows.Media.Ocr), so there is no engine picker: just the preferred
      language and the sampling limits.
    * The v0.2 tab is 12 disabled placeholders with a tooltip. No logic, ever.

Safety: this module only ever *writes a JSON file* and re-reads it. It never
touches a game process, memory, hooks, drivers or simulated input.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # Qt is required for every widget in this module.
    from PySide6.QtCore import QObject, Qt, Signal  # type: ignore[import-not-found]
    from PySide6.QtWidgets import (  # type: ignore[import-not-found]
        QApplication,
        QCheckBox,
        QColorDialog,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSlider,
        QSpinBox,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - the panel is simply unavailable then
    _QT_AVAILABLE = False

from src.core.config import (  # noqa: E402  (import order is intentional)
    APP_NAME,
    APP_VERSION,
    DEFAULT_LANGUAGE,
    LOAD_CORRUPT,
    LOAD_PARTIAL,
    SINGLE_QUEUE_MAX,
    SUPPORTED_LANGUAGES,
    normalize_hotkey,
)

# 0.1.19: the work-mode presets live in their own module so the panel, the
# runtime and the suites all read the same table.
try:  # pragma: no cover - trivial import guard
    from src.core import modes as app_modes
except Exception:  # pragma: no cover - keeps the panel importable
    app_modes = None

# The settings panel needs the same conflict test the hotkey manager uses; the
# local fallback keeps the panel importable if that module is ever missing.
try:  # pragma: no cover - trivial import guard
    from src.core.hotkey import check_hotkey_conflict
except Exception:  # pragma: no cover
    def check_hotkey_conflict(area_key: Any, fullscreen_key: Any,
                              toggle_key: Any = None,
                              region_toggle_key: Any = None) -> bool:
        values = [v for v in (normalize_hotkey(area_key),
                              normalize_hotkey(fullscreen_key),
                              normalize_hotkey(toggle_key) if toggle_key is not None else None,
                              (normalize_hotkey(region_toggle_key)
                               if region_toggle_key is not None else None))
                  if v is not None]
        return len(values) != len(set(values))

try:  # pragma: no cover - lane names are only used for the reset buttons
    from src.core.task_scheduler import (LANE_ASR, LANE_AUDIO, LANE_OCR, LANE_VAD)
except Exception:  # pragma: no cover
    LANE_AUDIO, LANE_VAD, LANE_ASR, LANE_OCR = "audio", "vad", "asr", "ocr"

# Logon auto-start lives in a small Qt-free module; the guard keeps the panel
# importable when a partial install dropped it.
try:  # pragma: no cover - trivial import guard
    from src.core import silent_launch
except Exception:  # pragma: no cover
    silent_launch = None                                       # type: ignore[assignment]

# Panel-size presets are defined next to the code that draws the panel, so the
# dropdown and the overlay can never disagree about the numbers.
try:  # pragma: no cover - trivial import guard
    from src.ui.overlay import DEFAULT_SIZE_PRESET, preset_geometry
except Exception:  # pragma: no cover
    DEFAULT_SIZE_PRESET = "default"

    def preset_geometry(name: str) -> Tuple[int, int, int]:
        table = {"default": (900, 220, 14), "compact": (660, 120, 10),
                 "square": (600, 600, 12)}
        return table.get(str(name or "").strip().lower(), table["default"])

__all__ = [
    "SettingsDialog",
    "DisclaimerDialog",
    "HotkeyEdit",
    "validate_model_dir",
    "notify_config_reset",
    "open_settings",
]

logger = logging.getLogger("submirror.ui.settings_ui")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MODEL_MARKERS: Tuple[str, ...] = (
    "model.bin",
    "model.safetensors",
    "model.onnx",
    "pytorch_model.bin",
)
CONFIG_HINTS: Tuple[str, ...] = ("config.json",)
SNAPSHOT_DIR = "snapshots"
MAX_ISSUES_SHOWN = 8

DIALOG_MIN_WIDTH = 720
DIALOG_MIN_HEIGHT = 560
OPACITY_SLIDER_MAX = 100

# Language names are shown in their own language, so they are not translated.
UI_LANGUAGE_LABELS: Tuple[Tuple[str, str], ...] = (
    ("zh-CN", "简体中文"),
    ("en", "English"),
)

# WinRT OCR languages: the first entry means "let Windows choose".
WINRT_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("", "settings.ocr_winrt_language.auto"),
    ("en-US", "settings.ocr_winrt_language.en-US"),
    ("zh-Hans-CN", "settings.ocr_winrt_language.zh-Hans-CN"),
    ("zh-Hant-TW", "settings.ocr_winrt_language.zh-Hant-TW"),
    ("ja-JP", "settings.ocr_winrt_language.ja-JP"),
    ("ko-KR", "settings.ocr_winrt_language.ko-KR"),
    ("ru-RU", "settings.ocr_winrt_language.ru-RU"),
)

ASR_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("auto", "settings.asr_language.auto"),
    ("en", "settings.asr_language.en"),
    ("zh", "settings.asr_language.zh"),
)

# Whisper model sizes offered on the ASR page. The empty value means "follow the
# model folder above"; the labels are shown verbatim because the size names are
# product names, not translated UI text, and config.t() falls back to the key.
ASR_MODEL_SIZES: Tuple[Tuple[str, str], ...] = (
    ("", "跟随模型路径 / Follow model path"),
    ("tiny", "tiny"),
    ("base", "base"),
    ("small", "small"),
    ("medium", "medium"),
)

VAD_MODES: Tuple[Tuple[str, str], ...] = (
    ("single", "settings.vad_mode.single"),
    ("fps", "settings.vad_mode.fps"),
)

# Hover tips for the ASR page (0.1.16). Presentation only: the text comes from
# config.t() so it follows the UI language, and nothing here is collected into
# config.json or read by the recognition pipeline.
ASR_MODEL_SIZE_TIPS: Tuple[Tuple[str, str], ...] = (
    ("", "settings.asr_model_size.tip.none"),
    ("tiny", "settings.asr_model_size.tip.tiny"),
    ("base", "settings.asr_model_size.tip.base"),
    ("small", "settings.asr_model_size.tip.small"),
    ("medium", "settings.asr_model_size.tip.medium"),
)

VAD_ENGINE_TIPS: Tuple[str, ...] = (
    "settings.asr_vad_engine.tip.auto",
    "settings.asr_vad_engine.tip.silero",
    "settings.asr_vad_engine.tip.energy",
)

SOURCE_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("auto", "settings.asr_language.auto"),
    ("en", "settings.asr_language.en"),
    ("zh", "settings.asr_language.zh"),
)

TARGET_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("zh-CN", "settings.ocr_winrt_language.zh-Hans-CN"),
    ("en", "settings.ocr_winrt_language.en-US"),
)

FONT_FAMILIES: Tuple[str, ...] = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "Segoe UI",
    "SimHei",
    "SimSun",
    "Consolas",
    "Arial",
    "Verdana",
)

# v0.2 is UI-only in v0.1: these boxes stay disabled forever in this release.
V02_PLACEHOLDERS: Tuple[str, ...] = (
    "v02.slang",
    "v02.log_export",
    "v02.pause_on_blur",
    "v02.model_download",
    "v02.multi_source",
    "v02.vad_slider",
    "v02.area_preset",
    "v02.subtitle_history",
    "v02.subtitle_anim",
    "v02.streaming_asr",
    "v02.window_tracking",
    "v02.multimonitor",
)


# --------------------------------------------------------------------------- #
# Helpers that work without Qt (also used by main.py)
# --------------------------------------------------------------------------- #

def _config_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except Exception:                                  # noqa: BLE001
            return default
        return default if value is None else value
    return default


def config_text(config: Any, key: str, **fmt: Any) -> str:
    """Localised UI text; falls back to the key so nothing renders as blank."""
    translator = getattr(config, "t", None)
    if callable(translator):
        try:
            return str(translator(key, **fmt))
        except Exception:                                  # noqa: BLE001
            logger.debug("could not translate %s", key, exc_info=True)
    return key


def _mode_options() -> List[Tuple[str, str]]:
    """``(value, lang key)`` pairs for the work-mode combo (0.1.19)."""
    if app_modes is not None:
        try:
            return [(key, app_modes.MODE_LABEL_KEYS[key]) for key in app_modes.MODE_ORDER]
        except Exception:                                  # noqa: BLE001
            logger.debug("work mode table unavailable", exc_info=True)
    return [("allround", "settings.mode.allround"),
            ("game", "settings.mode.game"),
            ("conversation", "settings.mode.conversation")]


def _source_options() -> List[Tuple[str, str]]:
    """``(value, lang key)`` pairs for the capture-source combo (0.1.19)."""
    if app_modes is not None:
        try:
            return [(key, app_modes.SOURCE_LABEL_KEYS[key]) for key in app_modes.SOURCE_ORDER]
        except Exception:                                  # noqa: BLE001
            logger.debug("capture source table unavailable", exc_info=True)
    return [("speaker", "settings.audio.source.speaker"),
            ("microphone", "settings.audio.source.microphone"),
            ("mixed", "settings.audio.source.mixed")]


def validate_model_dir(path: Any) -> Tuple[bool, str]:
    """
    Cheap local pre-check for a Faster-Whisper model folder.

    Accepted layouts (all offline, read-only):
      * ``model.bin`` + ``config.json``           (CTranslate2 export)
      * ``model.safetensors`` / ``model.onnx`` / ``pytorch_model.bin``
      * ``config.json`` plus any ``*.bin`` file
      * a Hugging Face cache root containing ``snapshots/<hash>/...``
    """
    text = str(path or "").strip()
    if not text:
        return False, "empty"
    target = os.path.abspath(os.path.expanduser(text))
    if os.path.isfile(target):                             # user picked a file
        target = os.path.dirname(target)
    if not os.path.isdir(target):
        return False, "not-a-directory"
    try:
        entries = os.listdir(target)
    except PermissionError:
        return False, "permission-denied"
    except OSError:
        return False, "unreadable"

    if _looks_like_model(entries):
        return True, "ok"

    snapshots = os.path.join(target, SNAPSHOT_DIR)
    if os.path.isdir(snapshots):
        try:
            for name in sorted(os.listdir(snapshots)):
                nested = os.path.join(snapshots, name)
                if os.path.isdir(nested) and _looks_like_model(os.listdir(nested)):
                    return True, "ok"
        except OSError:
            return False, "unreadable"
    return False, "incomplete"


def _looks_like_model(entries: Sequence[str]) -> bool:
    lowered = {str(name).lower() for name in entries}
    if any(marker in lowered for marker in MODEL_MARKERS):
        return True
    if any(hint in lowered for hint in CONFIG_HINTS):
        return any(name.endswith(".bin") for name in lowered)
    return False


def notify_config_reset(config: Any, parent: Any = None) -> Optional[str]:
    """
    Warn the user when config.json was damaged and repaired at load time.

    Returns the message that was shown (or logged when Qt is unavailable), or
    None when there is nothing to report. A missing file on first run is
    ``LOAD_DEFAULT`` and deliberately stays silent.
    """
    status = str(getattr(config, "status", "") or "")
    if status not in (LOAD_CORRUPT, LOAD_PARTIAL):
        return None
    title = config_text(config, "dialog.config_reset.title")
    body = config_text(config, "dialog.config_reset.body")
    issues = list(getattr(config, "issues", []) or [])
    if issues:
        details = "\n".join(
            f"- {key}: {reason}" for key, reason in issues[:MAX_ISSUES_SHOWN])
        body = f"{body}\n{details}"
    logger.warning("configuration was repaired at startup: %s",
                   body.replace("\n", " | "))
    if _QT_AVAILABLE and QApplication.instance() is not None:
        try:
            QMessageBox.warning(parent, title, body)
        except Exception:                                  # noqa: BLE001
            logger.debug("could not show the config reset notice", exc_info=True)
    return body


# --------------------------------------------------------------------------- #
# Qt key helpers (survive the scoped/unscoped enum change in PySide6)
# --------------------------------------------------------------------------- #

def _qt_const(enum_name: str, *member_names: str) -> Any:
    owners: List[Any] = []
    scoped = getattr(Qt, enum_name, None)
    if scoped is not None:
        owners.append(scoped)
    owners.append(Qt)
    for owner in owners:
        for member in member_names:
            value = getattr(owner, member, None)
            if value is not None:
                return value
    return None


if _QT_AVAILABLE:
    _MOD_CTRL = _qt_const("KeyboardModifier", "ControlModifier")
    _MOD_ALT = _qt_const("KeyboardModifier", "AltModifier")
    _MOD_SHIFT = _qt_const("KeyboardModifier", "ShiftModifier")
    _MOD_META = _qt_const("KeyboardModifier", "MetaModifier")
    _KEY_ESCAPE = _qt_const("Key", "Key_Escape", "Escape")
    _KEY_BACKSPACE = _qt_const("Key", "Key_Backspace", "Backspace")
    _KEY_DELETE = _qt_const("Key", "Key_Delete", "Delete")
    _MODIFIER_KEYS = {
        value for value in (
            _qt_const("Key", "Key_Control", "Control"),
            _qt_const("Key", "Key_Alt", "Alt"),
            _qt_const("Key", "Key_Shift", "Shift"),
            _qt_const("Key", "Key_Meta", "Meta"),
            _qt_const("Key", "Key_AltGr", "AltGr"),
            _qt_const("Key", "Key_CapsLock", "CapsLock"),
            _qt_const("Key", "Key_unknown", "Key_Unknown"),
        ) if value is not None
    }
    _NAMED_KEYS: Dict[Any, str] = {}
    for _name in ("Space", "Tab", "Return", "Enter", "Insert", "Home", "End",
                  "PageUp", "PageDown", "Up", "Down", "Left", "Right",
                  "Pause", "Print", "ScrollLock", "NumLock"):
        _value = _qt_const("Key", f"Key_{_name}", _name)
        if _value is not None:
            _NAMED_KEYS[_value] = "Enter" if _name == "Return" else _name
    for _index in range(1, 25):
        _value = _qt_const("Key", f"Key_F{_index}", f"F{_index}")
        if _value is not None:
            _NAMED_KEYS[_value] = f"F{_index}"
    del _name, _value, _index


def _modifier_names(modifiers: Any) -> List[str]:
    names: List[str] = []
    if _MOD_CTRL is not None and bool(modifiers & _MOD_CTRL):
        names.append("Ctrl")
    if _MOD_ALT is not None and bool(modifiers & _MOD_ALT):
        names.append("Alt")
    if _MOD_SHIFT is not None and bool(modifiers & _MOD_SHIFT):
        names.append("Shift")
    if _MOD_META is not None and bool(modifiers & _MOD_META):
        names.append("Win")
    return names


# --------------------------------------------------------------------------- #
# Pure-python fallbacks when Qt is unavailable
# --------------------------------------------------------------------------- #

if not _QT_AVAILABLE:  # pragma: no cover - exercised only without PySide6

    HotkeyEdit = None            # type: ignore[assignment]
    SettingsDialog = None        # type: ignore[assignment]
    DisclaimerDialog = None      # type: ignore[assignment]

    def open_settings(config: Any = None, **kwargs: Any) -> bool:
        """Without Qt there is no panel to open; say so instead of crashing."""
        logger.error("the settings panel needs PySide6 and is unavailable")
        return False

else:

    # ----------------------------------------------------------------------- #
    # Hotkey capture field
    # ----------------------------------------------------------------------- #

    class HotkeyEdit(QLineEdit):  # type: ignore[misc]
        """
        Read-only field that records the combination you press.

        Esc/Backspace/Delete clears it. Modifier-only presses are ignored, and
        the captured text only becomes the field value when ``normalize_hotkey``
        accepts it (at least one modifier plus exactly one ordinary key).
        """

        hotkey_changed = Signal(str)

        def __init__(self, hotkey: str = "", parent: Any = None) -> None:
            super().__init__(parent)
            self.setReadOnly(True)
            self.setClearButtonEnabled(False)
            self._hotkey = ""
            self.set_hotkey(hotkey, notify=False)

        # -- value --------------------------------------------------------- #

        def hotkey(self) -> str:
            return self._hotkey

        def set_hotkey(self, value: Any, notify: bool = True) -> bool:
            normalized = normalize_hotkey(value)
            if normalized is None:
                self._hotkey = ""
                super().setText("")
                return False
            self._hotkey = normalized
            super().setText(normalized)
            if notify:
                self.hotkey_changed.emit(normalized)
            return True

        def clear_hotkey(self) -> None:
            if self._hotkey:
                self._hotkey = ""
                super().setText("")
                self.hotkey_changed.emit("")

        # -- capture ------------------------------------------------------- #

        def keyPressEvent(self, event: Any) -> None:  # noqa: N802 (Qt API)
            key = event.key()
            if key in {k for k in (_KEY_ESCAPE, _KEY_BACKSPACE, _KEY_DELETE)
                       if k is not None}:
                self.clear_hotkey()
                event.accept()
                return
            if key in _MODIFIER_KEYS:
                event.accept()                             # wait for the real key
                return
            name = _NAMED_KEYS.get(key)
            if name is None:
                text = (event.text() or "").strip()
                name = text.upper() if len(text) == 1 else ""
            if not name:
                event.accept()
                return
            candidates = _modifier_names(event.modifiers()) + [name]
            normalized = normalize_hotkey("+".join(candidates))
            if normalized is not None:
                self.set_hotkey(normalized, notify=False)
                self.hotkey_changed.emit(normalized)
            event.accept()

    # ----------------------------------------------------------------------- #
    # Settings dialog
    # ----------------------------------------------------------------------- #

    class SettingsDialog(QDialog):  # type: ignore[misc]
        """The v0.1 settings panel: 8 tabs, declarative bindings, live language."""

        settings_applied = Signal(object)     # dict of dotted keys that changed
        ui_language_changed = Signal(str)     # new ui_language
        tasks_reset = Signal()                # reset audio/OCR background work
        pick_requested = Signal()             # draw a new fixed-region rectangle
        failed = Signal(str)                  # non fatal problem, for the log
        mode_selected = Signal(str)           # 0.1.19: work mode changed live

        def __init__(self, config: Any = None, context: Any = None,
                     overlay: Any = None, hotkey_manager: Any = None,
                     scheduler: Any = None, audio_capture: Any = None,
                     on_apply: Optional[Callable[[Dict[str, Any]], None]] = None,
                     parent: Any = None) -> None:
            super().__init__(parent)
            self._config = config
            self._context = context
            self._overlay = overlay
            self._hotkey_manager = hotkey_manager
            self._scheduler = scheduler
            self._audio_capture = audio_capture
            self._on_apply = on_apply
            self._saved = False
            # Whether speech recognition is currently switched on (the runtime
            # ASR on/off state). While it is off the Whisper folder is simply not
            # in use, so saving the panel must not refuse an unusable path.
            self._asr_enabled = True
            self._text_bindings: List[Tuple[Any, str, Callable[[Any, str], None]]] = []
            self._combo_bindings: List[Tuple[Any, Sequence[Tuple[str, str]]]] = []
            self._tab_bindings: List[Tuple[int, str]] = []
            self._build_ui()
            self.load_from_config()

        # -- construction -------------------------------------------------- #

        def set_asr_enabled(self, enabled: bool) -> None:
            """
            Tell the panel whether speech recognition is currently on.

            Called by the main window (which owns the ASR on/off state) right
            before the dialog is shown.  With ASR off the model folder is not
            used by anything, so ``validate`` stops judging it: an empty, moved
            or deleted folder must not block Save any more.
            """
            self._asr_enabled = bool(enabled)

        @property
        def asr_enabled(self) -> bool:
            return bool(self._asr_enabled)

        def _build_ui(self) -> None:
            self.setMinimumSize(DIALOG_MIN_WIDTH, DIALOG_MIN_HEIGHT)
            self._tabs = QTabWidget(self)

            # BL-5: nine pages, ordered exactly as the acceptance checklist lists
            # them. Only the grouping changed - every control, config key and the
            # collect() key set are untouched.
            self._tabs.addTab(self._build_general_tab(), "")
            self._tabs.addTab(self._build_hotkey_tab(), "")
            self._tabs.addTab(self._build_asr_tab(), "")
            self._tabs.addTab(self._build_translate_tab(), "")
            self._tabs.addTab(self._build_ocr_tab(), "")
            self._tabs.addTab(self._build_subtitle_tab(), "")
            self._tabs.addTab(self._build_queue_tab(), "")
            self._tabs.addTab(self._build_advanced_tab(), "")
            self._tabs.addTab(self._build_about_tab(), "")
            for index, key in enumerate(("settings.tab.general", "settings.tab.hotkeys",
                                        "settings.tab.asr", "settings.tab.translate",
                                        "settings.tab.ocr", "settings.tab.overlay",
                                        "settings.tab.queue", "settings.tab.advanced",
                                        "settings.tab.about")):
                self._tab_bindings.append((index, key))

            self._buttons = QDialogButtonBox(self)
            self._btn_save = self._buttons.addButton(
                "", QDialogButtonBox.ButtonRole.AcceptRole)
            self._btn_cancel = self._buttons.addButton(
                "", QDialogButtonBox.ButtonRole.RejectRole)
            self._btn_close = self._buttons.addButton(
                "", QDialogButtonBox.ButtonRole.DestructiveRole)
            self._btn_save.clicked.connect(self._on_save)
            self._btn_cancel.clicked.connect(self._on_cancel)
            self._btn_close.clicked.connect(self._on_cancel)
            self._bind_text(self._btn_save, "settings.btn.save", _set_button_text)
            self._bind_text(self._btn_cancel, "settings.btn.cancel", _set_button_text)
            self._bind_text(self._btn_close, "settings.btn.close", _set_button_text)

            layout = QVBoxLayout(self)
            layout.addWidget(self._tabs, 1)
            layout.addWidget(self._buttons, 0)
            self._bind_text(self, "settings.title", _set_window_title)
            self.retranslate()

        def _page(self) -> Tuple[QWidget, QVBoxLayout]:
            page = QWidget(self)
            outer = QVBoxLayout(page)
            outer.setContentsMargins(12, 12, 12, 12)
            scroll = QScrollArea(page)
            scroll.setWidgetResizable(True)
            inner = QWidget(scroll)
            box = QVBoxLayout(inner)
            box.setContentsMargins(4, 4, 4, 4)
            scroll.setWidget(inner)
            outer.addWidget(scroll, 1)
            return page, box

        def _form(self, box: QVBoxLayout, title_key: str) -> QFormLayout:
            group = QGroupBox(self)
            self._bind_text(group, title_key, _set_group_title)
            form = QFormLayout(group)
            form.setLabelAlignment(_qt_const("AlignmentFlag", "AlignRight",
                                             "AlignRight") or 0)
            box.addWidget(group)
            return form

        @staticmethod
        def _label(form: QFormLayout, widget: QWidget, key: str, owner: Any) -> None:
            label = QLabel(owner)
            owner._bind_text(label, key, _set_label_text)   # type: ignore[attr-defined]
            form.addRow(label, widget)

        def _spin(self, low: int, high: int, suffix: str = "",
                  step: int = 1) -> QSpinBox:
            spin = QSpinBox(self)
            spin.setRange(int(low), int(high))
            spin.setSingleStep(int(step))
            if suffix:
                spin.setSuffix(suffix)
            spin.setKeyboardTracking(False)
            return spin

        def _dspin(self, low: float, high: float, decimals: int = 2,
                   step: float = 0.1, suffix: str = "") -> QDoubleSpinBox:
            spin = QDoubleSpinBox(self)
            spin.setDecimals(int(decimals))
            spin.setRange(float(low), float(high))
            spin.setSingleStep(float(step))
            if suffix:
                spin.setSuffix(suffix)
            spin.setKeyboardTracking(False)
            return spin

        def _opacity_row(self, initial: float) -> Tuple[QWidget, QSlider, QLabel]:
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(_qt_const("Orientation", "Horizontal",
                                       "Horizontal") or 1, holder)
            slider.setRange(0, OPACITY_SLIDER_MAX)
            slider.setValue(int(round(float(initial) * OPACITY_SLIDER_MAX)))
            value = QLabel(f"{slider.value()}%", holder)
            value.setMinimumWidth(46)
            slider.valueChanged.connect(
                lambda raw, label=value: label.setText(f"{int(raw)}%"))
            row.addWidget(slider, 1)
            row.addWidget(value, 0)
            return holder, slider, value

        def _slider_row(self, low: int, high: int, initial: int,
                        suffix: str = "", step: int = 1
                        ) -> Tuple[QWidget, QSlider, QLabel]:
            """
            Integer slider plus its live value label (item 3/4 controls).

            ``_opacity_row`` is the percentage flavour; this one carries any
            unit (px / ms) and preserves a step so coarse ranges stay usable.
            """
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(_qt_const("Orientation", "Horizontal",
                                       "Horizontal") or 1, holder)
            slider.setRange(int(low), int(high))
            slider.setSingleStep(max(1, int(step)))
            slider.setPageStep(max(1, int(step) * 5))
            slider.setValue(max(int(low), min(int(high), int(initial))))
            value = QLabel(f"{slider.value()}{suffix}", holder)
            value.setMinimumWidth(56)
            slider.valueChanged.connect(
                lambda raw, label=value, unit=suffix: label.setText(
                    f"{int(raw)}{unit}"))
            row.addWidget(slider, 1)
            row.addWidget(value, 0)
            return holder, slider, value

        def _mirror(self, spin: Any, slider: Any) -> None:
            """
            Two-way sync between a spin box and a slider (0.1.17 item 3).

            Both widgets own the same value, so each change is pushed to the
            other only when it actually differs - that keeps the pair from
            bouncing and lets either one be the one the user touches.
            """
            def from_spin(raw: int) -> None:
                if slider.value() != int(raw):
                    slider.setValue(int(raw))

            def from_slider(raw: int) -> None:
                if spin.value() != int(raw):
                    spin.setValue(int(raw))

            spin.valueChanged.connect(from_spin)
            slider.valueChanged.connect(from_slider)

        def _spin_slider(self, low: int, high: int, initial: int,
                         suffix: str = "", step: int = 1) -> Tuple[QWidget, Any]:
            """
            Slider + spin box row (0.1.17 item 3 panel geometry).

            The spin box is returned as the authoritative widget so the value
            stays exact (a slider alone would quantise a hand-written config);
            the slider is the grab-and-drag control the item asks for.
            """
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(_qt_const("Orientation", "Horizontal",
                                       "Horizontal") or 1, holder)
            slider.setRange(int(low), int(high))
            slider.setSingleStep(max(1, int(step)))
            slider.setPageStep(max(1, int(step) * 5))
            spin = QSpinBox(holder)
            spin.setRange(int(low), int(high))
            spin.setSingleStep(max(1, int(step)))
            if suffix:
                spin.setSuffix(suffix)
            spin.setKeyboardTracking(False)
            spin.setValue(max(int(low), min(int(high), int(initial))))
            slider.setValue(spin.value())
            self._mirror(spin, slider)
            row.addWidget(slider, 1)
            row.addWidget(spin, 0)
            return holder, spin

        def _on_size_preset(self, _index: int = 0) -> None:
            """
            Apply the chosen panel preset to the geometry controls (item 3).

            The preset is a shortcut, not a mode: the geometry still lives in
            overlay.width / overlay.height / overlay.corner_radius, so the
            sliders keep working afterwards and a hand-edited config is never
            overridden behind the user's back.
            """
            name = str(self._size_preset.currentData() or DEFAULT_SIZE_PRESET)
            width, height, radius = preset_geometry(name)
            for widget, value in ((self._overlay_width, width),
                                  (self._overlay_height, height),
                                  (self._corner_radius, radius)):
                widget.blockSignals(True)
                widget.setValue(int(value))
                widget.blockSignals(False)

        def _color_row(self, initial: str) -> Tuple[QWidget, QLineEdit]:
            """BL-1: a #RRGGBB text field with a colour-picker button."""
            holder = QWidget(self)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            field = QLineEdit(holder)
            field.setText(str(initial or ""))
            field.setMaxLength(9)
            button = QPushButton(holder)
            self._bind_text(button, "settings.overlay_pick_color", _set_button_text)
            button.clicked.connect(
                lambda _checked=False, target=field: self._pick_color(target))
            row.addWidget(field, 1)
            row.addWidget(button, 0)
            return holder, field

        def _pick_color(self, field: QLineEdit) -> None:
            """Open the colour dialog and write the result back as #RRGGBB."""
            from PySide6.QtGui import QColor  # noqa: PLC0415 - optional dependency
            try:
                current = QColor(str(field.text() or "") or "#FFFFFF")
            except Exception:                                  # noqa: BLE001
                current = QColor("#FFFFFF")
            chosen = QColorDialog.getColor(
                current, self, self._text("settings.overlay_font_color"))
            if chosen.isValid():
                field.setText(chosen.name().upper())

        def _combo(self, options: Sequence[Tuple[str, str]]) -> QComboBox:
            combo = QComboBox(self)
            for value, key in options:
                combo.addItem(key, value)                   # text set by retranslate
            self._combo_bindings.append((combo, options))
            return combo

        def _bind_text(self, widget: Any, key: str,
                       setter: Callable[[Any, str], None]) -> None:
            self._text_bindings.append((widget, key, setter))

        # -- tabs ---------------------------------------------------------- #

        def _build_general_tab(self) -> QWidget:
            page, box = self._page()
            # 0.1.19: the work mode sits above everything else because switching
            # it rewrites the recommended rows further down.
            mode_form = self._form(box, "settings.group.mode")
            self._mode = self._combo(_mode_options())
            self._mode.currentIndexChanged.connect(self._on_mode_changed)
            self._label(mode_form, self._mode, "settings.mode", self)
            self._mode_hint = QLabel(self)
            self._mode_hint.setWordWrap(True)
            self._bind_text(self._mode_hint, "settings.mode.tip", _set_label_text)
            mode_form.addRow(self._mode_hint)
            self._mode_summary = QLabel(self)
            self._mode_summary.setWordWrap(True)
            mode_form.addRow(self._mode_summary)
            self._mode_auto = QCheckBox(self)
            self._bind_text(self._mode_auto, "settings.mode.auto_params", _set_button_text)
            mode_form.addRow(self._mode_auto)
            self._mode_auto_hint = QLabel(self)
            self._mode_auto_hint.setWordWrap(True)
            self._bind_text(self._mode_auto_hint, "settings.mode.auto_params.tip",
                            _set_label_text)
            mode_form.addRow(self._mode_auto_hint)

            general = self._form(box, "settings.group.general")
            self._language = QComboBox(self)
            for value, label in UI_LANGUAGE_LABELS:
                self._language.addItem(label, value)
            self._language.currentIndexChanged.connect(self._on_language_changed)
            self._label(general, self._language, "settings.language", self)

            self._fps_safe = QCheckBox(self)
            self._bind_text(self._fps_safe, "settings.fps_safe_mode",
                            _set_button_text)
            general.addRow(self._fps_safe)
            self._fps_hint = QLabel(self)
            self._fps_hint.setWordWrap(True)
            self._bind_text(self._fps_hint, "settings.fps_safe_hint", _set_label_text)
            general.addRow(self._fps_hint)
            box.addStretch(1)
            return page

        def _build_hotkey_tab(self) -> QWidget:
            page, box = self._page()
            form = self._form(box, "settings.group.hotkeys")
            self._hotkey_area = HotkeyEdit("", self)
            self._hotkey_full = HotkeyEdit("", self)
            self._hotkey_toggle = HotkeyEdit("", self)
            self._hotkey_region = HotkeyEdit("", self)
            self._label(form, self._hotkey_area, "settings.hotkey.area_ocr", self)
            self._label(form, self._hotkey_full, "settings.hotkey.fullscreen_ocr", self)
            self._label(form, self._hotkey_toggle, "settings.hotkey.toggle_asr", self)
            toggle_hint = QLabel(self)
            toggle_hint.setWordWrap(True)
            self._bind_text(toggle_hint, "settings.hotkey.toggle_hint", _set_label_text)
            form.addRow(toggle_hint)
            self._label(form, self._hotkey_region, "settings.hotkey.toggle_region", self)
            region_hint = QLabel(self)
            region_hint.setWordWrap(True)
            self._bind_text(region_hint, "settings.hotkey.region_hint", _set_label_text)
            form.addRow(region_hint)
            hint = QLabel(self)
            hint.setWordWrap(True)
            self._bind_text(hint, "settings.hotkey.hint", _set_label_text)
            form.addRow(hint)
            capture_hint = QLabel(self)
            capture_hint.setWordWrap(True)
            self._bind_text(capture_hint, "settings.shortcut_hint", _set_label_text)
            form.addRow(capture_hint)
            box.addStretch(1)
            return page

        def _build_asr_tab(self) -> QWidget:
            page, box = self._page()
            form = self._form(box, "settings.group.asr")
            model_row = QWidget(self)
            model_layout = QHBoxLayout(model_row)
            model_layout.setContentsMargins(0, 0, 0, 0)
            self._model_path = QLineEdit(self)
            self._model_path.setReadOnly(True)
            self._model_browse = QPushButton(self)
            self._model_browse.clicked.connect(self._on_browse_model)
            self._bind_text(self._model_browse, "settings.model_browse",
                            _set_button_text)
            model_layout.addWidget(self._model_path, 1)
            model_layout.addWidget(self._model_browse, 0)
            self._label(form, model_row, "settings.model_path", self)
            self._model_status = QLabel(self)
            self._model_status.setWordWrap(True)
            form.addRow(self._model_status)

            # Model size dropdown: "" keeps using the folder above, a concrete
            # size lets main.py resolve its own bundled/downloaded model. This
            # control never rewrites ``asr.model_path``.
            self._model_size = QComboBox(self)
            for _value, _label in ASR_MODEL_SIZES:
                self._model_size.addItem(_label, _value)
            self._label(form, self._model_size, "settings.asr_model_size", self)
            self._apply_model_size_tips()
            self._model_size.currentIndexChanged.connect(self._refresh_model_size_tip)
            self._model_size_hint = QLabel(self)
            self._model_size_hint.setWordWrap(True)
            self._model_size_hint.setStyleSheet("color: gray;")
            self._bind_text(self._model_size_hint, "settings.asr_model_size.hint",
                            _set_label_text)
            form.addRow(self._model_size_hint)

            self._asr_language = self._combo(ASR_LANGUAGES)
            self._label(form, self._asr_language, "settings.asr_language", self)
            self._vad_mode = self._combo(VAD_MODES)
            self._label(form, self._vad_mode, "settings.vad_mode", self)
            self._vad_min_speech = self._spin(50, 5000)
            self._label(form, self._vad_min_speech, "settings.asr_vad_min_speech", self)
            self._vad_hint_label = QLabel(self)
            self._vad_hint_label.setWordWrap(True)
            self._vad_min_speech.valueChanged.connect(self._vad_hint_refresh)
            form.addRow(self._vad_hint_label)
            self._vad_threshold = self._dspin(-80.0, -10.0, 1, 1.0)
            self._label(form, self._vad_threshold,
                        "settings.asr_vad_threshold", self)
            self._beam_size = self._spin(1, 10)
            self._label(form, self._beam_size, "settings.asr_beam_size", self)

            # Read-only status line: which voice-activity backend is configured
            # (config key ``audio.vad_engine``). No new key, no writes. The hover
            # tip explains all three engines; it is text only.
            self._vad_engine_value = QLabel(self)
            self._vad_engine_value.setEnabled(False)
            self._label(form, self._vad_engine_value, "settings.asr_vad_engine", self)
            self._refresh_vad_engine_tip()

            # BL-5: the three audio safety knobs live on the Advanced page now.
            # 0.1.19: capture source / mixing / noise / burst and the
            # backpressure switch. Everything here is written as a dotted key and
            # pushed into the live capture worker without a restart.
            capture = self._form(box, "settings.group.capture")
            self._audio_source = self._combo(_source_options())
            self._label(capture, self._audio_source, "settings.audio.source", self)
            source_hint = QLabel(self)
            source_hint.setWordWrap(True)
            self._bind_text(source_hint, "settings.audio.source.tip", _set_label_text)
            capture.addRow(source_hint)

            self._mic_device = QLineEdit(self)
            self._mic_device.setPlaceholderText(
                self._text("settings.audio.mic_device.tip"))
            self._label(capture, self._mic_device, "settings.audio.mic_device", self)

            gain_tip = QLabel(self)
            gain_tip.setWordWrap(True)
            self._bind_text(gain_tip, "settings.audio.gain.tip", _set_label_text)
            capture.addRow(gain_tip)
            self._mic_gain = self._dspin(0.0, 4.0, 2, 0.05)
            self._label(capture, self._mic_gain, "settings.audio.mic_gain", self)
            self._speaker_gain = self._dspin(0.0, 4.0, 2, 0.05)
            self._label(capture, self._speaker_gain, "settings.audio.speaker_gain", self)

            self._noise_on = QCheckBox(self)
            self._bind_text(self._noise_on, "settings.audio.noise_suppression",
                            _set_button_text)
            capture.addRow(self._noise_on)
            noise_hint = QLabel(self)
            noise_hint.setWordWrap(True)
            self._bind_text(noise_hint, "settings.audio.noise_suppression.tip",
                            _set_label_text)
            capture.addRow(noise_hint)
            self._noise_db = self._dspin(3.0, 40.0, 1, 1.0)
            self._label(capture, self._noise_db, "settings.audio.noise_reduction_db", self)

            self._burst_on = QCheckBox(self)
            self._bind_text(self._burst_on, "settings.audio.burst_filter",
                            _set_button_text)
            capture.addRow(self._burst_on)
            burst_hint = QLabel(self)
            burst_hint.setWordWrap(True)
            self._bind_text(burst_hint, "settings.audio.burst_filter.tip", _set_label_text)
            capture.addRow(burst_hint)
            self._burst_ms = self._dspin(2.0, 200.0, 1, 5.0)
            self._label(capture, self._burst_ms, "settings.audio.burst_max_ms", self)
            self._burst_margin = self._dspin(6.0, 40.0, 1, 1.0)
            self._label(capture, self._burst_margin,
                        "settings.audio.burst_margin_db", self)

            self._pause_on_full = QCheckBox(self)
            self._bind_text(self._pause_on_full, "settings.pause_on_full",
                            _set_button_text)
            capture.addRow(self._pause_on_full)
            pause_hint = QLabel(self)
            pause_hint.setWordWrap(True)
            self._bind_text(pause_hint, "settings.pause_on_full.tip", _set_label_text)
            capture.addRow(pause_hint)

            box.addStretch(1)
            return page

        def _build_ocr_tab(self) -> QWidget:
            page, box = self._page()
            form = self._form(box, "settings.group.ocr")

            self._winrt_auto = QCheckBox(self)
            self._bind_text(self._winrt_auto, "settings.ocr_winrt_auto",
                            _set_button_text)
            form.addRow(self._winrt_auto)

            self._winrt_language = self._combo(WINRT_LANGUAGES)
            self._label(form, self._winrt_language, "settings.ocr_winrt_language", self)
            self._winrt_auto.toggled.connect(
                lambda checked: self._winrt_language.setDisabled(bool(checked)))

            self._area_fps_normal = self._dspin(0.1, 30.0, 2, 0.1)
            self._label(form, self._area_fps_normal,
                        "settings.ocr_area_fps_normal", self)
            self._area_fps_safe = self._dspin(0.05, 5.0, 2, 0.05)
            self._label(form, self._area_fps_safe,
                        "settings.ocr_area_fps_safe", self)
            self._fullscreen_interval = self._spin(800, 60000)
            self._label(form, self._fullscreen_interval,
                        "settings.ocr_fullscreen_interval", self)

            # BL-3: line filtering. The slider is kept (the checklist asks for
            # it) but Windows OCR reports no confidence, so it can only ever
            # affect engines that score their lines - the hint says so, and
            # the height / character pair below is what really filters.
            self._ocr_confidence_holder, self._ocr_confidence, _value = (
                self._opacity_row(0.0))
            self._label(form, self._ocr_confidence_holder,
                        "settings.ocr_confidence_min", self)
            self._ocr_min_line_height = self._dspin(0.0, 500.0, 1, 1.0)
            self._label(form, self._ocr_min_line_height,
                        "settings.ocr_min_line_height", self)
            self._ocr_min_line_chars = self._spin(0, 500)
            self._label(form, self._ocr_min_line_chars,
                        "settings.ocr_min_line_chars", self)

            # Fixed-region OCR (v0.1.13): one saved rectangle, redrawn on screen.
            # The geometry itself is not typed into the panel; the picker is the
            # only writer, and this row only shows the saved value.
            fixed_row = QWidget(self)
            fixed_layout = QHBoxLayout(fixed_row)
            fixed_layout.setContentsMargins(0, 0, 0, 0)
            self._fixed_region_value = QLineEdit(self)
            self._fixed_region_value.setReadOnly(True)
            self._fixed_region_pick = QPushButton(self)
            self._fixed_region_pick.clicked.connect(self._on_pick_fixed_region)
            self._bind_text(self._fixed_region_pick, "settings.ocr_fixed_region_pick",
                            _set_button_text)
            fixed_layout.addWidget(self._fixed_region_value, 1)
            fixed_layout.addWidget(self._fixed_region_pick, 0)
            self._label(form, fixed_row, "settings.ocr_fixed_region", self)
            self._fixed_region_enabled = QCheckBox(self)
            self._bind_text(self._fixed_region_enabled, "settings.ocr_region_enabled",
                            _set_button_text)
            form.addRow(self._fixed_region_enabled)
            self._region_tolerance = self._spin(1, 64)
            self._label(form, self._region_tolerance,
                        "settings.ocr_region_tolerance", self)
            fixed_hint = QLabel(self)
            fixed_hint.setWordWrap(True)
            self._bind_text(fixed_hint, "settings.ocr_fixed_region_hint", _set_label_text)
            form.addRow(fixed_hint)
            conf_hint = QLabel(self)
            conf_hint.setWordWrap(True)
            self._bind_text(conf_hint, "settings.ocr_confidence_hint",
                            _set_label_text)
            form.addRow(conf_hint)

            hint = QLabel(self)
            hint.setWordWrap(True)
            self._bind_text(hint, "settings.ocr_winrt_hint", _set_label_text)
            form.addRow(hint)
            box.addStretch(1)
            return page

        def _build_translate_tab(self) -> QWidget:
            page, box = self._page()
            form = self._form(box, "settings.group.translate")
            self._source_lang = self._combo(SOURCE_LANGUAGES)
            self._label(form, self._source_lang, "settings.translate_source", self)
            self._target_lang = self._combo(TARGET_LANGUAGES)
            self._label(form, self._target_lang, "settings.translate_target", self)
            # BL-5: timeouts, circuit breaker and cache moved to the
            # "Queue and limits" / "Advanced" pages.
            self._show_original = QCheckBox(self)
            self._bind_text(self._show_original, "settings.show_original",
                            _set_button_text)
            form.addRow(self._show_original)
            hint = QLabel(self)
            hint.setWordWrap(True)
            self._bind_text(hint, "settings.show_original_hint", _set_label_text)
            form.addRow(hint)
            self._libre_base = QLineEdit(self)
            self._label(form, self._libre_base,
                        "settings.translate_libre_base", self)
            libre_hint = QLabel(self)
            libre_hint.setWordWrap(True)
            self._bind_text(libre_hint, "settings.translate_libre_hint",
                            _set_label_text)
            form.addRow(libre_hint)

            # v0.1.11: logon auto-start of the local service. The folder row is a
            # plain path field (no ASCII restriction: it only feeds a .bat call).
            self._libre_autostart = QCheckBox(self)
            self._bind_text(self._libre_autostart,
                            "settings.translate_libre_autostart",
                            _set_button_text)
            form.addRow(self._libre_autostart)
            self._libre_autostart_hint = QLabel(self)
            self._libre_autostart_hint.setWordWrap(True)
            self._bind_text(self._libre_autostart_hint,
                            "settings.translate_libre_autostart_hint",
                            _set_label_text)
            form.addRow(self._libre_autostart_hint)

            libre_dir_row = QWidget(self)
            libre_dir_layout = QHBoxLayout(libre_dir_row)
            libre_dir_layout.setContentsMargins(0, 0, 0, 0)
            self._libre_dir = QLineEdit(self)
            self._libre_dir.setReadOnly(True)
            self._libre_browse = QPushButton(self)
            self._libre_browse.clicked.connect(self._on_browse_libre)
            self._bind_text(self._libre_browse, "settings.libre_browse",
                            _set_button_text)
            libre_dir_layout.addWidget(self._libre_dir, 1)
            libre_dir_layout.addWidget(self._libre_browse, 0)
            self._label(form, libre_dir_row, "settings.translate_libre_dir", self)
            self._libre_dir_hint = QLabel(self)
            self._libre_dir_hint.setWordWrap(True)
            self._bind_text(self._libre_dir_hint,
                            "settings.translate_libre_dir_hint", _set_label_text)
            form.addRow(self._libre_dir_hint)
            self._libre_status = QLabel(self)
            self._libre_status.setWordWrap(True)
            form.addRow(self._libre_status)

            self._mymemory_email = QLineEdit(self)
            self._label(form, self._mymemory_email,
                        "settings.translate_mymemory_email", self)
            email_hint = QLabel(self)
            email_hint.setWordWrap(True)
            self._bind_text(email_hint, "settings.translate_mymemory_hint",
                            _set_label_text)
            form.addRow(email_hint)
            box.addStretch(1)
            return page

        def _build_subtitle_tab(self) -> QWidget:
            page, box = self._page()
            form = self._form(box, "settings.group.subtitle")
            self._font_family = QComboBox(self)
            self._font_family.setEditable(True)
            for family in FONT_FAMILIES:
                self._font_family.addItem(family, family)
            self._label(form, self._font_family, "settings.font_family", self)
            self._font_size = self._spin(8, 96)
            self._label(form, self._font_size, "settings.font_size", self)
            # Item 3: let the glyph size follow the panel size.
            self._font_scale_with_panel = QCheckBox(self)
            self._bind_text(self._font_scale_with_panel,
                            "settings.font_scale_with_panel", _set_button_text)
            self._bind_text(self._font_scale_with_panel,
                            "settings.font_scale_with_panel.tip", _set_tooltip_text)
            form.addRow(self._font_scale_with_panel)

            text_row, self._text_opacity, _ = self._opacity_row(1.0)
            self._label(form, text_row, "settings.text_opacity", self)
            mask_row, self._mask_opacity, _ = self._opacity_row(0.35)
            self._label(form, mask_row, "settings.mask_opacity", self)
            self._mask_hint = QLabel(self)
            self._mask_hint.setWordWrap(True)
            self._bind_text(self._mask_hint, "settings.mask_opacity.hint",
                            _set_label_text)
            form.addRow("", self._mask_hint)
            # Item 4-1: one slider drives fade in *and* fade out; 0 = no fade.
            fade_row, self._fade_ms, _ = self._slider_row(0, 800, 180, " ms",
                                                         step=10)
            self._label(form, fade_row, "settings.fade_duration", self)
            self._fade_hint = QLabel(self)
            self._fade_hint.setWordWrap(True)
            self._bind_text(self._fade_hint, "settings.fade_duration.hint", _set_label_text)
            form.addRow("", self._fade_hint)
            # BL-5: queue length / timeout moved to the "Queue and limits" page.

            overlay_form = self._form(box, "settings.group.overlay")
            # Item 3: preset first — it is the one-click way to size the panel,
            # the width/height/radius sliders below stay the fine control.
            self._size_preset = self._combo([
                ("default", "settings.size_preset.default"),
                ("compact", "settings.size_preset.compact"),
                ("square", "settings.size_preset.square"),
            ])
            self._label(overlay_form, self._size_preset, "settings.size_preset", self)
            self._bind_text(self._size_preset, "settings.size_preset.tip",
                            _set_tooltip_text)
            self._size_preset.currentIndexChanged.connect(self._on_size_preset)
            self._always_on_top = QCheckBox(self)
            self._bind_text(self._always_on_top, "settings.overlay_top",
                            _set_button_text)
            overlay_form.addRow(self._always_on_top)
            self._click_through = QCheckBox(self)
            self._bind_text(self._click_through, "settings.overlay_click_through",
                            _set_button_text)
            overlay_form.addRow(self._click_through)
            color_row, self._text_color = self._color_row("#FFFFFF")
            self._label(overlay_form, color_row,
                        "settings.overlay_font_color", self)
            # Item 4-2: outline thickness (0 = no outline).
            outline_row, self._outline_width, _ = self._slider_row(0, 8, 2, " px")
            self._label(overlay_form, outline_row,
                        "settings.overlay_outline_width", self)
            self._bind_text(self._outline_width,
                            "settings.overlay_outline_width.tip",
                            _set_tooltip_text)
            self._lock_position = QCheckBox(self)
            self._bind_text(self._lock_position, "settings.overlay_lock_position",
                            _set_button_text)
            overlay_form.addRow(self._lock_position)
            # Item 3: 宽度/高度滑块 —— 拖动自由拉伸面板尺寸，右侧数字框保留精确值。
            width_row, self._overlay_width = self._spin_slider(200, 8000, 900,
                                                               " px", step=10)
            self._label(overlay_form, width_row, "settings.overlay_width", self)
            height_row, self._overlay_height = self._spin_slider(60, 4000, 220,
                                                                 " px", step=10)
            self._label(overlay_form, height_row, "settings.overlay_height", self)
            # Item 3: 字幕文字大小独立滑块（与上方「字号」数字框双向同步）。
            font_row, self._font_size_slider, _ = self._slider_row(
                8, 96, self._font_size.value(), " px")
            self._mirror(self._font_size, self._font_size_slider)
            self._label(overlay_form, font_row, "settings.font_size", self)
            # Item 3: corner radius, 0 = square corners.
            radius_row, self._corner_radius, _ = self._slider_row(0, 48, 14, " px")
            self._label(overlay_form, radius_row,
                        "settings.corner_radius", self)
            self._bind_text(self._corner_radius, "settings.corner_radius.tip",
                            _set_tooltip_text)
            self._reset_position = QPushButton(self)
            self._reset_position.clicked.connect(self._on_reset_position)
            self._bind_text(self._reset_position, "settings.overlay_reset_position",
                            _set_button_text)
            overlay_form.addRow(self._reset_position)
            box.addStretch(1)
            return page

        def _build_queue_tab(self) -> QWidget:
            """BL-5 page 7: subtitle queue + translation cache limits."""
            page, box = self._page()
            form = self._form(box, "settings.group.queue")
            self._queue_max = self._spin(1, SINGLE_QUEUE_MAX)
            self._label(form, self._queue_max, "settings.subtitle_queue_max", self)
            self._subtitle_timeout = self._dspin(1.0, 60.0, 1, 1.0)
            self._label(form, self._subtitle_timeout,
                        "settings.subtitle_timeout", self)

            cache_form = self._form(box, "settings.group.cache")
            self._cache_ttl = self._dspin(0.0, 3600.0, 0, 30.0)
            self._label(cache_form, self._cache_ttl,
                        "settings.translate_cache_ttl", self)
            self._cache_max = self._spin(0, 4096)
            self._label(cache_form, self._cache_max,
                        "settings.translate_cache_max", self)
            box.addStretch(1)
            return page

        def _build_advanced_tab(self) -> QWidget:
            """BL-5 page 8: translation resilience + audio safety knobs.

            Nothing new is exposed here - these are the very same widgets that
            used to sit on the Translation and ASR pages.
            """
            page, box = self._page()
            form = self._form(box, "settings.group.resilience")
            self._timeout = self._dspin(0.5, 30.0, 1, 0.5)
            self._label(form, self._timeout, "settings.translate_timeout", self)
            self._failure_threshold = self._spin(1, 20)
            self._label(form, self._failure_threshold,
                        "settings.translate_threshold", self)
            self._circuit_break = self._dspin(1.0, 300.0, 1, 1.0)
            self._label(form, self._circuit_break, "settings.translate_circuit", self)

            audio_form = self._form(box, "settings.group.audio")
            self._spike_filter = self._spin(0, 100)
            self._label(audio_form, self._spike_filter,
                        "settings.audio_spike_filter", self)
            self._reconnect = self._spin(0, 50)
            self._label(audio_form, self._reconnect, "settings.audio_reconnect", self)
            self._silence_log = self._spin(0, 3600)
            self._label(audio_form, self._silence_log,
                        "settings.audio_silence_log", self)

            # Item 4: log level. "Warnings only" is the default, so an ordinary
            # session prints only warnings and errors instead of a debug flood.
            log_form = self._form(box, "settings.group.logging")
            self._log_level = self._combo([
                ("debug", "settings.log_level.debug"),
                ("info", "settings.log_level.info"),
                ("warning", "settings.log_level.warning"),
                ("error", "settings.log_level.error"),
            ])
            self._label(log_form, self._log_level, "settings.log_level", self)
            self._bind_text(self._log_level, "settings.log_level.tip",
                            _set_tooltip_text)

            # 0.1.18: start with Windows. Off by default (the installer says the
            # same) and it writes the same kind of Startup-folder entry the
            # LibreTranslate option uses, so no administrator rights are needed.
            app_form = self._form(box, "settings.group.application")
            self._autostart = QCheckBox(self)
            self._bind_text(self._autostart, "settings.autostart",
                            _set_button_text)
            app_form.addRow(self._autostart)
            self._bind_text(self._autostart, "settings.autostart.tip",
                            _set_tooltip_text)
            box.addStretch(1)
            return page

        def _build_about_tab(self) -> QWidget:
            """BL-5 page 9: about / privacy, the v0.2 roadmap and maintenance.

            The disclaimer body is re-used as the privacy statement: it already
            describes exactly what the tool touches. No config key is read or
            written here.
            """
            page, box = self._page()

            about_form = self._form(box, "settings.group.about")
            self._about_version = QLabel(self)
            self._about_version.setTextInteractionFlags(
                _qt_const("TextInteractionFlag", "TextSelectableByMouse",
                          "TextSelectableByMouse") or 0)
            about_form.addRow(self._about_version)
            self._about_statement = QLabel(self)
            self._about_statement.setWordWrap(True)
            self._bind_text(self._about_statement, "disclaimer.body",
                            _set_label_text)
            about_form.addRow(self._about_statement)

            privacy_form = self._form(box, "settings.group.privacy")
            self._privacy_note = QLabel(self)
            self._privacy_note.setWordWrap(True)
            self._bind_text(self._privacy_note, "settings.privacy_note",
                            _set_label_text)
            privacy_form.addRow(self._privacy_note)
            self._privacy_risk = QLabel(self)
            self._privacy_risk.setWordWrap(True)
            self._bind_text(self._privacy_risk, "disclaimer.risk", _set_label_text)
            privacy_form.addRow(self._privacy_risk)

            group = QGroupBox(self)
            self._bind_text(group, "settings.group.v02", _set_group_title)
            roadmap = QFormLayout(group)
            for key in V02_PLACEHOLDERS:
                holder = QWidget(self)
                row = QHBoxLayout(holder)
                row.setContentsMargins(0, 0, 0, 0)
                check = QCheckBox(self)
                check.setEnabled(False)                     # UI placeholder only
                label = QLabel(self)
                self._bind_text(label, key, _set_label_text)
                check.setToolTip(config_text(self._config, "v02.todo"))
                label.setToolTip(check.toolTip())
                row.addWidget(check, 0)
                row.addWidget(label, 1)
                roadmap.addRow(holder)
            box.addWidget(group)

            form = self._form(box, "settings.group.maintenance")
            path_label = QLabel(str(_config_get(self._config, "__path__", "")) or
                                str(getattr(self._config, "path", "") or ""))
            path_label.setWordWrap(True)
            path_label.setTextInteractionFlags(
                _qt_const("TextInteractionFlag", "TextSelectableByMouse",
                          "TextSelectableByMouse") or 0)
            form.addRow(path_label)

            buttons = QWidget(self)
            row = QHBoxLayout(buttons)
            row.setContentsMargins(0, 0, 0, 0)
            self._btn_restart_tasks = QPushButton(self)
            self._btn_restart_tasks.clicked.connect(self._on_restart_tasks)
            self._bind_text(self._btn_restart_tasks, "settings.btn.restart_tasks",
                            _set_button_text)
            self._btn_restart_audio = QPushButton(self)
            self._btn_restart_audio.clicked.connect(self._on_restart_audio)
            self._bind_text(self._btn_restart_audio, "settings.btn.restart_audio",
                            _set_button_text)
            self._btn_restart_ocr = QPushButton(self)
            self._btn_restart_ocr.clicked.connect(self._on_restart_ocr)
            self._bind_text(self._btn_restart_ocr, "settings.btn.restart_ocr",
                            _set_button_text)
            self._btn_reset_defaults = QPushButton(self)
            self._btn_reset_defaults.clicked.connect(self._on_reset_defaults)
            self._bind_text(self._btn_reset_defaults, "settings.btn.reset_defaults",
                            _set_button_text)
            for button in (self._btn_restart_tasks, self._btn_restart_audio,
                           self._btn_restart_ocr, self._btn_reset_defaults):
                row.addWidget(button, 1)
            form.addRow(buttons)
            self._maintenance_status = QLabel(self)
            self._maintenance_status.setWordWrap(True)
            form.addRow(self._maintenance_status)
            box.addStretch(1)
            return page

        # -- text / language ----------------------------------------------- #

        def _text(self, key: str, **fmt: Any) -> str:
            return config_text(self._config, key, **fmt)

        def retranslate(self) -> None:
            """Re-apply every localised string from the current UI language."""
            for widget, key, setter in self._text_bindings:
                try:
                    setter(widget, self._text(key))
                except Exception:                              # noqa: BLE001
                    logger.debug("could not retranslate %s", key, exc_info=True)
            for index, key in self._tab_bindings:
                try:
                    self._tabs.setTabText(index, self._text(key))
                except Exception:                              # noqa: BLE001
                    logger.debug("could not retranslate tab %s", key, exc_info=True)
            for combo, options in self._combo_bindings:
                for position, (_value, key) in enumerate(options):
                    try:
                        combo.setItemText(position, self._text(key))
                    except Exception:                          # noqa: BLE001
                        logger.debug("could not retranslate %s", key, exc_info=True)
            fps = self._config_get_float("ocr.area_fps_safe", 0.5)
            self._fps_hint.setText(self._text("settings.fps_safe_hint", fps=fps))
            self._refresh_mode_summary()
            self._refresh_about_version()
            self._vad_hint_refresh()
            self._refresh_winrt_state()
            self.refresh_fixed_region()
            self._apply_model_size_tips()
            self._refresh_vad_engine_tip()

        def _refresh_about_version(self) -> None:
            """Product name + version on the About page (never translated)."""
            label = getattr(self, "_about_version", None)
            if label is None:
                return
            try:
                label.setText("%s %s" % (APP_NAME, APP_VERSION))
            except Exception:                                  # noqa: BLE001
                logger.debug("could not refresh about version", exc_info=True)

        def _vad_hint_refresh(self) -> None:
            text = self._text("settings.vad_hint",
                              ms=int(self._vad_min_speech.value()))
            if hasattr(self, "_vad_hint_label"):
                self._vad_hint_label.setText(text)

        def _apply_model_size_tips(self) -> None:
            """Attach one hover tip per model-size entry and refresh the combo."""
            combo = getattr(self, "_model_size", None)
            if combo is None:
                return
            for position, (_value, key) in enumerate(ASR_MODEL_SIZE_TIPS):
                try:
                    combo.setItemData(position, self._text(key), Qt.ToolTipRole)
                except Exception:                              # noqa: BLE001
                    logger.debug("could not tip model size %s", key, exc_info=True)
            self._refresh_model_size_tip()

        def _refresh_model_size_tip(self) -> None:
            """A closed combo shows the tip of the entry that is selected."""
            combo = getattr(self, "_model_size", None)
            if combo is None:
                return
            try:
                text = combo.itemData(int(combo.currentIndex()), Qt.ToolTipRole)
                combo.setToolTip(str(text or ""))
            except Exception:                                  # noqa: BLE001
                logger.debug("could not refresh model size tip", exc_info=True)

        def _refresh_vad_engine_tip(self) -> None:
            """Hover tip naming the active VAD engine plus all three options."""
            label = getattr(self, "_vad_engine_value", None)
            if label is None:
                return
            try:
                engine = str(label.text() or "auto")
                lines = [self._text("settings.asr_vad_engine.tip.header",
                                    engine=engine)]
                lines.extend(self._text(key) for key in VAD_ENGINE_TIPS)
                label.setToolTip("\n".join(lines))
            except Exception:                                  # noqa: BLE001
                logger.debug("could not refresh vad engine tip", exc_info=True)

        def _refresh_winrt_state(self) -> None:
            if hasattr(self, "_winrt_language"):
                self._winrt_language.setDisabled(bool(self._winrt_auto.isChecked()))

        def _config_get_float(self, key: str, default: float) -> float:
            try:
                return float(_config_get(self._config, key, default))
            except (TypeError, ValueError):
                return float(default)

        def _config_get_int(self, key: str, default: int) -> int:
            try:
                return int(_config_get(self._config, key, default))
            except (TypeError, ValueError):
                return int(default)

        def _config_get_bool(self, key: str, default: bool) -> bool:
            value = _config_get(self._config, key, default)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")

        def _set_combo_value(self, combo: QComboBox, value: Any) -> None:
            target = "" if value is None else str(value)
            index = combo.findData(target)
            if index < 0 and target:
                combo.addItem(target, target)
                index = combo.findData(target)
            combo.setCurrentIndex(max(0, index))

        # -- load / collect ------------------------------------------------ #

        def load_from_config(self) -> None:
            """Fill every widget from the config file without emitting signals."""
            self._language.blockSignals(True)
            self._set_combo_value(self._language,
                                  _config_get(self._config, "ui_language",
                                              DEFAULT_LANGUAGE))
            self._language.blockSignals(False)

            self._fps_safe.setChecked(self._config_get_bool("fps_safe_mode", False))

            # 0.1.19: the work mode drives the recommended rows; load it first and
            # refresh the summary without emitting mode_selected.
            self._mode.blockSignals(True)
            self._set_combo_value(self._mode,
                                  _config_get(self._config, "app.mode", "allround"))
            self._mode.blockSignals(False)
            self._mode_auto.setChecked(
                self._config_get_bool("app.mode_auto_params", True))
            self._refresh_mode_summary()

            self._hotkey_area.set_hotkey(
                _config_get(self._config, "hotkeys.area_ocr", ""), notify=False)
            self._hotkey_full.set_hotkey(
                _config_get(self._config, "hotkeys.fullscreen_ocr", ""), notify=False)
            self._hotkey_toggle.set_hotkey(
                _config_get(self._config, "hotkeys.toggle_asr", ""), notify=False)
            self._hotkey_region.set_hotkey(
                _config_get(self._config, "hotkeys.toggle_region_ocr", ""),
                notify=False)

            self._model_path.setText(
                str(_config_get(self._config, "asr.model_path", "") or ""))
            self._set_combo_value(self._model_size,
                                  _config_get(self._config, "asr.model_size", ""))
            self._vad_engine_value.setText(
                str(_config_get(self._config, "audio.vad_engine", "auto") or "auto"))
            self._refresh_vad_engine_tip()
            self._set_combo_value(self._asr_language,
                                  _config_get(self._config, "asr.language", "auto"))
            self._set_combo_value(self._vad_mode,
                                  _config_get(self._config, "asr.vad_mode", "single"))
            self._vad_min_speech.setValue(
                self._config_get_int("asr.vad_min_speech_ms", 300))
            self._vad_threshold.setValue(
                self._config_get_float("asr.vad_threshold_db", -45.0))
            self._beam_size.setValue(self._config_get_int("asr.beam_size", 5))
            self._spike_filter.setValue(
                self._config_get_int("audio.spike_filter_ms", 10))
            self._reconnect.setValue(
                self._config_get_int("audio.reconnect_attempts", 5))
            self._silence_log.setValue(
                self._config_get_int("audio.silence_log_seconds", 30))
            # 0.1.19 capture source / mixing / noise / burst / backpressure.
            self._set_combo_value(self._audio_source,
                                  _config_get(self._config, "audio.source", "speaker"))
            self._mic_device.setText(
                str(_config_get(self._config, "audio.mic_device", "") or ""))
            self._mic_gain.setValue(self._config_get_float("audio.mic_gain", 1.0))
            self._speaker_gain.setValue(
                self._config_get_float("audio.speaker_gain", 1.0))
            self._noise_on.setChecked(
                self._config_get_bool("audio.noise_suppression", False))
            self._noise_db.setValue(
                self._config_get_float("audio.noise_reduction_db", 12.0))
            self._burst_on.setChecked(
                self._config_get_bool("audio.burst_filter", False))
            self._burst_ms.setValue(self._config_get_float("audio.burst_max_ms", 60.0))
            self._burst_margin.setValue(
                self._config_get_float("audio.burst_margin_db", 10.0))
            self._pause_on_full.setChecked(
                self._config_get_bool("perf.pause_capture_on_full", True))
            # Item 4: log level, blockSignals because a combo change here is a
            # user action and must not be treated as one while loading.
            self._log_level.blockSignals(True)
            self._set_combo_value(
                self._log_level,
                str(_config_get(self._config, "logging.level", "warning")
                    or "warning").strip().lower())
            self._log_level.blockSignals(False)

            self._winrt_auto.setChecked(
                self._config_get_bool("ocr.winrt_language_auto", True))
            self._set_combo_value(self._winrt_language,
                                  _config_get(self._config, "ocr.winrt_language", ""))
            self._area_fps_normal.setValue(
                self._config_get_float("ocr.area_fps_normal", 5.0))
            self._area_fps_safe.setValue(
                self._config_get_float("ocr.area_fps_safe", 0.5))
            self._fullscreen_interval.setValue(
                self._config_get_int("ocr.fullscreen_min_interval_ms", 800))
            self._ocr_confidence.setValue(
                int(round(self._config_get_float("ocr.confidence_min", 0.0)
                          * OPACITY_SLIDER_MAX)))
            self._ocr_min_line_height.setValue(
                self._config_get_float("ocr.min_line_height", 0.0))
            self._ocr_min_line_chars.setValue(
                self._config_get_int("ocr.min_line_chars", 0))
            self._fixed_region_enabled.setChecked(
                self._config_get_bool("ocr.fixed_region_enabled", False))
            self._region_tolerance.setValue(
                self._config_get_int("ocr.region_change_tolerance", 8))
            self.refresh_fixed_region()

            self._set_combo_value(self._source_lang,
                                  _config_get(self._config, "translate.source_lang", "auto"))
            self._set_combo_value(self._target_lang,
                                  _config_get(self._config, "translate.target_lang", "zh-CN"))
            self._timeout.setValue(
                self._config_get_float("translate.timeout_seconds", 3.0))
            self._failure_threshold.setValue(
                self._config_get_int("translate.failure_threshold", 3))
            self._circuit_break.setValue(
                self._config_get_float("translate.circuit_break_seconds", 10.0))
            self._show_original.setChecked(
                self._config_get_bool("translate.show_original", False))
            self._libre_base.setText(
                str(_config_get(self._config, "translate.libre_base_url", "") or ""))
            self._libre_autostart.setChecked(
                self._config_get_bool("translate.libre_autostart", False))
            self._autostart.setChecked(
                self._config_get_bool("app.autostart", False))
            self._libre_dir.setText(self._resolve_libre_dir_for_panel(
                str(_config_get(self._config, "translate.libre_dir", "") or "")))
            self._refresh_libre_status()
            self._mymemory_email.setText(
                str(_config_get(self._config, "translate.mymemory_email", "") or ""))
            self._cache_ttl.setValue(
                self._config_get_float("translate.cache_ttl_seconds", 300.0))
            self._cache_max.setValue(
                self._config_get_int("translate.cache_max_entries", 256))

            family = str(_config_get(self._config, "subtitle.font_family",
                                     "Microsoft YaHei") or "")
            index = self._font_family.findData(family)
            if index < 0:
                self._font_family.addItem(family, family)
                index = self._font_family.findData(family)
            self._font_family.setCurrentIndex(max(0, index))
            self._font_size.setValue(self._config_get_int("subtitle.font_size", 22))
            self._font_scale_with_panel.setChecked(
                self._config_get_bool("subtitle.font_scale_with_panel", False))
            # One slider represents both fade durations: show the longer one so
            # a non-zero animation never looks switched off.
            self._fade_ms.setValue(max(
                self._config_get_int("subtitle.fade_in_ms", 180),
                self._config_get_int("subtitle.fade_out_ms", 220)))
            self._text_opacity.setValue(int(round(
                self._config_get_float("subtitle.text_opacity", 1.0) * 100)))
            self._mask_opacity.setValue(int(round(
                self._config_get_float("subtitle.mask_opacity", 0.35) * 100)))
            self._queue_max.setValue(
                self._config_get_int("subtitle.queue_max", SINGLE_QUEUE_MAX))
            self._subtitle_timeout.setValue(
                self._config_get_float("subtitle.timeout_seconds", 8.0))

            self._always_on_top.setChecked(
                self._config_get_bool("overlay.always_on_top", True))
            self._click_through.setChecked(
                self._config_get_bool("overlay.click_through", True))
            self._overlay_width.setValue(
                self._config_get_int("overlay.width", 900))
            self._overlay_height.setValue(
                self._config_get_int("overlay.height", 220))
            self._text_color.setText(
                str(_config_get(self._config, "overlay.text_color", "#FFFFFF")
                    or "#FFFFFF"))
            self._outline_width.setValue(
                self._config_get_int("overlay.outline_width", 2))
            self._corner_radius.setValue(
                self._config_get_int("overlay.corner_radius", 14))
            # Guard the preset signal: applying a preset here would overwrite
            # the width/height that were just read from the config.
            preset = str(_config_get(self._config, "overlay.size_preset",
                                     DEFAULT_SIZE_PRESET) or DEFAULT_SIZE_PRESET)
            preset_index = self._size_preset.findData(preset)
            self._size_preset.blockSignals(True)
            self._size_preset.setCurrentIndex(max(0, preset_index))
            self._size_preset.blockSignals(False)
            self._lock_position.setChecked(
                self._config_get_bool("overlay.lock_position", False))

            self._model_status.setText("")
            self._maintenance_status.setText("")
            self.retranslate()

        def collect(self) -> Dict[str, Any]:
            """Current widget values as dotted config keys."""
            return {
                "ui_language": self._language.currentData() or DEFAULT_LANGUAGE,
                "fps_safe_mode": self._fps_safe.isChecked(),
                "app.mode": self._mode.currentData() or "allround",
                "app.mode_auto_params": self._mode_auto.isChecked(),
                "hotkeys.area_ocr": self._hotkey_area.hotkey(),
                "hotkeys.fullscreen_ocr": self._hotkey_full.hotkey(),
                "hotkeys.toggle_asr": self._hotkey_toggle.hotkey(),
                "hotkeys.toggle_region_ocr": self._hotkey_region.hotkey(),
                "asr.model_path": self._model_path.text().strip(),
                "asr.model_size": self._model_size.currentData() or "",
                "asr.language": self._asr_language.currentData() or "auto",
                "asr.vad_mode": self._vad_mode.currentData() or "single",
                "asr.vad_min_speech_ms": int(self._vad_min_speech.value()),
                "asr.vad_threshold_db": float(self._vad_threshold.value()),
                "asr.beam_size": int(self._beam_size.value()),
                "audio.spike_filter_ms": int(self._spike_filter.value()),
                "audio.reconnect_attempts": int(self._reconnect.value()),
                "audio.silence_log_seconds": int(self._silence_log.value()),
                "logging.level": self._log_level.currentData() or "warning",
                "app.autostart": self._autostart.isChecked(),
                "audio.source": self._audio_source.currentData() or "speaker",
                "audio.mic_device": self._mic_device.text().strip(),
                "audio.mic_gain": round(float(self._mic_gain.value()), 2),
                "audio.speaker_gain": round(float(self._speaker_gain.value()), 2),
                "audio.noise_suppression": self._noise_on.isChecked(),
                "audio.noise_reduction_db": round(float(self._noise_db.value()), 1),
                "audio.burst_filter": self._burst_on.isChecked(),
                "audio.burst_max_ms": round(float(self._burst_ms.value()), 1),
                "audio.burst_margin_db": round(float(self._burst_margin.value()), 1),
                "perf.pause_capture_on_full": self._pause_on_full.isChecked(),
                "ocr.winrt_language_auto": self._winrt_auto.isChecked(),
                "ocr.winrt_language": self._winrt_language.currentData() or "",
                "ocr.area_fps_normal": float(self._area_fps_normal.value()),
                "ocr.area_fps_safe": float(self._area_fps_safe.value()),
                "ocr.fullscreen_min_interval_ms": int(self._fullscreen_interval.value()),
                "ocr.confidence_min": round(
                    float(self._ocr_confidence.value()) / OPACITY_SLIDER_MAX, 3),
                "ocr.min_line_height": float(self._ocr_min_line_height.value()),
                "ocr.min_line_chars": int(self._ocr_min_line_chars.value()),
                "ocr.fixed_region": str(
                    _config_get(self._config, "ocr.fixed_region", "") or "").strip(),
                "ocr.fixed_region_enabled": self._fixed_region_enabled.isChecked(),
                "ocr.region_change_tolerance": int(self._region_tolerance.value()),
                "translate.source_lang": self._source_lang.currentData() or "auto",
                "translate.target_lang": self._target_lang.currentData() or "zh-CN",
                "translate.timeout_seconds": float(self._timeout.value()),
                "translate.failure_threshold": int(self._failure_threshold.value()),
                "translate.circuit_break_seconds": float(self._circuit_break.value()),
                "translate.show_original": self._show_original.isChecked(),
                "translate.libre_base_url": self._libre_base.text().strip(),
                "translate.libre_dir": self._libre_dir.text().strip(),
                "translate.libre_autostart": self._libre_autostart.isChecked(),
                "translate.mymemory_email": self._mymemory_email.text().strip(),
                "translate.cache_ttl_seconds": float(self._cache_ttl.value()),
                "translate.cache_max_entries": int(self._cache_max.value()),
                "subtitle.font_family": (self._font_family.currentData()
                                         or self._font_family.currentText()),
                "subtitle.font_size": int(self._font_size.value()),
                "subtitle.font_scale_with_panel": self._font_scale_with_panel.isChecked(),
                "subtitle.text_opacity": round(
                    float(self._text_opacity.value()) / OPACITY_SLIDER_MAX, 3),
                "subtitle.mask_opacity": round(
                    float(self._mask_opacity.value()) / OPACITY_SLIDER_MAX, 3),
                # The single animation slider writes both fade durations.
                "subtitle.fade_in_ms": int(self._fade_ms.value()),
                "subtitle.fade_out_ms": int(self._fade_ms.value()),
                "subtitle.queue_max": int(self._queue_max.value()),
                "subtitle.timeout_seconds": float(self._subtitle_timeout.value()),
                "overlay.always_on_top": self._always_on_top.isChecked(),
                "overlay.click_through": self._click_through.isChecked(),
                "overlay.width": int(self._overlay_width.value()),
                "overlay.height": int(self._overlay_height.value()),
                "overlay.size_preset": (self._size_preset.currentData()
                                        or DEFAULT_SIZE_PRESET),
                "overlay.corner_radius": int(self._corner_radius.value()),
                "overlay.text_color": self._text_color.text().strip() or "#FFFFFF",
                "overlay.outline_width": int(self._outline_width.value()),
                # A zero thickness means "no outline" for the renderer.
                "overlay.outline": int(self._outline_width.value()) > 0,
                "overlay.lock_position": self._lock_position.isChecked(),
            }

        # -- validation / apply -------------------------------------------- #

        def validate(self) -> Tuple[bool, str]:
            """
            Check the hotkeys and the model folder.

            Returns (ok, reason). A conflict is reported with the localised
            ``dialog.hotkey_conflict.*`` message box and must stop the save.
            The ASR on/off hotkey is optional; when set it joins the same
            uniqueness test as the two OCR hotkeys.
            """
            area = self._hotkey_area.hotkey()
            fullscreen = self._hotkey_full.hotkey()
            toggle = self._hotkey_toggle.hotkey()
            region_toggle = self._hotkey_region.hotkey()
            if not area or not fullscreen:
                QMessageBox.warning(self, self._text("dialog.error.title"),
                                    self._text("settings.shortcut_hint"))
                return False, "hotkey-invalid"
            if check_hotkey_conflict(area, fullscreen, toggle, region_toggle):
                self.failed.emit("hotkey-conflict")
                QMessageBox.warning(
                    self, self._text("dialog.hotkey_conflict.title"),
                    self._text("dialog.hotkey_conflict.body",
                               keys=" / ".join(k for k in (area, fullscreen, toggle,
                                                           region_toggle) if k)))
                return False, "hotkey-conflict"
            model = self._model_path.text().strip()
            # Only judge the model folder while speech recognition is on: the
            # user may keep a stale path around precisely because ASR is off,
            # and refusing to save it would make the panel unusable.
            if model and self._asr_enabled:
                ok, reason = validate_model_dir(model)
                if not ok:
                    self.failed.emit(f"model:{reason}")
                    QMessageBox.warning(
                        self, self._text("settings.model_invalid_title"),
                        f"{self._text('settings.model_invalid_body')}\n\n({reason})")
                    return False, f"model:{reason}"
            libre_dir = self._libre_dir.text().strip()
            # 0.1.18: with app-only autostart the folder is not required, so an
            # unusable LibreTranslate directory is only validated when the user
            # actually asks for the service at logon (or keeps a path around).
            if self._libre_autostart.isChecked() or (libre_dir and not self._autostart.isChecked()):
                checker = (silent_launch.validate_libre_dir
                           if silent_launch is not None else None)
                ok, reason = (checker(libre_dir) if checker is not None
                              else (False, "module-missing"))
                if not ok:
                    self.failed.emit(f"libre:{reason}")
                    QMessageBox.warning(
                        self, self._text("settings.translate_libre_dir"),
                        f"{self._text('settings.libre_dir_invalid')}\n\n({reason})")
                    return False, f"libre:{reason}"
            return True, ""

        def apply(self, save: bool = True) -> bool:
            """
            Validate, write the config file, then push the values at runtime.

            The config file is only touched after validation passed, so a
            rejected hotkey combination can never reach disk.
            """
            ok, reason = self.validate()
            if not ok:
                logger.warning("settings were not applied: %s", reason)
                return False
            values = self.collect()
            previous_language = str(_config_get(self._config, "ui_language",
                                                DEFAULT_LANGUAGE))
            try:
                updater = getattr(self._config, "update", None)
                if callable(updater):
                    updater(values, save=bool(save))
                else:
                    setter = getattr(self._config, "set", None)
                    if callable(setter):
                        for key, value in values.items():
                            setter(key, value, save=False)
                    if save:
                        saver = getattr(self._config, "save", None)
                        if callable(saver):
                            saver()
            except Exception as exc:                            # noqa: BLE001
                logger.exception("the settings could not be written")
                QMessageBox.critical(self, self._text("dialog.error.title"),
                                     f"{exc.__class__.__name__}: {exc}")
                return False

            self._saved = True
            self._push_runtime()
            if callable(self._on_apply):
                try:
                    self._on_apply(values)
                except Exception:                              # noqa: BLE001
                    logger.exception("the settings callback failed")
            self.settings_applied.emit(values)
            if values.get("ui_language") != previous_language:
                self.ui_language_changed.emit(str(values["ui_language"]))
            return True

        def _push_runtime(self) -> None:
            """Apply the new values to the running components we were given."""
            for name, target in (("overlay", self._overlay),
                                 ("audio capture", self._audio_capture)):
                applier = getattr(target, "apply_config", None)
                if not callable(applier):
                    continue
                try:
                    applier()
                except Exception:                              # noqa: BLE001
                    logger.exception("%s could not apply the new settings", name)
            reloader = getattr(self._hotkey_manager, "reload", None)
            if callable(reloader):
                try:
                    reloader()
                except Exception:                              # noqa: BLE001
                    logger.exception("the hotkeys could not be reloaded")
            self._sync_libre_autostart()

        def _sync_libre_autostart(self) -> None:
            """Create or delete the logon entry so it matches the saved setting."""
            if silent_launch is None:
                return
            try:
                result = silent_launch.sync(self._config)
            except Exception:                                  # noqa: BLE001
                logger.exception("the logon entry could not be synchronised")
                self._libre_status.setText(
                    self._text("settings.libre_register_failed"))
                return
            logger.info("logon auto-start: %s", result)
            self._refresh_libre_status()
            if result.startswith("error:"):
                self._libre_status.setText(
                    "%s (%s)" % (self._text("settings.libre_register_failed"),
                                 result.split(":", 1)[1]))
            elif result == "removed":
                self._libre_status.setText(
                    self._text("settings.libre_unregister_ok"))
            elif result in ("written", "unchanged"):
                self._libre_status.setText(self._text("settings.libre_register_ok"))

        # -- slots ---------------------------------------------------------- #

        def _on_save(self) -> None:
            if self.apply(save=True):
                self.accept()
            # A failed validation keeps the panel open so the user can fix it.

        def _on_cancel(self) -> None:
            self.reject()

        def _on_mode_changed(self, _index: int) -> None:
            """
            0.1.19: switching the mode loads that mode's recommended parameters.

            The preset is written into the in-memory config at once (so the rows
            below show what will actually run), the panel reloads its widgets and
            the application is told to re-apply the runtime values without
            waiting for Save. Individual rows stay editable afterwards.
            """
            mode = str(self._mode.currentData() or "allround")
            setter = getattr(self._config, "set", None)
            if callable(setter):
                # The preset table deliberately does not carry app.mode itself:
                # the selected mode is recorded separately so a preset can never
                # switch the user into a different mode.
                try:
                    setter("app.mode", mode)
                except Exception:                              # noqa: BLE001
                    logger.debug("the work mode key could not be stored")
            if self._mode_auto.isChecked() and app_modes is not None:
                try:
                    applied = app_modes.apply_mode(self._config, mode)
                    logger.info("work mode %s loaded %d recommended value(s)",
                                mode, len(applied or {}))
                except Exception:                              # noqa: BLE001
                    logger.exception("the work mode preset could not be applied")
            self.load_from_config()
            self.mode_selected.emit(mode)

        def _refresh_mode_summary(self) -> None:
            """One-line description of the selected mode's recommended values."""
            label = getattr(self, "_mode_summary", None)
            if label is None:
                return
            mode = str(self._mode.currentData() or "allround")
            summary = ""
            if app_modes is not None:
                try:
                    summary = app_modes.mode_summary(self._config, mode)
                except Exception:                              # noqa: BLE001
                    logger.debug("mode summary failed", exc_info=True)
            try:
                label.setText(self._text("settings.mode.summary", summary=summary))
            except Exception:                                  # noqa: BLE001
                logger.debug("mode summary label failed", exc_info=True)

        def _on_language_changed(self, _index: int) -> None:
            """
            Live UI language switch: persist at once, then re-apply every string.

            The switch is visible immediately and stays even if the user leaves
            without pressing Save, because it is a display preference only.
            """
            language = str(self._language.currentData() or DEFAULT_LANGUAGE)
            if language not in SUPPORTED_LANGUAGES:
                language = DEFAULT_LANGUAGE
            setter = getattr(self._config, "set", None)
            if callable(setter):
                try:
                    setter("ui_language", language, save=True)
                except Exception:                              # noqa: BLE001
                    logger.exception("the UI language could not be saved")
            self.retranslate()
            self.ui_language_changed.emit(language)

        def _on_pick_fixed_region(self) -> None:
            """
            Ask the application to run the region picker.

            The dialog is modeless, so the overlay may sit on top of it; the
            application owns the picker and calls back through
            ``refresh_fixed_region`` once a rectangle was saved.
            """
            self.pick_requested.emit()

        def refresh_fixed_region(self) -> None:
            """Re-read the saved rectangle (called after an in-app pick)."""
            stored = str(_config_get(self._config, "ocr.fixed_region", "") or "").strip()
            if stored:
                self._fixed_region_value.setText(
                    self._text("settings.ocr_fixed_region_yes", region=stored))
            else:
                self._fixed_region_value.setText(
                    self._text("settings.ocr_fixed_region_none"))

        def _on_browse_model(self) -> None:
            start = self._model_path.text().strip() or os.path.expanduser("~")
            chosen = QFileDialog.getExistingDirectory(
                self, self._text("settings.model_path"), start)
            if not chosen:
                return
            ok, reason = validate_model_dir(chosen)
            self._model_path.setText(chosen)
            if ok:
                if self._warn_non_ascii_path(chosen):
                    self._model_status.setText(
                        self._text("settings.model_non_ascii_body"))
                else:
                    self._model_status.setText(self._text("settings.model_ok"))
            else:
                self._model_status.setText(self._text("settings.model_invalid_body"))
                QMessageBox.warning(self, self._text("settings.model_invalid_title"),
                                    f"{self._text('settings.model_invalid_body')}\n\n({reason})")

        def _on_browse_libre(self) -> None:
            """Pick the LibreTranslate folder (the one holding its start script)."""
            start = self._libre_dir.text().strip()
            if not start and silent_launch is not None:
                start = self._resolve_libre_dir_for_panel("")
            if not start:
                start = os.path.expanduser("~")
            chosen = QFileDialog.getExistingDirectory(
                self, self._text("settings.translate_libre_dir"), start)
            if not chosen:
                return
            self._libre_dir.setText(chosen)
            if silent_launch is None:
                ok, reason = False, "module-missing"
            else:
                ok, reason = silent_launch.validate_libre_dir(chosen)
            if ok:
                self._refresh_libre_status()
            else:
                self._libre_status.setText(
                    "%s (%s)" % (self._text("settings.libre_dir_invalid"), reason))
                QMessageBox.warning(
                    self, self._text("settings.translate_libre_dir"),
                    f"{self._text('settings.libre_dir_invalid')}\n\n({reason})")

        def _resolve_libre_dir_for_panel(self, stored: str) -> str:
            """The stored folder, else the first auto-detected candidate."""
            if stored:
                return stored
            if silent_launch is None:
                return ""
            try:
                return silent_launch.guess_libre_dir(self._config)
            except Exception:                                  # noqa: BLE001
                logger.debug("the LibreTranslate folder was not auto-detected",
                             exc_info=True)
                return ""

        def _refresh_libre_status(self) -> None:
            """Report whether the folder is usable and whether an entry exists."""
            if silent_launch is None:
                self._libre_status.setText("")
                return
            folder = self._libre_dir.text().strip()
            ok, reason = silent_launch.validate_libre_dir(folder)
            if ok:
                text = self._text("settings.libre_dir_ok")
            elif self._autostart.isChecked():
                # 0.1.18: SubMirror starts at logon on its own, so an unused
                # LibreTranslate folder is a note, not a warning.
                text = self._text("settings.libre_dir_optional")
            else:
                text = "%s (%s)" % (self._text("settings.libre_dir_invalid"), reason)
            try:
                if silent_launch.legacy_entry_present():
                    text = "%s %s" % (text, self._text("settings.libre_legacy_hint"))
            except Exception:                                  # noqa: BLE001
                logger.debug("the legacy startup entry could not be probed",
                             exc_info=True)
            self._libre_status.setText(text)

        def _warn_non_ascii_path(self, path: str) -> bool:
            """BL-1: warn (without rejecting) when the model path is not ASCII.

            Faster-Whisper's native libraries open the model directory through
            narrow-char APIs on some builds, where a Chinese path fails.
            """
            try:
                str(path).encode("ascii")
            except UnicodeEncodeError:
                QMessageBox.warning(
                    self, self._text("settings.model_non_ascii_title"),
                    self._text("settings.model_non_ascii_body"))
                return True
            return False

        def _on_reset_position(self) -> None:
            resetter = getattr(self._overlay, "reset_position", None)
            if callable(resetter):
                try:
                    resetter()
                except Exception:                              # noqa: BLE001
                    logger.exception("the overlay position could not be reset")
                    return
            self._overlay_width.setValue(
                self._config_get_int("overlay.width", 900))
            self._overlay_height.setValue(
                self._config_get_int("overlay.height", 220))

        def _on_reset_defaults(self) -> None:
            answer = QMessageBox.question(
                self, self._text("settings.btn.reset_defaults"),
                self._text("settings.btn.reset_defaults"))
            if answer != QMessageBox.StandardButton.Yes:
                return
            resetter = getattr(self._config, "reset", None)
            if callable(resetter):
                try:
                    resetter(save=True)
                except Exception:                              # noqa: BLE001
                    logger.exception("the defaults could not be restored")
                    return
            self.load_from_config()
            self._push_runtime()
            snapshot: Dict[str, Any] = {}
            as_dict = getattr(self._config, "as_dict", None)
            if callable(as_dict):
                try:
                    snapshot = dict(as_dict())
                except Exception:                              # noqa: BLE001
                    logger.debug("could not snapshot the config", exc_info=True)
            self.settings_applied.emit(snapshot)
            self._maintenance_status.setText(self._text("settings.reset_done"))

        def _on_restart_tasks(self) -> None:
            """Spec item 10: reset audio + OCR background work, no restart."""
            self.tasks_reset.emit()
            resetter = getattr(self._scheduler, "reset_all", None)
            if callable(resetter):
                try:
                    resetter()
                except Exception:                              # noqa: BLE001
                    logger.exception("the scheduler could not be reset")
            audio_reset = getattr(self._audio_capture, "reset_background_tasks", None)
            if callable(audio_reset):
                try:
                    audio_reset()
                except Exception:                              # noqa: BLE001
                    logger.exception("the audio tasks could not be reset")
            self._maintenance_status.setText(self._text("settings.tasks_reset"))

        def _on_restart_audio(self) -> None:
            reseter = getattr(self._scheduler, "reset_lane", None)
            if callable(reseter):
                for lane in (LANE_AUDIO, LANE_VAD, LANE_ASR):
                    try:
                        reseter(lane)
                    except Exception:                          # noqa: BLE001
                        logger.debug("lane %s was not reset", lane, exc_info=True)
            audio_reset = getattr(self._audio_capture, "reset_background_tasks", None)
            if callable(audio_reset):
                try:
                    audio_reset()
                except Exception:                              # noqa: BLE001
                    logger.exception("the audio tasks could not be reset")
            self._maintenance_status.setText(self._text("settings.tasks_reset"))

        def _on_restart_ocr(self) -> None:
            reseter = getattr(self._scheduler, "reset_lane", None)
            if callable(reseter):
                try:
                    reseter(LANE_OCR)
                except Exception:                              # noqa: BLE001
                    logger.exception("the OCR lane could not be reset")
            self._maintenance_status.setText(self._text("settings.tasks_reset"))

        # -- state ---------------------------------------------------------- #

        @property
        def was_saved(self) -> bool:
            return self._saved

    # ----------------------------------------------------------------------- #
    # Disclaimer gate
    # ----------------------------------------------------------------------- #

    class DisclaimerDialog(QDialog):  # type: ignore[misc]
        """
        Startup gate. ``run()`` returns True only when the box was ticked, and
        it persists ``disclaimer_accepted`` so it is asked exactly once.
        """

        def __init__(self, config: Any = None, parent: Any = None) -> None:
            super().__init__(parent)
            self._config = config
            self._is_accepted = False
            self._build_ui()

        @staticmethod
        def already_accepted(config: Any) -> bool:
            return bool(_config_get(config, "disclaimer_accepted", False))

        @property
        def is_accepted(self) -> bool:
            return self._is_accepted

        def _text(self, key: str, **fmt: Any) -> str:
            return config_text(self._config, key, **fmt)

        def _build_ui(self) -> None:
            self.setModal(True)
            self.setMinimumWidth(620)
            self.setWindowTitle(self._text("disclaimer.title"))
            layout = QVBoxLayout(self)

            heading = QLabel(f"{APP_NAME} {APP_VERSION}", self)
            heading.setStyleSheet("font-size: 16px; font-weight: 600;")
            layout.addWidget(heading)

            body = QLabel(self._text("disclaimer.body"), self)
            body.setWordWrap(True)
            layout.addWidget(body)

            risk_title = QLabel(self._text("disclaimer.risk_title"), self)
            risk_title.setStyleSheet("font-weight: 600; color: #B26A00;")
            layout.addWidget(risk_title)

            risk = QLabel(self._text("disclaimer.risk"), self)
            risk.setWordWrap(True)
            layout.addWidget(risk)

            self._agree_check = QCheckBox(self._text("disclaimer.accept"), self)
            layout.addWidget(self._agree_check)

            self._hint = QLabel(self._text("disclaimer.hint"), self)
            self._hint.setWordWrap(True)
            layout.addWidget(self._hint)

            buttons = QDialogButtonBox(self)
            self._btn_accept = buttons.addButton(
                self._text("disclaimer.accept"),
                QDialogButtonBox.ButtonRole.AcceptRole)
            self._btn_decline = buttons.addButton(
                self._text("disclaimer.decline"),
                QDialogButtonBox.ButtonRole.RejectRole)
            self._btn_accept.setEnabled(False)
            self._agree_check.toggled.connect(self._btn_accept.setEnabled)
            self._btn_accept.clicked.connect(self._on_accept)
            self._btn_decline.clicked.connect(self.reject)
            layout.addWidget(buttons)

        def _on_accept(self) -> None:
            if not self._agree_check.isChecked():
                self._hint.setText(self._text("disclaimer.required"))
                return
            self._is_accepted = True
            setter = getattr(self._config, "set", None)
            if callable(setter):
                try:
                    setter("disclaimer_accepted", True, save=True)
                except Exception:                              # noqa: BLE001
                    logger.exception("the disclaimer answer could not be saved")
            self.accept()

        @classmethod
        def run(cls, config: Any = None, parent: Any = None) -> bool:
            """Show the gate unless it was accepted before. True = may continue."""
            if cls.already_accepted(config):
                return True
            if not _QT_AVAILABLE or QApplication.instance() is None:
                logger.error("the disclaimer needs a running QApplication")
                return False
            dialog = cls(config, parent)
            dialog.exec()
            return bool(dialog.is_accepted)


def open_settings(config: Any = None, **kwargs: Any) -> bool:
    """
    Convenience entry point: build, show and save a settings dialog.

    Returns True when the user saved something. Used by tooling and tests; the
    tray wires the dialog up itself through ``SettingsDialog``.
    """
    if not _QT_AVAILABLE or SettingsDialog is None:
        logger.error("the settings panel needs PySide6 and is unavailable")
        return False
    if QApplication.instance() is None:
        logger.error("cannot open the settings panel without a QApplication")
        return False
    try:
        dialog = SettingsDialog(config, **kwargs)
        dialog.show()
        dialog.exec()
        return bool(getattr(dialog, "was_saved", False))
    except Exception:                                          # noqa: BLE001
        logger.exception("the settings panel could not be opened")
        return False


# --------------------------------------------------------------------------- #
# Text setters used by the binding table
# --------------------------------------------------------------------------- #

def _set_label_text(widget: Any, text: str) -> None:
    widget.setText(text)


def _set_tooltip_text(widget: Any, text: str) -> None:
    """Set a widget tooltip; an empty key value means 'clear it'."""
    widget.setToolTip(text or "")


def _set_button_text(widget: Any, text: str) -> None:
    widget.setText(text)


def _set_group_title(widget: Any, text: str) -> None:
    widget.setTitle(text)


def _set_window_title(widget: Any, text: str) -> None:
    widget.setWindowTitle(text)
