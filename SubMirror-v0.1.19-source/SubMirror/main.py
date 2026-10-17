# -*- coding: utf-8 -*-
"""
SubMirror - application entry point (file 16 of 24).

This module is the only place where the independent components meet.  It owns
the process lifetime:

    1. console logging and the single-instance mutex,
    2. QApplication, config repair notice and the disclaimer gate,
    3. construction of every long-lived component (scheduler, subtitle queues,
       text pipeline, translator, screen capture, OCR engine, audio capture,
       VAD, ASR, overlay, hotkeys, tray icon),
    4. the two pipelines that actually carry subtitles:

       audio :  AudioCaptureWorker -> VAD lane -> ASR lane -> TextProcessor
                -> BaseTranslator -> SubtitleManager.asr

       screen:  AreaPicker (region) / hotkey (full screen) -> ScreenCapturer
                -> OCR lane -> TextProcessor -> BaseTranslator
                -> SubtitleManager.ocr

    5. an orderly shutdown (stop audio/OCR work, drain threads, release hotkeys).

Safety boundaries honoured here: only the WASAPI loopback device is opened (the
microphone is never requested), the screen is read with the ordinary desktop
capture APIs, no process is injected and no input is synthesised.  v0.1 has no
TTS playback - the TTS line of the overlay stays a hidden placeholder.
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Project imports
# --------------------------------------------------------------------------- #

from src.core.config import (APP_NAME, APP_VERSION, DEFAULT_LANGUAGE,
                             LOAD_CORRUPT, LOAD_PARTIAL, get_config)
from src.core.context import (INIT_ALREADY_RUNNING, get_activate_message_id,
                              get_app_context, get_logger, init_app_context,
                              resolve_log_level, set_log_level,
                              setup_console_logging)
from src.core.hotkey import GlobalHotkeyManager
from src.core import modes as app_modes
from src.core.silent_launch import sync as sync_silent_start
from src.core.region_watch import (fingerprint as region_fingerprint,
                                   format_region, is_changed as region_changed,
                                   parse_region, text_changed as text_is_new)
from src.core.subtitle_queue import SubtitleManager
from src.core.task_scheduler import (LANE_ASR, LANE_OCR, LANE_TRANSLATE,
                                    LANE_VAD, TaskScheduler)
from src.audio.capture_win import AudioCaptureWorker
from src.audio.vad import VoiceActivityDetector
from src.screen.capture_win import ScreenCapturer
from src.screen.area_picker import AreaPicker
from src.ai.text_processor import TextProcessor
from src.ai.online_translator import OnlineTranslator
from src.screen.winrt_ocr import WinrtOcrEngine
from src.ui.overlay import SubtitleOverlay

# The settings UI is imported defensively: a broken/missing settings module must
# not take the whole program down (spec item: one module failing is survivable).
try:                                                    # pragma: no cover
    from src.ui.settings_ui import (DisclaimerDialog, SettingsDialog,
                                    notify_config_reset)
except Exception as _settings_import_error:             # pragma: no cover
    SettingsDialog = None                               # type: ignore[assignment]
    DisclaimerDialog = None                             # type: ignore[assignment]
    notify_config_reset = None                          # type: ignore[assignment]
    _SETTINGS_IMPORT_ERROR = str(_settings_import_error)
else:                                                   # pragma: no cover
    _SETTINGS_IMPORT_ERROR = ""

# --------------------------------------------------------------------------- #
# Qt imports (the whole UI is optional at import time, mandatory to actually run)
# --------------------------------------------------------------------------- #

try:                                                    # pragma: no cover
    from PySide6.QtCore import (QAbstractNativeEventFilter, QObject, Qt,
                                QTimer, Signal)
    from PySide6.QtGui import (QAction, QColor, QIcon, QPainter, QPixmap)
    from PySide6.QtWidgets import (QApplication, QMenu, QMessageBox,
                                   QSystemTrayIcon, QWidget)
    _QT_AVAILABLE = True
except Exception:                                       # pragma: no cover
    _QT_AVAILABLE = False

    class _FallbackSignal:
        """No-op signal used when PySide6 is unavailable."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._slots: List[Any] = []

        def connect(self, slot: Any) -> None:
            self._slots.append(slot)

        def disconnect(self, slot: Any = None) -> None:
            self._slots = []

        def emit(self, *args: Any, **kwargs: Any) -> None:
            return None

    class _BaseObject:
        """Minimal QObject stand-in."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    class QAbstractNativeEventFilter:                   # type: ignore[no-redef]
        def nativeEventFilter(self, _event_type: Any, _message: Any) -> Any:
            return False

    def Signal(*_args: Any, **_kwargs: Any) -> Any:      # type: ignore[no-redef]
        return _FallbackSignal()

    QObject = _BaseObject                                # type: ignore[assignment]
    QTimer = None                                        # type: ignore[assignment]
    QAction = None                                       # type: ignore[assignment]
    QColor = None                                        # type: ignore[assignment]
    QIcon = None                                         # type: ignore[assignment]
    QPainter = None                                      # type: ignore[assignment]
    QPixmap = None                                       # type: ignore[assignment]
    QApplication = None                                  # type: ignore[assignment]
    QMenu = None                                         # type: ignore[assignment]
    QMessageBox = None                                   # type: ignore[assignment]
    QSystemTrayIcon = None                               # type: ignore[assignment]
    QWidget = _BaseObject                                # type: ignore[assignment]


logger = get_logger("main")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

AREA_STREAM_TICK_MS = 60          # GUI-side polling while a region is selected
# Scheduler/OCR task tag for the fixed-region loop (v0.1.13). It only changes how
# a frame is gated, never what the OCR engine is asked to do.
MODE_FIXED_REGION = "fixed-region"
SHUTDOWN_TIMEOUT_S = 3.0
TRAY_NOTICE_MS = 4000
DEFAULT_VAD_SAMPLE_RATE = 16_000
ACTIVATE_MESSAGE_NAME = "SubMirror.Activate.v1"

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 0
EXIT_IMPORT_FAILED = 2
EXIT_NO_QT = 3
EXIT_INIT_FAILED = 4
EXIT_START_FAILED = 5

_ZH_TEXT: Dict[str, str] = {
    "dialog.notice.title": "SubMirror",
    "dialog.fatal.title": "启动失败",
    "dialog.fatal.body": "无法初始化 {app}：{reason}",
    "dialog.permission.title": "目录权限不足",
    "dialog.permission.body": "无法写入配置目录：{path}\n程序将以只读方式继续运行。",
    "dialog.hotkey.title": "全局热键不可用",
    "dialog.hotkey.body": "热键 {key}（{name}）已被其他程序占用。\n请打开设置更换热键后重试。",
    "dialog.about.title": "关于 SubMirror",
    "dialog.about.body": ("SubMirror {version}\n\n"
                          "桌面实时字幕翻译工具。\n"
                          "仅使用 WASAPI 环回与普通桌面截图，"
                          "绝不注入游戏进程、不读取游戏内存、不模拟键鼠输入。"),
    "disclaimer.title": "使用前须知",
    "disclaimer.body": ("SubMirror 不会注入、挂钩或修改任何游戏文件，"
                        "也不读写游戏内存；它只捕获系统混音并做普通的屏幕截图。\n\n"
                        "即便如此，个别反作弊系统仍可能对任何叠加窗口产生误判，"
                        "请自行评估风险，并优先在非竞技环境试用。\n\n"
                        "是否同意并继续？"),
    "disclaimer.agree": "同意并继续",
    "disclaimer.decline": "退出",
    "tray.tooltip": "SubMirror - 实时字幕翻译",
    "tray.show": "显示悬浮窗",
    "tray.hide": "隐藏悬浮窗",
    "tray.settings": "打开设置…",
    "tray.reset": "重置悬浮窗",
    "tray.ocr_stop": "暂停 OCR",
    "tray.ocr_start": "启用 OCR",
    "tray.state_on": "OCR 运行中",
    "tray.state_off": "OCR 已暂停",
    "tray.state_na": "OCR 不可用",
    "tray.about": "关于",
    "tray.quit": "退出",
    "status.starting": "正在启动…",
    "status.ready": "就绪",
    "status.stopped": "已停止",
    "notice.asr_unavailable": "语音识别不可用：请在设置中选择 Whisper 模型目录。",
    "notice.ocr_unavailable": "OCR 不可用：未检测到 Windows OCR 语言包。",
    "notice.hotkey_unavailable": "全局热键注册失败，请在设置中更换热键。",
    "notice.tasks_reset": "后台任务已重置。",
    "notice.asr_paused": "语音识别已暂停。",
    "notice.asr_resumed": "语音识别已恢复。",
    "notice.ocr_paused": "屏幕 OCR 已暂停。",
    "notice.ocr_resumed": "屏幕 OCR 已恢复。",
    "notice.overlay_reset": "悬浮窗已重置到默认位置。",
    "notice.region_ocr_on": "固定区域 OCR 已开启。",
    "notice.region_ocr_off": "固定区域 OCR 已停止。",
    "notice.region_ocr_pick": "请在屏幕上拖拽选择固定区域。",
    "notice.region_ocr_saved": "固定区域已保存。",
    "notice.region_ocr_none": "尚未保存固定区域，请先画框。",
    "notice.region_ocr_unavailable": "OCR 不可用：无法开启固定区域识别。",
    "name.area_ocr": "区域 OCR",
    "name.fullscreen_ocr": "全屏 OCR",
    "name.toggle_asr": "ASR 启停",
    "name.toggle_region_ocr": "固定区域 OCR 启停",
}

_EN_TEXT: Dict[str, str] = {
    "dialog.notice.title": "SubMirror",
    "dialog.fatal.title": "Startup failed",
    "dialog.fatal.body": "Could not initialise {app}: {reason}",
    "dialog.permission.title": "Folder not writable",
    "dialog.permission.body": ("The configuration folder is not writable: {path}\n"
                               "SubMirror keeps running in read-only mode."),
    "dialog.hotkey.title": "Global hotkey unavailable",
    "dialog.hotkey.body": ("Hotkey {key} ({name}) is already used by another program.\n"
                           "Open the settings and pick a different combination."),
    "dialog.about.title": "About SubMirror",
    "dialog.about.body": ("SubMirror {version}\n\n"
                          "Real-time desktop subtitle translation.\n"
                          "It only uses WASAPI loopback and ordinary desktop capture - "
                          "it never injects into a game, reads game memory or "
                          "synthesises input."),
    "disclaimer.title": "Before you continue",
    "disclaimer.body": ("SubMirror does not inject into, hook or modify any game file, "
                        "and it never reads game memory; it only captures the system "
                        "mix and takes ordinary screenshots.\n\n"
                        "Some anti-cheat systems may still flag any overlay window, so "
                        "judge the risk yourself and try SubMirror in a non-competitive "
                        "environment first.\n\nDo you agree and want to continue?"),
    "disclaimer.agree": "Agree and continue",
    "disclaimer.decline": "Quit",
    "tray.tooltip": "SubMirror - real-time subtitle translation",
    "tray.show": "Show overlay",
    "tray.hide": "Hide overlay",
    "tray.settings": "Open settings...",
    "tray.reset": "Reset overlay",
    "tray.ocr_stop": "Pause OCR",
    "tray.ocr_start": "Resume OCR",
    "tray.state_on": "OCR running",
    "tray.state_off": "OCR paused",
    "tray.state_na": "OCR unavailable",
    "tray.about": "About",
    "tray.quit": "Quit",
    "status.starting": "Starting...",
    "status.ready": "Ready",
    "status.stopped": "Stopped",
    "notice.asr_unavailable": "Speech recognition unavailable: choose a Whisper model folder in the settings.",
    "notice.ocr_unavailable": "OCR unavailable: no Windows OCR language pack was found.",
    "notice.hotkey_unavailable": "Global hotkeys could not be registered; change them in the settings.",
    "notice.tasks_reset": "Background tasks were reset.",
    "notice.asr_paused": "Speech recognition paused.",
    "notice.asr_resumed": "Speech recognition resumed.",
    "notice.ocr_paused": "Screen OCR paused.",
    "notice.ocr_resumed": "Screen OCR resumed.",
    "notice.overlay_reset": "The overlay was moved back to its default position.",
    "notice.region_ocr_on": "Fixed-region OCR started.",
    "notice.region_ocr_off": "Fixed-region OCR stopped.",
    "notice.region_ocr_pick": "Drag on the screen to select the fixed region.",
    "notice.region_ocr_saved": "The fixed region was saved.",
    "notice.region_ocr_none": "No fixed region saved yet - draw one first.",
    "notice.region_ocr_unavailable": "OCR unavailable: fixed-region OCR cannot start.",
    "name.area_ocr": "Area OCR",
    "name.fullscreen_ocr": "Full-screen OCR",
    "name.toggle_asr": "ASR on/off",
    "name.toggle_region_ocr": "Fixed-region OCR on/off",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    """Read a dotted config key without ever raising."""
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _cfg_set(config: Any, key: str, value: Any, save: bool = True) -> bool:
    if config is None:
        return False
    try:
        return bool(config.set(key, value, save=save))
    except Exception:
        logger.exception("could not store %s", key)
        return False


def _language(config: Any) -> str:
    try:
        value = str(getattr(config, "ui_language", "") or "")
    except Exception:
        value = ""
    return value if value in ("zh-CN", "en") else DEFAULT_LANGUAGE


def _tr(config: Any, key: str, **fmt: Any) -> str:
    """Translate a main.py string, preferring the config's own dictionary."""
    if config is not None:
        try:
            value = config.t(key)
            if isinstance(value, str) and value and value != key:
                return value.format(**fmt) if fmt else value
        except Exception:
            logger.debug("config translation failed for %s", key, exc_info=True)
    table = _EN_TEXT if _language(config) == "en" else _ZH_TEXT
    text = table.get(key) or _EN_TEXT.get(key) or key
    try:
        return text.format(**fmt) if fmt else text
    except Exception:
        return text


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _warn_dialog(config: Any, title_key: str, body_key: str, **fmt: Any) -> None:
    """Show a modal warning; never raises, never blocks a worker thread."""
    if not _QT_AVAILABLE:
        logger.warning("%s %s", _tr(config, title_key), _tr(config, body_key, **fmt))
        return
    try:
        box = QMessageBox()
        box.setWindowTitle(_tr(config, title_key))
        box.setText(_tr(config, body_key, **fmt))
        box.setIcon(QMessageBox.Icon.Warning)
        box.exec()
    except Exception:
        logger.exception("could not show the %s dialog", title_key)


def _fallback_disclaimer(config: Any) -> bool:
    """Minimal disclaimer gate used only when ``src.ui.settings_ui`` is missing."""
    if bool(_cfg_get(config, "disclaimer_accepted", False)):
        return True
    if not _QT_AVAILABLE:
        logger.error("the disclaimer cannot be shown without PySide6")
        return False
    box = QMessageBox()
    box.setWindowTitle(_tr(config, "disclaimer.title"))
    box.setText(_tr(config, "disclaimer.body"))
    box.setIcon(QMessageBox.Icon.Warning)
    agree = box.addButton(_tr(config, "disclaimer.agree"),
                          QMessageBox.ButtonRole.AcceptRole)
    box.addButton(_tr(config, "disclaimer.decline"),
                  QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not agree:
        return False
    _cfg_set(config, "disclaimer_accepted", True, save=True)
    return True


def _run_disclaimer(config: Any) -> bool:
    if DisclaimerDialog is not None:
        try:
            return bool(DisclaimerDialog.run(config))
        except Exception:
            logger.exception("the disclaimer dialog failed")
            return False
    return _fallback_disclaimer(config)


# --------------------------------------------------------------------------- #
# Single-instance activation
# --------------------------------------------------------------------------- #

class _MsgStruct(ctypes.Structure):
    """WIN32 ``MSG`` - only the field we read is spelled out."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_void_p),
        ("lParam", ctypes.c_void_p),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class ActivateMessageFilter(QAbstractNativeEventFilter):
    """
    Turn the "second instance started" broadcast into a callback.

    The second process only posts a message (``SingleInstanceGuard.notify_existing``).
    This filter is how the first process hears it; if Qt cannot deliver native
    messages the tray menu still offers the same entry points.
    """

    def __init__(self, callback: Any, message_id: int) -> None:
        super().__init__()
        self._callback = callback
        self._message_id = int(message_id)

    def nativeEventFilter(self, event_type: Any, message: Any) -> Any:  # noqa: N802
        try:
            kind = event_type.decode() if isinstance(event_type, bytes) else str(event_type)
            if "windows" not in kind.lower():
                return False
            address = getattr(message, "address", None)
            if address is None:
                address = int(message)                          # type: ignore[arg-type]
            payload = ctypes.cast(int(address), ctypes.POINTER(_MsgStruct)).contents
            if int(payload.message) == self._message_id:
                logger.info("another instance asked us to show up")
                self._callback()
        except Exception:
            logger.debug("native message filter failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Faster-Whisper wrapper
# --------------------------------------------------------------------------- #

BUNDLED_MODEL_RELATIVE = os.path.join("models", "faster-whisper-base")

# The sizes the settings dropdown offers.  A size is only usable when its
# folder is actually on disk: SubMirror never downloads a model at runtime.
MODEL_SIZE_NAMES = ("tiny", "base", "small", "medium", "large-v3")


def _model_roots() -> List[str]:
    """Folders that may contain a ``models\\`` bundle, nearest first."""
    roots: List[str] = []
    exe = str(getattr(sys, "executable", "") or "")
    if exe:
        roots.append(os.path.dirname(os.path.abspath(exe)))
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(here)
    roots.append(os.path.dirname(here))
    return roots


def _find_bundle(relative: str) -> str:
    """Absolute path of a bundle folder found under any known root, else ""."""
    seen = set()
    for root in _model_roots():
        if not root or root in seen:
            continue
        seen.add(root)
        candidate = os.path.join(root, relative)
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return ""


def bundled_model_dir() -> str:
    """
    Locate the Whisper model folder that ships with the portable package.

    The portable layout keeps it at ``<portable root>\\models\\faster-whisper-base``,
    i.e. next to ``SubMirror.exe``.  This is a read-only lookup: nothing is
    created, copied or written, and a folder found here is still accepted (or
    rejected) by exactly the same checks that judge a hand-configured path.

    Returns an absolute folder path, or an empty string when no bundle exists.
    """
    return _find_bundle(BUNDLED_MODEL_RELATIVE)


def bundled_model_dir_for(size: str) -> str:
    """
    Folder of a size-specific bundle (``models\\faster-whisper-<size>``).

    This is what makes the settings dropdown effective: choosing ``small``
    loads ``models\\faster-whisper-small`` when that folder was shipped (or
    placed) next to the executable.  Unknown names and missing folders return
    an empty string so the caller can fall back.
    """
    name = str(size or "").strip().lower()
    if name not in MODEL_SIZE_NAMES:
        return ""
    return _find_bundle(os.path.join("models", "faster-whisper-%s" % name))


class AsrEngine:
    """
    Thin, lazy wrapper around ``faster_whisper.WhisperModel``.

    v0.1 only transcribes - it never speaks.  Loading is lazy and failures are
    reported instead of raised so that a missing model only disables the audio
    pipeline: the overlay, the OCR pipeline and the settings keep working.
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._model: Any = None
        self._model_path = ""
        self._model_path_source = ""
        self._model_size = ""
        self._language = "auto"
        self._configured_language = "auto"
        self._force_english = True
        self._device = "cpu"
        self._compute_type = "int8"
        self._beam_size = 1
        self._best_of = 1
        self._temperature = 0.0
        self._condition_on_previous_text = False
        self._without_timestamps = True
        self._initial_prompt = ""
        self._no_speech_threshold = 0.5
        self._compression_ratio_threshold = 2.2
        self._log_prob_threshold = -1.0
        self._adaptive_beam = True
        self._fast_speech_wps = 3.0
        self._fast_beam_size = 1
        # Rolling words-per-second of the last few finished segments (v0.1.18
        # item 1): whisper costs grow with the beam, so fast speech switches to
        # the cheaper greedy pass instead of falling behind the speaker.
        self._recent_speech: Any = deque(maxlen=8)
        self._last_speech_wps = 0.0
        self._last_beam_used = 1
        self._fast_passes = 0
        self._load_error = ""
        self._stats: Dict[str, Any] = {"calls": 0, "failures": 0, "segments": 0,
                                       "total_ms": 0.0, "last_ms": 0.0,
                                       "partials": 0, "audio_ms": 0.0,
                                       "greedy_passes": 0}
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        self._model_size = self._normalize_size(_cfg_get(self._config, "asr.model_size", ""))
        configured = str(_cfg_get(self._config, "asr.model_path", "") or "").strip()
        path, self._model_path_source = self._resolve_model_path(configured, self._model_size)
        if path != self._model_path:
            self.unload()
            self._model_path = path
            if self._model_path_source == "bundled":
                logger.info("using the bundled Whisper model at %s", self._model_path)
            elif self._model_path_source == "size":
                logger.info("using the %s model bundle at %s",
                            self._model_size, self._model_path)
        self._configured_language = str(
            _cfg_get(self._config, "asr.language", "auto") or "auto")
        # Item "force English": the recogniser may only ever produce English,
        # which is what stops it from guessing a language from game noise.
        self._force_english = bool(_cfg_get(self._config, "asr.force_english", True))
        self._language = "en" if self._force_english else self._configured_language
        self._beam_size = max(1, int(_cfg_get(self._config, "asr.beam_size", 1) or 1))
        self._best_of = max(1, int(_cfg_get(self._config, "asr.best_of", 1) or 1))
        self._temperature = float(_cfg_get(self._config, "asr.temperature", 0.0) or 0.0)
        self._condition_on_previous_text = bool(
            _cfg_get(self._config, "asr.condition_on_previous_text", False))
        self._without_timestamps = bool(
            _cfg_get(self._config, "asr.without_timestamps", True))
        self._initial_prompt = str(
            _cfg_get(self._config, "asr.initial_prompt", "") or "").strip()
        self._no_speech_threshold = float(
            _cfg_get(self._config, "asr.no_speech_threshold", 0.5) or 0.5)
        self._compression_ratio_threshold = float(
            _cfg_get(self._config, "asr.compression_ratio_threshold", 2.2) or 2.2)
        self._log_prob_threshold = float(
            _cfg_get(self._config, "asr.log_prob_threshold", -1.0))
        self._device = str(_cfg_get(self._config, "asr.device", "cpu") or "cpu")
        self._compute_type = str(_cfg_get(self._config, "asr.compute_type", "int8") or "int8")
        # v0.1.18 item 1: speaking-rate guard.
        self._adaptive_beam = bool(_cfg_get(self._config, "asr.adaptive_beam", True))
        try:
            self._fast_speech_wps = max(
                0.1, float(_cfg_get(self._config, "asr.fast_speech_wps", 3.0) or 3.0))
        except (TypeError, ValueError):
            self._fast_speech_wps = 3.0
        try:
            self._fast_beam_size = max(
                1, int(_cfg_get(self._config, "asr.fast_beam_size", 1) or 1))
        except (TypeError, ValueError):
            self._fast_beam_size = 1

    def note_speech_rate(self, seconds: float, word_count: int) -> None:
        """Remember how fast one finished segment was spoken (words/second)."""
        try:
            duration = float(seconds)
            words = int(word_count)
        except (TypeError, ValueError):
            return
        if duration <= 0.05 or words <= 0:
            return
        with self._lock:
            self._recent_speech.append((duration, words))
            total_s = sum(item[0] for item in self._recent_speech)
            total_w = sum(item[1] for item in self._recent_speech)
            self._last_speech_wps = (total_w / total_s) if total_s > 0.0 else 0.0

    @property
    def speech_wps(self) -> float:
        """Rolling speaking rate of the recent segments."""
        with self._lock:
            return float(self._last_speech_wps)

    def beam_for(self, partial: bool = False) -> int:
        """Beam size for the next pass; fast speech drops to the cheap one."""
        if partial:
            return 1
        beam = max(1, int(self._beam_size))
        if self._adaptive_beam and self._fast_speech_wps > 0.0:
            if self.speech_wps >= self._fast_speech_wps:
                beam = min(beam, max(1, int(self._fast_beam_size)))
        self._last_beam_used = beam
        return beam

    @staticmethod
    def _normalize_size(value: Any) -> str:
        """Accept only a known dropdown size; anything else means "follow path"."""
        name = str(value or "").strip().lower()
        return name if name in MODEL_SIZE_NAMES else ""

    @staticmethod
    def _resolve_model_path(configured: str, size: str = "") -> Tuple[str, str]:
        """
        Pick the folder to load the model from.

        The dropdown wins while its bundle exists, a usable configured folder
        is next, and the bundle shipped next to the executable is the last
        resort - this is what keeps a fresh portable extraction working after
        the folder is moved.  When nothing exists the configured value is
        returned untouched, so the usual "model-path-empty" /
        "model-path-missing" errors still fire.
        """
        sized = bundled_model_dir_for(size)
        if sized:
            return sized, "size"
        if configured and os.path.isdir(configured):
            return configured, "configured"
        bundled = bundled_model_dir()
        if bundled:
            return bundled, "bundled"
        return configured, ("configured" if configured else "")

    @staticmethod
    def is_available() -> bool:
        return _has_module("faster_whisper")

    # -- introspection ----------------------------------------------------- #

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._model is not None

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def model_source(self) -> str:
        """``"configured"``, ``"bundled"`` or ``""`` - where model_path came from."""
        return self._model_path_source

    @property
    def language(self) -> str:
        """Effective recognition language ("en" while force-English is on)."""
        return self._language

    @property
    def configured_language(self) -> str:
        """What the settings store asked for, before force-English."""
        return self._configured_language

    @property
    def force_english(self) -> bool:
        return self._force_english

    @property
    def model_size(self) -> str:
        """Dropdown choice, or "" when the model path decides the folder."""
        return self._model_size

    @property
    def load_error(self) -> str:
        return self._load_error

    # -- lifecycle --------------------------------------------------------- #

    def load(self) -> bool:
        """Load the model once; returns False (with ``load_error``) on failure."""
        with self._lock:
            if self._model is not None:
                return True
            if not self._model_path:
                self._load_error = "model-path-empty"
                logger.info("no Whisper model folder configured yet")
                return False
            if not self.is_available():
                self._load_error = "faster-whisper-not-installed"
                logger.warning("faster-whisper is not installed; ASR is disabled")
                return False
            if not os.path.isdir(self._model_path):
                self._load_error = "model-path-missing"
                logger.warning("Whisper model folder does not exist: %s", self._model_path)
                return False
            try:
                from faster_whisper import WhisperModel
            except Exception as exc:                    # pragma: no cover
                self._load_error = "import-failed: %s" % exc
                logger.warning("faster-whisper import failed: %s", exc)
                return False

            attempts: List[Tuple[str, str]] = [(self._device, self._compute_type)]
            if self._device != "cpu":
                # A GPU that cannot actually run must not kill speech recognition.
                attempts.append(("cpu", "int8"))
            for device, compute_type in attempts:
                try:
                    started = time.monotonic()
                    model = WhisperModel(self._model_path, device=device,
                                         compute_type=compute_type)
                    elapsed = (time.monotonic() - started) * 1000.0
                    self._model = model
                    self._load_error = ""
                    logger.info("Whisper model loaded from %s (device=%s, compute_type=%s, %.0f ms)",
                                self._model_path, device, compute_type, elapsed)
                    return True
                except Exception as exc:
                    self._load_error = str(exc)
                    logger.warning("Whisper load failed on %s/%s: %s",
                                   device, compute_type, exc)
            logger.error("Whisper model could not be loaded: %s", self._load_error)
            return False

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def close(self) -> None:
        self.unload()

    # -- recognition ------------------------------------------------------- #

    def transcribe(self, samples: Any, sample_rate: int = DEFAULT_VAD_SAMPLE_RATE,
                   language: Optional[str] = None, partial: bool = False) -> str:
        """Transcribe one speech segment; returns "" on any failure."""
        parts = self.transcribe_parts(samples, sample_rate, language=language,
                                      partial=partial)
        return " ".join(text for _s, _e, text in parts if text).strip()

    def transcribe_parts(self, samples: Any, sample_rate: int = DEFAULT_VAD_SAMPLE_RATE,
                         language: Optional[str] = None,
                         partial: bool = False) -> List[Tuple[float, float, str]]:
        """
        Transcribe one segment and keep whisper's own timing.

        Returns ``[(start_s, end_s, text)]`` - empty on any failure.  The
        timings are what the pipeline uses to give a translation the same
        on-screen lifetime as the speech it belongs to.
        """
        if samples is None:
            return []
        if not _has_module("numpy"):
            # faster-whisper needs a float32 ndarray; without numpy we degrade.
            logger.debug("numpy is unavailable, skipping recognition")
            return []
        if not self.load():
            return []
        try:
            import numpy as np
            audio = np.asarray(samples, dtype="float32").reshape(-1)
            if audio.size == 0:
                return []
            if int(sample_rate) != DEFAULT_VAD_SAMPLE_RATE:
                audio = self._resample(audio, int(sample_rate), DEFAULT_VAD_SAMPLE_RATE)
            lang = language if language is not None else self._language
            lang = None if str(lang).lower() in ("", "auto", "none") else str(lang)
            with self._lock:
                model = self._model
            if model is None:
                return []
            # A partial pass runs on the sentence so far: we want the text as
            # early as possible, so it is allowed to drop timestamps and it
            # never keeps context from the previous sentence.
            without_timestamps = True if partial else self._without_timestamps
            beam = self.beam_for(partial)
            kwargs: Dict[str, Any] = {
                "language": lang,
                "beam_size": beam,
                "best_of": 1 if partial else max(1, self._best_of),
                "temperature": 0.0 if partial else self._temperature,
                "condition_on_previous_text": False if partial
                else self._condition_on_previous_text,
                "without_timestamps": without_timestamps,
                "vad_filter": False,          # our own VAD already trimmed audio
                "no_speech_threshold": self._no_speech_threshold,
                "compression_ratio_threshold": self._compression_ratio_threshold,
                "log_prob_threshold": self._log_prob_threshold,
            }
            if self._initial_prompt and not partial:
                kwargs["initial_prompt"] = self._initial_prompt
            started = time.monotonic()
            segments, _info = model.transcribe(audio, **kwargs)
            parts: List[Tuple[float, float, str]] = []
            for segment in segments:
                text = str(getattr(segment, "text", "") or "").strip()
                if not text:
                    continue
                begin = float(getattr(segment, "start", 0.0) or 0.0)
                end = float(getattr(segment, "end", begin) or begin)
                parts.append((begin, end, text))
            elapsed = (time.monotonic() - started) * 1000.0
            with self._lock:
                self._stats["calls"] += 1
                self._stats["segments"] += len(parts)
                self._stats["total_ms"] += elapsed
                self._stats["last_ms"] = elapsed
                self._stats["audio_ms"] += 1000.0 * audio.size / float(DEFAULT_VAD_SAMPLE_RATE)
                if partial:
                    self._stats["partials"] += 1
                if not partial and beam <= 1:
                    self._stats["greedy_passes"] = self._stats.get(
                        "greedy_passes", 0) + 1
                self._last_beam_used = beam
            # Release the (possibly resampled) audio buffer as soon as whisper is
            # done with it: a 30 s clip is several MB and the lane must not
            # accumulate them (v0.1.18 item 3).
            del audio
            return parts
        except Exception:
            with self._lock:
                self._stats["failures"] += 1
            logger.exception("speech recognition failed; segment dropped")
            return []

    @staticmethod
    def _resample(audio: Any, src_rate: int, dst_rate: int) -> Any:
        """Linear resample - the same maths as ``Resampler.resample``."""
        try:
            from src.audio.ring_buffer import Resampler
            return Resampler.resample(audio, src_rate, dst_rate)
        except Exception:
            return audio

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._stats)
        calls = max(1, int(data.get("calls", 0)))
        data["avg_ms"] = round(float(data.get("total_ms", 0.0)) / calls, 2)
        audio_ms = float(data.get("audio_ms", 0.0))
        data["speed"] = round(audio_ms / max(1.0, float(data.get("total_ms", 0.0))), 2)
        data["loaded"] = self.is_loaded
        data["model_path"] = self._model_path
        data["model_source"] = self._model_path_source
        data["model_size"] = self._model_size
        data["device"] = self._device
        data["language"] = self._language
        data["force_english"] = self._force_english
        data["beam_size"] = self._beam_size
        data["beam_used"] = self._last_beam_used
        data["speech_wps"] = round(self.speech_wps, 2)
        data["adaptive_beam"] = self._adaptive_beam
        data["load_error"] = self._load_error
        return data


# --------------------------------------------------------------------------- #
# ASR pipeline
# --------------------------------------------------------------------------- #

class AsrService(QObject):
    """
    Audio -> VAD -> ASR -> text filter -> translation -> ASR subtitle queue.

    Blocks are handed to the scheduler instead of being processed inline so the
    WASAPI callback returns immediately.  The VAD lane has a single worker, which
    keeps the streaming detector's state strictly ordered.
    """

    recognized = Signal(object)          # TextDecision
    failed = Signal(str)
    status_changed = Signal(str, str)

    def __init__(self, config: Any, scheduler: TaskScheduler, capture: Any,
                 vad: VoiceActivityDetector, engine: AsrEngine,
                 text_processor: TextProcessor, translator: Any,
                 subtitles: SubtitleManager, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._scheduler = scheduler
        self._capture = capture
        self._vad = vad
        self._engine = engine
        self._text = text_processor
        self._translator = translator
        self._subtitles = subtitles
        self._target_lang = ""
        self._max_chars = 200
        self._started = False
        self._shutting_down = False
        # -- incremental (streaming) output --
        self._streaming = True
        # 0.1.17 item 2: render the *translation* of a preview line, so a
        # half-recognised source line is never what the player reads.
        self._preview_translate = False
        self._translate_in_lane = True
        self._partial_interval_ms = 1100
        self._partial_min_seconds = 0.6
        self._partial_max_seconds = 8.0
        self._last_partial_at = 0.0
        self._partial_inflight = False
        self._partial_item_id = 0        # preview line currently on screen
        self._partial_sequence = 0       # utterance that line belongs to
        self._partial_text = ""
        # v0.1.18 item 4: the last few sentences are the only context a
        # translation gets. A game line is short and self-contained, so a long
        # history mainly risks a bigger request and a slower reply.
        self._context_sentences: List[str] = []
        self._context_max_sentences = 2
        self._max_sentences_per_utterance = 4
        # v0.1.18 item 6: consecutive timeouts widen the audio slice instead of
        # queueing ever more work behind a slow recogniser.
        self._consecutive_failures = 0
        self._overload_streak = 0
        self._stats: Dict[str, int] = {"frames": 0, "segments": 0, "recognized": 0,
                                       "rejected": 0, "pushed": 0, "dropped": 0,
                                       "partials": 0, "updates": 0,
                                       "translated_in_lane": 0,
                                       "sentences": 0, "sent_skipped": 0,
                                       "silence_skipped": 0,
                                       "translate_hints": 0}
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        self._target_lang = str(_cfg_get(self._config, "translate.target_lang", "zh-CN")
                                or "zh-CN")
        self._max_chars = int(_cfg_get(self._config, "asr.max_subtitle_chars", 200) or 200)
        # Preview output only: the finished segment still goes through the same
        # text filter and the same translator, so nothing downstream changes.
        self._streaming = bool(_cfg_get(self._config, "asr.streaming", True))
        self._preview_translate = bool(
            _cfg_get(self._config, "asr.preview_translate", True))
        self._translate_in_lane = bool(
            _cfg_get(self._config, "asr.translate_in_lane", True))
        self._context_max_sentences = max(
            0, int(_cfg_get(self._config, "translate.context_sentences", 2) or 0))
        self._max_sentences_per_utterance = max(
            1, int(_cfg_get(self._config, "asr.max_sentences", 4) or 4))
        self._partial_interval_ms = max(
            200, int(_cfg_get(self._config, "asr.partial_interval_ms", 1100) or 1100))
        self._partial_min_seconds = max(
            0.2, float(_cfg_get(self._config, "asr.partial_min_seconds", 0.6) or 0.6))
        self._partial_max_seconds = max(
            self._partial_min_seconds,
            float(_cfg_get(self._config, "asr.partial_max_seconds", 8.0) or 8.0))
        # v0.1.19 item 3: when the ASR lane is full the capture is paused instead
        # of growing an unbounded backlog. The limit is the same number the
        # scheduler uses for its queue, so "full" means the queue really is full.
        self._pause_on_full = bool(
            _cfg_get(self._config, "perf.pause_capture_on_full", True))
        self._queue_limit = max(1, int(_cfg_get(self._config, "perf.asr_queue_max", 3) or 3))
        self._install_queue_probe()

    def _install_queue_probe(self) -> None:
        """Let the capture see the ASR backlog without reaching into the lane."""
        install = getattr(self._capture, "set_queue_probe", None)
        if not callable(install):
            return
        try:
            if self._pause_on_full:
                install(lambda: self._scheduler.pending_count(LANE_ASR), self._queue_limit)
            else:
                install(None, self._queue_limit)
        except Exception:
            logger.debug("the capture backpressure probe could not be installed",
                         exc_info=True)

    @property
    def streaming(self) -> bool:
        """True while incremental preview lines are enabled."""
        return self._streaming

    @property
    def is_running(self) -> bool:
        return self._started

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> bool:
        if self._started:
            return True
        self._shutting_down = False
        if self._capture is None:
            self.failed.emit("audio-capture-missing")
            return False
        if self._engine is not None and not self._engine.model_path:
            # Not fatal: the user simply has not picked a model folder yet.
            logger.info("ASR idle: no Whisper model folder configured")
            self.failed.emit("model-path-empty")
        try:
            self._capture.set_sink(self._on_frame)
            self._install_queue_probe()
            started = bool(self._capture.start())
        except Exception:
            logger.exception("audio capture could not be started")
            self.failed.emit("audio-start-failed")
            return False
        self._started = started
        self.status_changed.emit("started" if started else "error", "")
        return started

    def stop(self, timeout: float = SHUTDOWN_TIMEOUT_S) -> None:
        self._shutting_down = True
        try:
            if self._capture is not None:
                self._capture.clear_sink()
        except Exception:
            logger.debug("could not clear the audio sink", exc_info=True)
        try:
            if self._capture is not None:
                self._capture.stop("application-exit")
        except Exception:
            logger.exception("audio capture did not stop cleanly")
        try:
            if self._vad is not None:
                segment = self._vad.flush()
                # Whatever was still open at exit is deliberately dropped.
                if segment is not None:
                    logger.debug("dropped the trailing segment on shutdown")
        except Exception:
            logger.debug("VAD flush failed", exc_info=True)
        self._partial_inflight = False
        self._partial_item_id = 0
        self._partial_sequence = 0
        self._partial_text = ""
        self._last_partial_at = 0.0
        self._started = False
        self.status_changed.emit("stopped", "")

    def reset_background_tasks(self) -> None:
        """Spec item 10: reset the audio pipeline without restarting SubMirror."""
        try:
            if self._vad is not None:
                self._vad.reset()
        except Exception:
            logger.debug("VAD reset failed", exc_info=True)
        try:
            if self._capture is not None:
                resetter = getattr(self._capture, "reset_background_tasks", None)
                if callable(resetter):
                    resetter()
        except Exception:
            logger.exception("audio background tasks could not be reset")
        try:
            self._scheduler.reset_lane(LANE_VAD, cancel_running=True)
            self._scheduler.reset_lane(LANE_ASR, cancel_running=True)
            self._scheduler.reset_lane(LANE_TRANSLATE, cancel_running=True)
        except Exception:
            logger.debug("scheduler reset failed", exc_info=True)
        self._partial_inflight = False
        self._partial_item_id = 0
        self._partial_sequence = 0
        self._partial_text = ""
        self._last_partial_at = 0.0

    # -- audio path -------------------------------------------------------- #

    def _on_frame(self, frame: Any) -> None:
        """Called from the capture thread - keep it short."""
        if self._shutting_down or frame is None:
            return
        self._stats["frames"] += 1
        if not self._scheduler.submit(LANE_VAD, self._vad_task, frame):
            self._stats["dropped"] += 1

    def _vad_task(self, token: Any, frame: Any) -> None:
        if token is not None and getattr(token, "is_cancelled", False):
            return
        try:
            segments = self._vad.push(getattr(frame, "samples", None),
                                      getattr(frame, "sample_rate", None))
        except Exception:
            logger.exception("VAD failed; block dropped")
            return
        for segment in segments or []:
            self._stats["segments"] += 1
            if getattr(segment, "samples", None) is None:
                continue
            # v0.1.18 item 1: a slice that is only background noise never reaches
            # whisper, which is where most of the CPU would have gone.
            if bool(getattr(segment, "silent", False)):
                self._stats["silence_skipped"] += 1
                self._release_segment(segment)
                continue
            if not self._scheduler.submit(LANE_ASR, self._asr_task, segment):
                self._stats["dropped"] += 1
                self._release_segment(segment)
        # v0.1.18 item 6: while the recognition lane is backed up the slices grow
        # (up to audio.vad_chunk_max_seconds) instead of queueing more work.
        self._note_load()
        # The whole point of the preview line: recognise what has been said so
        # far while the speaker is still talking, so the first text shows up
        # seconds before the segment - and its translation - are ready.
        self._maybe_submit_partial()

    def _note_load(self) -> None:
        """Tell the detector to back off while the recognition lane is busy."""
        pending = 0
        try:
            pending = int(self._scheduler.pending_count(LANE_ASR))
        except Exception:
            pending = 0
        overloaded = pending >= 2
        try:
            self._vad.note_load(overloaded)
        except Exception:
            logger.debug("chunk feedback failed", exc_info=True)
        if overloaded:
            self._overload_streak += 1
        else:
            self._overload_streak = 0

    @staticmethod
    def _release_segment(segment: Any) -> None:
        """
        Drop the audio buffer of a processed segment immediately.

        A 16 kHz float32 slice is a few hundred KB; holding it after the text
        exists is pure waste and the lane can hold several at once (item 3).
        """
        try:
            segment.samples = None
        except Exception:
            pass

    # -- incremental preview ----------------------------------------------- #

    def _maybe_submit_partial(self) -> None:
        """Queue a preview pass over the audio of the utterance in progress."""
        if self._shutting_down or not self._streaming or self._engine is None:
            return
        if self._partial_inflight:
            return
        now = time.monotonic()
        if (now - self._last_partial_at) * 1000.0 < self._partial_interval_ms:
            return
        try:
            audio = self._vad.active_audio()
        except Exception:
            logger.debug("could not read the in-progress audio", exc_info=True)
            return
        if audio is None:
            return
        try:
            rate = int(getattr(self._vad, "sample_rate", DEFAULT_VAD_SAMPLE_RATE)
                       or DEFAULT_VAD_SAMPLE_RATE)
            seconds = float(len(audio)) / float(rate)
        except Exception:
            return
        if seconds < self._partial_min_seconds or seconds > self._partial_max_seconds:
            return
        try:
            sequence = int(self._vad.current_sequence)
        except Exception:
            sequence = 0
        self._last_partial_at = now
        self._partial_inflight = True
        if not self._scheduler.submit(LANE_ASR, self._partial_task,
                                      (sequence, audio, seconds)):
            self._partial_inflight = False

    def _partial_task(self, token: Any, payload: Any) -> None:
        try:
            sequence, audio, seconds = payload
        except Exception:
            self._partial_inflight = False
            return
        try:
            if self._shutting_down or not self._streaming:
                return
            sample_rate = int(getattr(self._vad, "sample_rate", DEFAULT_VAD_SAMPLE_RATE)
                              or DEFAULT_VAD_SAMPLE_RATE)
            text = self._engine.transcribe(audio, sample_rate, partial=True)
            if token is not None and getattr(token, "is_cancelled", False):
                return
            if not text:
                return
            decision = self._text.process_asr(text, self._target_lang)
            if not decision.accepted:
                return
            self._stats["partials"] += 1
            sequence = int(sequence or 0)
            seconds = float(seconds or 0.0)
            # 0.1.17 item 2: translate first, render second. The translation
            # goes to the translation lane so recognising the next segment is
            # never held up by it; a failed translation falls back to the
            # recognised source text, exactly like the finished line does.
            if self._preview_translate and self._translator is not None:
                payload = (decision.text, sequence, seconds)
                if self._scheduler.submit(LANE_TRANSLATE,
                                          self._preview_translate_task, payload):
                    return
            self._push_preview(decision.text, sequence, seconds)
        except Exception:
            logger.debug("incremental recognition failed", exc_info=True)
        finally:
            self._partial_inflight = False

    def _preview_translate_task(self, token: Any, payload: Any) -> None:
        """Translate one preview line, then write it (0.1.17 item 2)."""
        try:
            text, sequence, seconds = payload
        except Exception:
            return
        translated = ""
        try:
            if not self._shutting_down and self._streaming:
                result = self._translator.translate(text,
                                                    target_lang=self._target_lang)
                if result is not None and bool(getattr(result, "ok", False)):
                    translated = str(getattr(result, "translated", "") or "").strip()
        except Exception:
            logger.exception("preview translation raised despite its contract")
        if token is not None and getattr(token, "is_cancelled", False):
            return
        if self._shutting_down:
            return
        self._push_preview(translated or text, sequence, seconds)

    def _push_preview(self, text: str, sequence: int, seconds: float) -> None:
        """Write (or rewrite) the incremental line for the current utterance."""
        if not text:
            return
        lifetime = self._aligned_timeout(seconds)
        if sequence and sequence == self._partial_sequence:
            if self._update_line(self._partial_item_id, original=text, pending=True,
                                 timeout_s=lifetime):
                self._partial_text = text
                self._stats["updates"] += 1
                return
        item = self._subtitles.asr.push(text, "", failed=False, pending=True,
                                        sequence=int(sequence or 0),
                                        timeout_s=lifetime)
        if item is not None:
            self._partial_item_id = int(getattr(item, "id", 0) or 0)
            self._partial_sequence = int(sequence or 0)
            self._partial_text = text

    def _update_line(self, item_id: int, **fields: Any) -> bool:
        """Rewrite a line already on screen; False when the queue cannot."""
        if not item_id:
            return False
        queue = getattr(self._subtitles, "asr", None)
        updater = getattr(queue, "update", None)
        if not callable(updater):
            return False
        try:
            return updater(item_id, **fields) is not None
        except Exception:
            logger.debug("subtitle update failed", exc_info=True)
            return False

    def _aligned_timeout(self, seconds: float) -> float:
        """
        On-screen lifetime for one line.

        A short line does not linger and a long one is allowed to stay: the
        translation is kept for roughly as long as the speech it belongs to.
        """
        try:
            value = float(seconds or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value <= 0.0:
            return self._default_timeout()
        return min(8.0, max(1.5, value * 0.9 + 1.2))

    def _default_timeout(self) -> float:
        """The subtitle queue's own lifetime, used when no speech length is known."""
        queue = getattr(self._subtitles, "asr", None)
        value = getattr(queue, "_timeout_s", None)
        if isinstance(value, (int, float)) and float(value) > 0.0:
            return float(value)
        return 6.0

    def _asr_task(self, token: Any, segment: Any) -> None:
        if self._shutting_down:
            return
        try:
            if bool(getattr(segment, "silent", False)):
                self._stats["silence_skipped"] += 1
                return
            sample_rate = int(getattr(segment, "sample_rate", DEFAULT_VAD_SAMPLE_RATE)
                              or DEFAULT_VAD_SAMPLE_RATE)
            parts = self._engine.transcribe_parts(getattr(segment, "samples", None),
                                                  sample_rate)
            if token is not None and getattr(token, "is_cancelled", False):
                return
            if not parts:
                return
            text = " ".join(piece for _start, _end, piece in parts if piece).strip()
            if not text:
                return
            decision = self._text.process_asr(text, self._target_lang)
            if not decision.accepted:
                self._stats["rejected"] += 1
                logger.debug("ASR text rejected (%s): %r", decision.reason, text[:60])
                return
            self._stats["recognized"] += 1
            self.recognized.emit(decision)
            sequence = int(getattr(segment, "sequence", 0) or 0)
            seconds = float(getattr(segment, "duration", 0.0) or 0.0)
            if seconds <= 0.0:
                # Whisper's own timing is the fallback when the segment is synthetic.
                starts = [start for start, _end, _text in parts]
                ends = [end for _start, end, _text in parts]
                seconds = max(0.0, max(ends) - min(starts)) if starts and ends else 0.0
            # v0.1.18 item 1: the spoken speed of this segment decides whether the
            # next pass may afford a wider beam.
            words = len([word for word in decision.text.split() if word.strip(".,!?;:")])
            if self._engine is not None:
                self._engine.note_speech_rate(seconds, words)
            self._push_sentences(decision.text, sequence, seconds)
        finally:
            # The recognised text is all that is needed downstream (item 3).
            self._release_segment(segment)

    def _push_sentences(self, text: str, sequence: int, seconds: float) -> None:
        """
        Show one finished utterance as one short line per sentence.

        Long lines are what produced the runaway translations: the recogniser
        emits a whole paragraph and the translator invents the rest. Splitting on
        ``. ! ?`` keeps every request small and every line readable, and each
        sentence carries the on-screen time of the speech it came from (item 4).
        """
        try:
            sentences = self._text.sentences(text)
        except Exception:
            sentences = [str(text or "").strip()] if text else []
        sentences = [piece for piece in sentences if piece]
        if not sentences:
            return
        self._stats["sentences"] += len(sentences)
        total_chars = max(1, sum(len(piece) for piece in sentences))
        for index, sentence in enumerate(sentences):
            if index >= self._max_sentences_per_utterance:
                # A wall of text in one utterance: keep the first lines rather
                # than flooding the translator (item 4, "no bulk submission").
                self._stats["sent_skipped"] += len(sentences) - index
                break
            share = seconds * (len(sentence) / float(total_chars)) if seconds > 0 else 0.0
            # The first sentence owns the preview line; the rest are new lines.
            self._push(sentence, sequence=sequence if index == 0 else 0,
                       seconds=max(0.8, share))
        # Remember what was said, so the next sentence has context (item 4).
        self._remember_sentences(sentences[:self._max_sentences_per_utterance])

    def _remember_sentences(self, sentences: List[str]) -> None:
        limit = max(0, int(self._context_max_sentences))
        if limit == 0:
            self._context_sentences = []
            return
        buffer = self._context_sentences + [str(piece).strip() for piece in sentences
                                            if str(piece or "").strip()]
        self._context_sentences = buffer[-limit:]

    def _context_text(self) -> str:
        """The rolling context handed to a prompt-capable translator."""
        if not self._context_sentences:
            return ""
        return " ".join(self._context_sentences)

    def _ui_text(self, key: str, fallback: str) -> str:
        """Localised UI string from the config, or *fallback*."""
        config = getattr(self, "_config", None)
        getter = getattr(config, "t", None)
        if callable(getter):
            try:
                value = getter(key)
            except Exception:
                value = None
            if value and str(value) != str(key):
                return str(value)
        return fallback

    def _push(self, original: str, sequence: int = 0, seconds: float = 0.0) -> int:
        """
        Show the finished line and translate it.

        The source text is written immediately (so no time is lost waiting for
        the translator) and the translation replaces it in place when it
        arrives, even when that happens on the translation lane.
        """
        item_id = self._push_final(original, sequence, seconds)
        self._translate(original, item_id, seconds)
        return item_id

    def _push_final(self, original: str, sequence: int, seconds: float) -> int:
        """Write the finished source text, reusing the preview line when it is
        the same utterance.  Returns the subtitle item id (0 when nothing was
        written)."""
        lifetime = self._aligned_timeout(seconds)
        item_id = 0
        if sequence and sequence == self._partial_sequence:
            item_id = self._partial_item_id
        if item_id:
            # 0.1.17 item 2: the preview line already shows a translation, so
            # keep it on screen while the finished line is being translated
            # instead of flashing the raw recognised text first.
            keep = self._partial_text if self._preview_translate else ""
            fields: Dict[str, Any] = {"original": original, "pending": True,
                                      "timeout_s": lifetime}
            if keep:
                fields.update({"translated": keep, "failed": False})
            if self._update_line(item_id, **fields):
                self._partial_item_id = 0        # the finished line owns it now
                self._partial_sequence = 0
                self._partial_text = ""
                self._stats["updates"] += 1
                return item_id
        item = self._subtitles.asr.push(original, "", failed=False, pending=True,
                                        sequence=int(sequence or 0),
                                        timeout_s=lifetime)
        if item is None:
            return 0
        return int(getattr(item, "id", 0) or 0)

    def _translate(self, original: str, item_id: int, seconds: float) -> None:
        """Translate off the ASR lane when the scheduler can take the work."""
        lifetime = self._aligned_timeout(seconds)
        # The rolling context is captured now, on the caller's thread: by the time
        # the lane runs, later sentences may already be in the buffer (item 4).
        payload = (original, item_id, lifetime, self._context_text())
        if self._translate_in_lane and item_id:
            if self._scheduler.submit(LANE_TRANSLATE, self._translate_task, payload):
                self._stats["translated_in_lane"] += 1
                return
        self._translate_task(None, payload)

    def _translate_task(self, token: Any, payload: Any) -> None:
        try:
            original, item_id, lifetime = payload[0], payload[1], payload[2]
            context = payload[3] if len(payload) > 3 else ""
        except Exception:
            return
        result = None
        try:
            result = self._translator.translate(original,
                                                target_lang=self._target_lang,
                                                context=context)
        except TypeError:
            # A translator that predates the context argument still works.
            try:
                result = self._translator.translate(original,
                                                    target_lang=self._target_lang)
            except Exception:
                logger.exception("translation raised despite its contract")
        except Exception:
            logger.exception("translation raised despite its contract")
        if token is not None and getattr(token, "is_cancelled", False):
            return
        translated = ""
        ok = False
        if result is not None:
            ok = bool(getattr(result, "ok", False))
            translated = str(getattr(result, "translated", "") or "").strip()
        failed = not (ok and translated)
        display = translated
        line_failed = failed
        if failed:
            # v0.1.18 item 6: a missing translation must never leave a blank
            # overlay. The line says what happened, in the UI language, instead of
            # dropping out silently (0.1.17 still forbids showing raw English).
            error = str(getattr(result, "error", "") or "") if result is not None else ""
            timed_out = (result is None) or error.startswith("timeout")
            display = self._ui_text(
                "overlay.status.translate_timeout" if timed_out
                else "overlay.status.translate_failed",
                "翻译超时" if timed_out else "翻译服务繁忙")
            # The notice replaces the pending line, so it has to survive
            # SubtitleItem.display_text(): a failed item renders its source text,
            # which would hide the hint. The source stays on item.original (and
            # translate.show_original still prints both lines).
            line_failed = False
            self._stats["translate_hints"] += 1
            self._note_translate_failure(timed_out)
        else:
            self._consecutive_failures = 0
        if item_id and self._update_line(item_id, translated=display,
                                         failed=line_failed, pending=False,
                                         timeout_s=lifetime):
            self._stats["updates"] += 1
            self._stats["pushed"] += 1
            return
        self._subtitles.asr.push(original, display,
                                 failed=line_failed, timeout_s=lifetime)
        self._stats["pushed"] += 1

    def _note_translate_failure(self, timed_out: bool) -> None:
        """Repeated translation trouble widens the audio slice (item 6)."""
        self._consecutive_failures += 1
        if self._consecutive_failures not in (3, 6, 9):
            return
        try:
            if timed_out:
                self._vad.stretch_chunk()
            else:
                self._vad.relax_chunk()
        except Exception:
            logger.debug("chunk adaptation failed", exc_info=True)
        logger.warning("translation failing repeatedly (%d in a row, timeout=%s): "
                       "audio slice is now %.2fs",
                       self._consecutive_failures, timed_out,
                       float(getattr(self._vad, "chunk_limit_seconds", 0.0) or 0.0))

    def stats(self) -> Dict[str, Any]:
        data: Dict[str, Any] = dict(self._stats)
        data["streaming"] = self._streaming
        data["preview_sequence"] = self._partial_sequence
        data["translate_in_lane"] = self._translate_in_lane
        try:
            # stats is a property on the detector; calling it was a latent
            # bug that the except below hid, leaving "vad" always empty.
            data["vad"] = self._vad.stats
        except Exception:
            data["vad"] = {}
        try:
            data["asr"] = self._engine.stats()
        except Exception:
            data["asr"] = {}
        data["started"] = self._started
        return data


# --------------------------------------------------------------------------- #
# OCR pipeline
# --------------------------------------------------------------------------- #

class OcrService(QObject):
    """
    Area OCR (streaming while the hotkey is held) and one-shot full-screen OCR.

    Frames come from ``ScreenCapturer``, which already enforces the FPS-safe cap
    and the 800 ms full-screen floor and keeps at most one pending frame.  The
    heavy work runs in the scheduler's OCR lane, so the GUI thread never blocks.
    """

    recognized = Signal(object)          # TextDecision
    failed = Signal(str)
    status_changed = Signal(str, str)

    def __init__(self, config: Any, scheduler: TaskScheduler, capturer: ScreenCapturer,
                 picker: AreaPicker, engine: Any, text_processor: TextProcessor,
                 translator: Any, subtitles: SubtitleManager,
                 parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._scheduler = scheduler
        self._capturer = capturer
        self._picker = picker
        self._engine = engine
        self._text = text_processor
        self._translator = translator
        self._subtitles = subtitles
        self._timer: Any = None
        self._region: Any = None
        self._streaming = False
        self._shutting_down = False
        self._enabled = True
        self._target_lang = "zh-CN"
        self._max_chars = 200
        # Fixed-region loop (v0.1.13). _fixed_region is the saved rectangle,
        # _fixed_enabled mirrors the config switch, _fixed_pick is set while the
        # user is drawing a new rectangle and suppresses the drag-OCR path.
        self._fixed_region: Any = None
        self._fixed_enabled = False
        self._fixed_streaming = False
        self._fixed_pick = False
        self._region_tolerance = 8
        self._last_fingerprint: Any = None
        self._last_text = ""
        self._stats: Dict[str, int] = {"ticks": 0, "frames": 0, "throttled": 0,
                                       "recognized": 0, "rejected": 0,
                                       "pushed": 0, "dropped": 0, "fullscreen": 0,
                                       "fixed": 0, "unchanged": 0}
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        self._target_lang = str(_cfg_get(self._config, "translate.target_lang", "zh-CN")
                                or "zh-CN")
        self._max_chars = int(_cfg_get(self._config, "asr.max_subtitle_chars", 200) or 200)
        # Fixed-region OCR: the geometry only ever changes through an explicit
        # re-pick or a settings edit, so the loop reads it here and nowhere else.
        stored = _cfg_get(self._config, "ocr.fixed_region", "")
        self._fixed_region = parse_region(stored)
        self._fixed_enabled = bool(_cfg_get(self._config, "ocr.fixed_region_enabled", False))
        try:
            self._region_tolerance = int(_cfg_get(self._config, "ocr.region_change_tolerance", 8))
        except (TypeError, ValueError):
            self._region_tolerance = 8
        # The engine caches its own thresholds (language, timeout, line
        # filter), so it has to be told about a settings change as well.
        applier = getattr(self._engine, "apply_config", None)
        if callable(applier):
            try:
                applier()
            except Exception:
                logger.exception("the OCR engine could not apply the new settings")
        try:
            self._capturer.apply_config()
        except Exception:
            logger.debug("screen capturer could not apply the new settings", exc_info=True)

    @property
    def is_running(self) -> bool:
        return self._streaming

    @property
    def is_enabled(self) -> bool:
        """False while the screen pipeline is paused from the tray menu."""
        return self._enabled

    @property
    def region(self) -> Any:
        return self._region

    @property
    def fixed_region(self) -> Any:
        return self._fixed_region

    @property
    def is_fixed_running(self) -> bool:
        """True while the saved rectangle is being sampled on a loop."""
        return self._fixed_streaming

    @property
    def fixed_enabled(self) -> bool:
        """The saved switch; the loop itself may still be idle without a region."""
        return self._fixed_enabled

    # -- fixed region (v0.1.13) -------------------------------------------- #

    def set_fixed_region(self, region: Any, save: bool = True) -> bool:
        """
        Remember one fixed rectangle (hotkey drag or the settings "draw" button).

        The geometry is stored as text so a hand-edited config stays readable,
        and the running loop picks it up on its next tick.
        """
        parsed = parse_region(region)
        if parsed is None:
            logger.debug("fixed region rejected: %r", region)
            return False
        self._fixed_region = parsed
        self._last_fingerprint = None
        self._last_text = ""
        if save:
            if not _cfg_set(self._config, "ocr.fixed_region", format_region(parsed)):
                logger.warning("the fixed region could not be saved to the config")
        return True

    def begin_fixed_pick(self) -> None:
        """Suppress the drag-OCR path while the user redraws the rectangle."""
        self._fixed_pick = True

    def end_fixed_pick(self) -> None:
        self._fixed_pick = False

    def start_fixed_region(self) -> bool:
        """Begin sampling the saved rectangle (no-op without region or engine)."""
        if self._fixed_region is None or self._engine is None or not self._enabled \
                or self._shutting_down:
            return False
        if self._fixed_streaming:
            return True
        self._last_fingerprint = None
        self._last_text = ""
        self._fixed_streaming = True
        try:
            if self._timer is not None:
                self._timer.start()
        except Exception:
            logger.debug("area OCR timer start failed", exc_info=True)
        logger.info("fixed-region OCR started: %s", format_region(self._fixed_region))
        return True

    def stop_fixed_region(self, reason: str = "fixed-region-off",
                          clear: bool = True) -> None:
        """
        Stop sampling the rectangle.

        The drag path may still own the shared timer, so it is only stopped when
        no region drag is in progress.  ``clear=False`` is used for the silent
        re-sync paths (start-up, config push) where the subtitle line is not
        supposed to blink out.
        """
        was_running = self._fixed_streaming
        self._fixed_streaming = False
        self._last_fingerprint = None
        self._last_text = ""
        if not self._streaming:
            try:
                if self._timer is not None:
                    self._timer.stop()
            except Exception:
                logger.debug("area OCR timer stop failed", exc_info=True)
        if was_running:
            logger.info("fixed-region OCR stopped (%s)", reason)
            if clear:
                self._clear_ocr(reason)

    def set_fixed_enabled(self, enabled: bool, save: bool = True) -> bool:
        """
        Turn the recurring loop on or off (hotkey / settings checkbox).

        The switch is persisted, so a user who leaves the loop running finds it
        running again after a restart; the rectangle itself is never discarded
        by this call.
        """
        enabled = bool(enabled)
        self._fixed_enabled = enabled
        if save:
            if not _cfg_set(self._config, "ocr.fixed_region_enabled", enabled):
                logger.warning("the fixed-region switch could not be saved")
        if not enabled:
            self.stop_fixed_region("fixed-region-off")
            return False
        return self.start_fixed_region()

    def sync_fixed_region(self) -> bool:
        """
        Reconcile the loop with the persisted switch.

        Called on start-up, after a settings change and when the screen pipeline
        is paused or resumed, so a saved "on" always comes back by itself.
        """
        if not self._fixed_enabled or self._fixed_region is None or not self._enabled \
                or self._shutting_down:
            self.stop_fixed_region("fixed-region-idle", clear=False)
            return False
        return self.start_fixed_region()

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> bool:
        self._shutting_down = False
        if self._engine is None:
            logger.warning("OCR is unavailable; the screen pipeline stays idle")
            self.failed.emit("ocr-engine-unavailable")
            return False
        if _QT_AVAILABLE and self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(AREA_STREAM_TICK_MS)
            self._timer.timeout.connect(self._tick_area)
        if self._picker is not None:
            self._picker.region_ready.connect(self.on_region_ready)
            self._picker.region_changed.connect(self.on_region_changed)
            self._picker.finished.connect(self.on_region_finished)
            self._picker.failed.connect(self._on_picker_failed)
        self.sync_fixed_region()
        self.status_changed.emit("started", "")
        return True

    def stop(self, timeout: float = SHUTDOWN_TIMEOUT_S) -> None:
        self._shutting_down = True
        self._streaming = False
        self._fixed_pick = False
        self.stop_fixed_region("shutdown", clear=False)
        try:
            if self._timer is not None:
                self._timer.stop()
        except Exception:
            logger.debug("area OCR timer stop failed", exc_info=True)
        try:
            if self._picker is not None:
                self._picker.shutdown()
        except Exception:
            logger.debug("area picker shutdown failed", exc_info=True)
        try:
            self._scheduler.reset_lane(LANE_OCR, cancel_running=True)
        except Exception:
            logger.debug("OCR lane reset failed", exc_info=True)
        self.status_changed.emit("stopped", "")

    def reset_background_tasks(self) -> None:
        try:
            self._capturer.reset_backend()
        except Exception:
            logger.debug("screen backend reset failed", exc_info=True)
        self._streaming = False
        self._region = None
        try:
            if self._timer is not None:
                self._timer.stop()
        except Exception:
            logger.debug("area OCR timer stop failed", exc_info=True)
        try:
            self._scheduler.reset_lane(LANE_OCR, cancel_running=True)
        except Exception:
            logger.debug("OCR lane reset failed", exc_info=True)
        self.stop_fixed_region("tasks-reset", clear=False)
        if not self._is_picking():
            self._clear_ocr("tasks-reset")

    def set_enabled(self, enabled: bool) -> None:
        """
        Pause or resume the screen pipeline (tray menu / settings toggle).

        This only gates the app-level orchestration: the OCR engine itself is
        never touched, so resuming needs no reload and no warm-up.  While paused
        no snapshot is taken, no OCR task is submitted and no subtitle is pushed.
        """
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._streaming = False
            self._region = None
            self.stop_fixed_region("ocr-paused", clear=False)
            try:
                if self._timer is not None:
                    self._timer.stop()
            except Exception:
                logger.debug("area OCR timer stop failed", exc_info=True)
            # A drag that is still in progress must not survive the pause.
            if self._is_picking():
                try:
                    self._picker.cancel("ocr-paused")
                except Exception:
                    logger.debug("area picker cancel failed", exc_info=True)
            self._clear_ocr("ocr-paused")
        self.sync_fixed_region()
        logger.info("screen OCR %s", "resumed" if enabled else "paused")
        self.status_changed.emit("resumed" if enabled else "paused", "")

    # -- hotkey / picker wiring -------------------------------------------- #

    def on_region_ready(self, region: Any) -> None:
        """First valid drag rectangle: begin streaming."""
        if not self._enabled:
            logger.debug("area OCR is paused; the drag is ignored")
            return
        if self._fixed_pick:
            # The drag is only drawing a new fixed rectangle; the app-level
            # commit handler saves it, so no streaming starts here.
            return
        self._region = region
        self._streaming = True
        if self._timer is not None:
            self._timer.start()
        logger.debug("area OCR streaming started: %s",
                     getattr(region, "as_tuple", region))

    def on_region_changed(self, region: Any) -> None:
        """The rectangle moved mid-drag; the next tick uses the new geometry."""
        if self._fixed_pick:
            return
        self._region = region

    def on_region_finished(self, region: Any, reason: str) -> None:
        """Hotkey released / Esc / shutdown: stop streaming and clear the line."""
        self._fixed_pick = False
        self._streaming = False
        if self._timer is not None:
            self._timer.stop()
        self._region = None
        self._clear_ocr(reason or "region-release")
        logger.debug("area OCR streaming stopped (%s)", reason)

    def on_fullscreen_trigger(self) -> None:
        """Full-screen OCR: exactly one snapshot per trigger (spec item: 800 ms)."""
        if self._shutting_down or not self._enabled:
            return
        remaining = 0
        try:
            remaining = int(self._capturer.remaining_fullscreen_ms())
        except Exception:
            remaining = 0
        if remaining > 0:
            self._stats["throttled"] += 1
            logger.debug("full-screen OCR throttled, %d ms left", remaining)
            return
        frame = None
        try:
            frame = self._capturer.grab_primary(force=False)
        except Exception:
            logger.exception("full-screen snapshot failed; trigger dropped")
            return
        if frame is None:
            self._stats["dropped"] += 1
            return
        try:
            self._capturer.mark_fullscreen()
        except Exception:
            logger.debug("could not stamp the full-screen trigger", exc_info=True)
        self._stats["fullscreen"] += 1
        self._submit(frame, "fullscreen")

    def _on_picker_failed(self, reason: str) -> None:
        logger.warning("area picker failed: %s", reason)
        self.failed.emit(reason)

    def _is_picking(self) -> bool:
        try:
            return bool(self._picker.is_active)
        except Exception:
            return False

    # -- capture loop ------------------------------------------------------ #

    def _tick_area(self) -> None:
        self._stats["ticks"] += 1
        if self._shutting_down or not self._enabled:
            return
        # The hold-and-drag path wins: while a drag is active the saved
        # rectangle is not sampled.
        if self._streaming and self._region is not None:
            self._tick_drag(self._region)
            return
        if self._fixed_streaming and self._fixed_region is not None:
            self._tick_fixed(self._fixed_region)

    def _tick_drag(self, region: Any) -> None:
        """Hold-to-drag area OCR: every accepted frame is recognised."""
        frame = self._grab(region)
        if frame is None:
            return
        self._submit(frame, "area")

    def _tick_fixed(self, region: Any) -> None:
        """
        Fixed-region OCR: only a picture that really changed reaches the engine.

        An idle region therefore costs one small luminance fingerprint per tick
        instead of a full OCR pass and a translation request.
        """
        frame = self._grab(region)
        if frame is None:
            return
        current = region_fingerprint(frame)
        if not region_changed(self._last_fingerprint, current, self._region_tolerance):
            self._stats["unchanged"] += 1
            return
        if current is not None:
            self._last_fingerprint = current
        self._stats["fixed"] += 1
        self._submit(frame, MODE_FIXED_REGION)

    def _grab(self, region: Any) -> Any:
        """One throttled area grab; None simply means "nothing this tick"."""
        try:
            return self._capturer.try_grab_area(region, monitor_index=0)
        except Exception:
            logger.exception("area snapshot failed; frame dropped")
            return None

    def _submit(self, frame: Any, mode: str) -> None:
        if not self._enabled:
            return
        self._stats["frames"] += 1
        if not self._scheduler.submit(LANE_OCR, self._ocr_task, frame, mode):
            self._stats["dropped"] += 1

    def _ocr_task(self, token: Any, frame: Any, mode: str) -> None:
        if self._shutting_down or not self._enabled:
            return
        image = getattr(frame, "image", None)
        region = getattr(frame, "region", None)
        offset = None
        if region is not None:
            try:
                offset = (int(region.left), int(region.top))
            except Exception:
                offset = None
        try:
            if image is None:
                # Some capture backends keep only the raw buffer (numpy absent).
                # WinRT needs a real bitmap, so let the engine rebuild one.
                result = self._engine.recognize_frame(frame, region=region,
                                                      offset=offset)
            else:
                result = self._engine.recognize(image, region=region, offset=offset)
        except Exception:
            # WinrtOcrEngine.recognize() never raises, but belt and braces.
            logger.exception("OCR engine raised; frame dropped")
            return
        if token is not None and getattr(token, "is_cancelled", False):
            return
        if result is None or not getattr(result, "ok", False) or getattr(result, "is_empty", True):
            logger.debug("OCR produced nothing (%s): %s", mode,
                         getattr(result, "error", "") or "empty")
            return
        try:
            text = result.joined(separator=" ", limit=self._max_chars)
        except Exception:
            text = str(getattr(result, "text", "") or "")
        if not text:
            return
        decision = self._text.process_ocr(text, self._target_lang)
        if not decision.accepted:
            self._stats["rejected"] += 1
            logger.debug("OCR text rejected (%s): %r", decision.reason, text[:60])
            return
        if mode == MODE_FIXED_REGION:
            # Second gate of the fixed-region loop: a picture can change (a
            # clock, a cursor) without the recognised line changing, and the
            # same line must not be re-translated over and over.
            if not text_is_new(self._last_text, decision.text):
                self._stats["unchanged"] += 1
                logger.debug("fixed-region OCR: the recognised line did not change")
                return
            self._last_text = decision.text
        self._stats["recognized"] += 1
        self.recognized.emit(decision)
        self._push(decision.text)

    def _push(self, original: str) -> None:
        result = None
        try:
            result = self._translator.translate(original, target_lang=self._target_lang)
        except Exception:
            logger.exception("translation raised despite its contract")
        if result is not None and result.ok and (result.translated or "").strip():
            self._subtitles.ocr.push(original, result.translated, failed=False)
        else:
            self._subtitles.ocr.push(original, "", failed=True)
        self._stats["pushed"] += 1

    def _clear_ocr(self, reason: str) -> None:
        try:
            self._subtitles.clear_ocr(reason)
        except Exception:
            logger.debug("OCR subtitle clear failed", exc_info=True)

    def stats(self) -> Dict[str, Any]:
        data: Dict[str, Any] = dict(self._stats)
        data["streaming"] = self._streaming
        try:
            data["capture"] = self._capturer.stats()
        except Exception:
            data["capture"] = {}
        try:
            data["ocr"] = getattr(self._engine, "stats", lambda: {})()
        except Exception:
            data["ocr"] = {}
        return data


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #

class TrayController(QObject):
    """System tray entry points: overlay visibility, settings, OCR on/off, quit."""

    toggle_subtitles_requested = Signal()
    reset_overlay_requested = Signal()
    ocr_toggle_requested = Signal()
    settings_requested = Signal()
    about_requested = Signal()
    quit_requested = Signal()

    def __init__(self, config: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._tray: Any = None
        self._menu: Any = None
        self._actions: Dict[str, Any] = {}
        self._overlay_visible = True
        self._ocr_active = False
        self._ocr_available = True

    @property
    def is_available(self) -> bool:
        return bool(self._tray is not None and self._tray.isVisible())

    def start(self) -> bool:
        if not _QT_AVAILABLE:
            return False
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                logger.warning("no system tray on this desktop; tray menu disabled")
                return False
            self._tray = QSystemTrayIcon(self)
            icon = self._state_icon(self._ocr_active)
            if icon is None:
                try:
                    style = QApplication.style()
                    icon = style.standardIcon(style.StandardPixmap.SP_ComputerIcon)
                except Exception:
                    icon = None
            if icon is not None:
                self._tray.setIcon(icon)
            self._menu = QMenu()
            # The label of "toggle" and "ocr" follows the live state; the keys
            # below are the fallback labels used until the state is known.
            specs = (("toggle", "toggle_subtitles_requested"),
                     ("settings", "settings_requested"),
                     ("reset", "reset_overlay_requested"),
                     ("ocr", "ocr_toggle_requested"))
            for kind, signal_name in specs:
                action = QAction(self._menu)
                action.triggered.connect(getattr(self, signal_name).emit)
                self._menu.addAction(action)
                self._actions[kind] = action
            self._menu.addSeparator()
            for kind, signal_name in (("about", "about_requested"),
                                      ("quit", "quit_requested")):
                action = QAction(self._menu)
                action.triggered.connect(getattr(self, signal_name).emit)
                self._menu.addAction(action)
                self._actions[kind] = action
            self._tray.setContextMenu(self._menu)
            self._tray.activated.connect(self._on_activated)
            self._refresh_labels()
            self._tray.show()
            return True
        except Exception:
            logger.exception("the tray icon could not be created")
            self._tray = None
            return False

    def _on_activated(self, reason: Any) -> None:
        try:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self.toggle_subtitles_requested.emit()
        except Exception:
            logger.debug("tray activation handling failed", exc_info=True)

    def _state_icon(self, active: bool) -> Any:
        """
        Draw the tray icon: a green dot while OCR runs, a gray dot while paused.

        The pixmap is painted at runtime so the repository needs no icon asset
        and the two states can never drift apart.
        """
        if not _QT_AVAILABLE or QPixmap is None or QPainter is None:
            return None
        try:
            size = 64
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                # Dark disc first: the coloured dot stays visible on light trays.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(32, 34, 38))
                painter.drawEllipse(2, 2, size - 4, size - 4)
                painter.setBrush(QColor(46, 204, 113) if active
                                 else QColor(148, 153, 161))
                painter.drawEllipse(16, 16, size - 32, size - 32)
            finally:
                painter.end()
            return QIcon(pixmap)
        except Exception:
            logger.debug("the tray state icon could not be drawn", exc_info=True)
            return None

    def set_overlay_visible(self, visible: bool) -> None:
        """Remember the overlay visibility so the menu label stays accurate."""
        self._overlay_visible = bool(visible)
        self._refresh_labels()

    def set_ocr_active(self, active: bool, available: bool = True) -> None:
        """Green dot = the screen pipeline runs, gray dot = it is paused."""
        self._ocr_active = bool(active)
        self._ocr_available = bool(available)
        try:
            if self._tray is not None:
                icon = self._state_icon(self._ocr_active)
                if icon is not None:
                    self._tray.setIcon(icon)
        except Exception:
            logger.debug("the tray icon could not be refreshed", exc_info=True)
        self._refresh_labels()

    def _refresh_labels(self) -> None:
        """Repaint the tooltip from the live overlay/OCR state."""
        if self._tray is None:
            return
        try:
            if not self._ocr_available:
                state_key = "tray.state_na"
            else:
                state_key = "tray.state_on" if self._ocr_active else "tray.state_off"
            self._tray.setToolTip("%s\n%s" % (_tr(self._config, "tray.tooltip"),
                                              _tr(self._config, state_key)))
        except Exception:
            logger.debug("tray tooltip update failed", exc_info=True)
        self.retranslate()

    def retranslate(self) -> None:
        if self._tray is None:
            return
        try:
            keys = {
                "toggle": "tray.hide" if self._overlay_visible else "tray.show",
                "reset": "tray.reset",
                "ocr": "tray.ocr_stop" if self._ocr_active else "tray.ocr_start",
                "settings": "tray.settings",
                "about": "tray.about",
                "quit": "tray.quit",
            }
            for kind, key in keys.items():
                action = self._actions.get(kind)
                if action is not None:
                    action.setText(_tr(self._config, key))
            ocr_action = self._actions.get("ocr")
            if ocr_action is not None:
                # A machine without the Windows OCR language pack gets a
                # disabled entry instead of a toggle that can never work.
                ocr_action.setEnabled(self._ocr_available)
        except Exception:
            logger.debug("tray retranslation failed", exc_info=True)

    def notify(self, message: str, title: str = "") -> None:
        if self._tray is None:
            return
        try:
            self._tray.showMessage(title or APP_NAME, message,
                                   QSystemTrayIcon.MessageIcon.Information,
                                   TRAY_NOTICE_MS)
        except Exception:
            logger.debug("tray notification failed", exc_info=True)

    def shutdown(self) -> None:
        try:
            if self._tray is not None:
                self._tray.hide()
        except Exception:
            logger.debug("tray teardown failed", exc_info=True)
        self._tray = None


# --------------------------------------------------------------------------- #
# Application controller
# --------------------------------------------------------------------------- #

class SubMirrorApp(QObject):
    """Owns every component, wires the signals and shuts everything down."""

    def __init__(self, app: Any, context: Any, config: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._app = app
        self._context = context
        self._config = config

        self._scheduler: Optional[TaskScheduler] = None
        self._subtitles: Optional[SubtitleManager] = None
        self._text: Optional[TextProcessor] = None
        self._translator: Any = None
        self._capturer: Optional[ScreenCapturer] = None
        self._picker: Optional[AreaPicker] = None
        self._ocr_engine: Any = None
        self._vad: Optional[VoiceActivityDetector] = None
        self._capture: Any = None
        self._asr: Optional[AsrEngine] = None
        self._overlay: Any = None
        self._hotkeys: Optional[GlobalHotkeyManager] = None
        self._tray: Optional[TrayController] = None
        self._settings: Any = None
        self._asr_service: Optional[AsrService] = None
        self._asr_off = False          # speech recognition switched off by the user
        self._ocr_service: Optional[OcrService] = None
        self._fix_pick_armed = False   # waiting for a fixed-region drag
        self._fix_pick_saved = False   # the drag was already committed
        self._settings_hidden_for_pick = False
        self._filter: Any = None
        self._settings_ok = SettingsDialog is not None
        self._shutting_down = False
        self._started_at = 0.0

    # -- construction ------------------------------------------------------ #

    def start(self) -> bool:
        self._started_at = time.monotonic()
        try:
            # v0.1.19: the selected work mode's recommended parameters must be in
            # force before any component reads the config (app.mode_auto_params
            # off leaves the config file untouched).
            self._apply_mode_params()
            self._build_scheduler()
            self._build_subtitles()
            self._build_text_pipeline()
            self._build_screen_pipeline()
            self._build_audio_pipeline()
            self._build_overlay()
            self._build_hotkeys()
            self._build_tray()
            self._wire_settings_hint()
            self._sync_silent_start()
            self._start_backends()
        except Exception:
            logger.exception("SubMirror could not be assembled")
            return False
        logger.info("SubMirror %s started (language=%s)", APP_VERSION, _language(self._config))
        return True

    def _build_scheduler(self) -> None:
        self._scheduler = TaskScheduler(self._config)
        self._scheduler.start()

    def _build_subtitles(self) -> None:
        self._subtitles = SubtitleManager(self._config)
        self._subtitles.start()

    def _build_text_pipeline(self) -> None:
        self._text = TextProcessor(self._config)
        self._translator = OnlineTranslator(self._config)
        try:
            self._translator.failed.connect(self._on_translation_failed)
            self._translator.status_changed.connect(self._on_translation_status)
        except Exception:
            logger.debug("could not connect the translator signals", exc_info=True)

    def _build_screen_pipeline(self) -> None:
        self._capturer = ScreenCapturer(self._config)
        self._picker = AreaPicker(self._config)
        # v0.1 has exactly one OCR engine: the one built into Windows. There is
        # no registry and no engine switching, so this is a direct instantiation.
        self._ocr_engine = None
        available, reason = WinrtOcrEngine.is_available()
        if not available:
            logger.warning("WinRT OCR is unavailable: %s", reason)
        else:
            engine = WinrtOcrEngine(self._config)
            if engine.load():
                self._ocr_engine = engine
            else:
                logger.warning("WinRT OCR could not start: %s",
                               engine.error or "unknown error")
                try:
                    engine.close()
                except Exception:
                    logger.debug("OCR engine close failed", exc_info=True)
        if self._ocr_engine is None:
            logger.warning("screen OCR is disabled; the rest of SubMirror keeps running")
        self._ocr_service = OcrService(self._config, self._scheduler, self._capturer,
                                       self._picker, self._ocr_engine, self._text,
                                       self._translator, self._subtitles)
        try:
            self._ocr_service.failed.connect(self._on_ocr_failed)
            self._ocr_service.status_changed.connect(self._on_ocr_status)
            self._picker.committed.connect(self._on_fixed_region_committed)
            self._picker.finished.connect(self._on_fixed_region_finished)
        except Exception:
            logger.debug("could not connect the OCR service signals", exc_info=True)

    def _build_audio_pipeline(self) -> None:
        self._vad = VoiceActivityDetector(self._config, sample_rate=DEFAULT_VAD_SAMPLE_RATE)
        self._asr = AsrEngine(self._config)
        try:
            self._capture = AudioCaptureWorker(config=self._config)
        except Exception:
            logger.exception("the WASAPI loopback capture could not be created")
            self._capture = None
        self._asr_service = AsrService(self._config, self._scheduler, self._capture,
                                       self._vad, self._asr, self._text,
                                       self._translator, self._subtitles)
        try:
            self._asr_service.failed.connect(self._on_asr_failed)
            self._asr_service.status_changed.connect(self._on_asr_status)
            if self._capture is not None:
                # v0.1.19: the capture reports "queue full, capture paused"
                # through the same status path the overlay already renders.
                self._capture.status_changed.connect(self._on_asr_status)
        except Exception:
            logger.debug("could not connect the ASR service signals", exc_info=True)

    def _build_overlay(self) -> None:
        self._overlay = SubtitleOverlay(self._config, self._subtitles)
        try:
            self._overlay.failed.connect(self._on_overlay_failed)
            self._overlay.closed.connect(self._on_overlay_closed)
            self._overlay.visibility_changed.connect(self._on_overlay_visibility)
        except Exception:
            logger.debug("could not connect the overlay signals", exc_info=True)

    def _build_hotkeys(self) -> None:
        self._hotkeys = GlobalHotkeyManager(self._config)
        try:
            self._hotkeys.region_pressed.connect(self._on_region_pressed)
            self._hotkeys.region_released.connect(self._on_region_released)
            self._hotkeys.fullscreen_pressed.connect(self._ocr_service.on_fullscreen_trigger)
            self._hotkeys.asr_toggled.connect(self._toggle_asr)
            self._hotkeys.region_toggled.connect(self._toggle_region_ocr)
            self._hotkeys.registered.connect(self._on_hotkey_registered)
            self._hotkeys.register_failed.connect(self._on_hotkey_failed)
        except Exception:
            logger.debug("could not connect the hotkey signals", exc_info=True)

    def _build_tray(self) -> None:
        self._tray = TrayController(self._config, self)
        try:
            self._tray.toggle_subtitles_requested.connect(self._toggle_overlay)
            self._tray.reset_overlay_requested.connect(self._reset_overlay)
            self._tray.ocr_toggle_requested.connect(self._toggle_ocr)
            self._tray.settings_requested.connect(self.open_settings)
            self._tray.about_requested.connect(self._show_about)
            self._tray.quit_requested.connect(self._on_quit_requested)
        except Exception:
            logger.debug("could not connect the tray signals", exc_info=True)

    def _wire_settings_hint(self) -> None:
        if self._settings_ok:
            return
        logger.warning("the settings panel is unavailable: %s",
                       _SETTINGS_IMPORT_ERROR or "module missing")

    def _sync_silent_start(self) -> None:
        """
        Keep the logon entry in step with the saved setting.

        The Startup-folder script is a file, not a service, so it can go stale
        (moved portable folder, hand-edited config). Converging on start-up means
        the checkbox and reality never drift apart.
        """
        try:
            result = sync_silent_start(self._config)
        except Exception:
            logger.exception("the logon start entry could not be synchronised")
            return
        if result and result != "disabled":
            logger.info("logon start entry: %s", result)

    def _start_backends(self) -> None:
        # Hotkeys are registered before the pipelines so the very first hotkey
        # press already has somewhere to go.
        if self._hotkeys is not None:
            if not self._hotkeys.start():
                logger.error("global hotkeys could not be registered")
                self._notify_hotkeys()
        if self._ocr_service is not None:
            if not self._ocr_service.start() and self._overlay is not None:
                self._overlay.set_notice(_tr(self._config, "notice.ocr_unavailable"))
        if self._asr_service is not None:
            if not self._asr_service.start() and not self._asr_off and self._overlay is not None:
                self._overlay.set_notice(_tr(self._config, "notice.asr_unavailable"))
        if self._tray is not None and not self._tray.is_available:
            # The controller is built with the rest of the app, but the icon has
            # to be created explicitly - and only after the pipelines exist, so
            # the first paint already shows the real OCR state.
            if not self._tray.start():
                logger.warning("the tray icon is unavailable on this desktop")
        if self._tray is not None:
            self._tray_sync_ocr()
            self._tray.set_overlay_visible(True)
        if self._overlay is not None:
            try:
                self._overlay.show_overlay()
                self._overlay.on_status_changed("ok", "")
            except Exception:
                logger.exception("the overlay could not be shown")
        self._install_activate_filter()

    def _install_activate_filter(self) -> None:
        """Hear the single-instance broadcast so a second launch raises us."""
        if not _QT_AVAILABLE or self._app is None:
            return
        try:
            message_id = int(get_activate_message_id())
        except Exception:
            logger.debug("no activation message id available", exc_info=True)
            return
        try:
            self._filter = ActivateMessageFilter(self._on_activate_request, message_id)
            self._app.installNativeEventFilter(self._filter)
        except Exception:
            logger.warning("the activation filter could not be installed; "
                           "use the tray icon instead")

    # -- signal handlers --------------------------------------------------- #

    def _on_activate_request(self) -> None:
        """A second launch happened: show something the user can see."""
        try:
            self._overlay.show_overlay()
        except Exception:
            logger.debug("could not re-show the overlay", exc_info=True)
        if self._settings_ok:
            self.open_settings()
        else:
            logger.info("SubMirror is already running")

    def _on_translation_failed(self, result: Any) -> None:
        error = getattr(result, "error", "") or "unknown"
        logger.warning("translation failed: %s", error)
        try:
            self._overlay.on_translate_state("failed", str(error))
        except Exception:
            logger.debug("overlay translation notice failed", exc_info=True)

    def _on_translation_status(self, state: str, message: str) -> None:
        logger.info("translator state: %s (%s)", state, message)
        try:
            self._overlay.on_translate_state(state, message)
        except Exception:
            logger.debug("overlay translation state failed", exc_info=True)

    def _on_translation_result(self, result: Any) -> None:      # pragma: no cover
        # Results are pushed from the pipeline lanes; this hook only logs.
        logger.debug("translation result: %s", getattr(result, "status", ""))

    def _on_asr_status(self, state: str, message: str) -> None:
        logger.info("audio pipeline: %s %s", state, message)
        try:
            self._overlay.on_audio_state(state, message)
        except Exception:
            logger.debug("overlay audio state failed", exc_info=True)

    def _on_asr_failed(self, reason: str) -> None:
        logger.warning("audio pipeline problem: %s", reason)
        if reason == "model-path-empty" and not self._asr_off and self._overlay is not None:
            self._overlay.set_notice(_tr(self._config, "notice.asr_unavailable"))

    def _on_ocr_status(self, state: str, message: str) -> None:
        logger.debug("OCR pipeline: %s %s", state, message)
        self._tray_sync_ocr()

    def _on_ocr_failed(self, reason: str) -> None:
        logger.warning("OCR pipeline problem: %s", reason)
        if reason == "ocr-engine-unavailable" and self._overlay is not None:
            self._overlay.set_notice(_tr(self._config, "notice.ocr_unavailable"))

    def _on_overlay_failed(self, reason: str) -> None:
        logger.error("overlay problem: %s", reason)

    def _on_overlay_closed(self) -> None:
        logger.info("the overlay was closed by the user")

    def _on_hotkey_registered(self, name: str, key: str) -> None:
        logger.info("hotkey registered: %s = %s", name, key)

    def _on_hotkey_failed(self, name: str, key: str) -> None:
        logger.warning("hotkey already taken: %s = %s", name, key)
        _warn_dialog(self._config, "dialog.hotkey.title", "dialog.hotkey.body",
                     name=_tr(self._config, "name.%s" % name) if name else name, key=key)
        if self._overlay is not None:
            self._overlay.set_notice(_tr(self._config, "notice.hotkey_unavailable"))
        if self._settings_ok:
            self.open_settings()

    def _notify_hotkeys(self) -> None:
        if self._overlay is not None:
            self._overlay.set_notice(_tr(self._config, "notice.hotkey_unavailable"))

    def _on_overlay_visibility(self, visible: bool) -> None:
        """Keep the tray menu label in step with the overlay itself."""
        if self._tray is not None:
            self._tray.set_overlay_visible(visible)

    def _on_region_pressed(self) -> None:
        """Area OCR hotkey: ignored while the screen pipeline is paused."""
        service = self._ocr_service
        if service is not None and not service.is_enabled:
            logger.debug("screen OCR is paused; the area hotkey is ignored")
            return
        if self._picker is not None:
            self._picker.on_region_pressed()

    def _on_region_released(self) -> None:
        """
        Always release the picker, so a pause mid-drag cannot strand it.

        Exception: while the fixed-region pick is armed the window is a session
        of its own.  It is opened by the fixed-region hotkey and closed by Esc
        (or by pressing that hotkey again), so releasing the *area* hotkey must
        not tear it down and lose the rectangle the user just drew.
        """
        if self._fix_pick_armed:
            logger.debug("hotkey release ignored: the fixed-region pick is armed")
            return
        if self._picker is not None:
            self._picker.on_region_released()

    def _toggle_overlay(self) -> None:
        try:
            visible = bool(self._overlay.toggle_visible())
        except Exception:
            logger.exception("could not toggle the overlay")
            return
        if self._tray is not None:
            self._tray.set_overlay_visible(visible)
        logger.info("overlay %s", "shown" if visible else "hidden")

    def _reset_overlay(self) -> None:
        """Tray entry point: bring the overlay back to its default position."""
        if self._overlay is None:
            logger.debug("the overlay reset is ignored: there is no overlay")
            return
        try:
            if not bool(self._overlay.isVisible()):
                self._overlay.show_overlay()
            self._overlay.reset_position()
            logger.info("the overlay was reset to its default position")
            self._notify_state("notice.overlay_reset")
        except Exception:
            logger.exception("the overlay could not be reset")
        if self._tray is not None:
            try:
                self._tray.set_overlay_visible(bool(self._overlay.isVisible()))
            except Exception:
                logger.debug("the tray overlay state failed", exc_info=True)

    def _toggle_ocr(self) -> None:
        """
        Pause or resume screen OCR from the tray menu.

        This is an app-level gate: no snapshot is taken, no OCR task is
        submitted and no subtitle is pushed while paused.  The Windows OCR
        engine and the recognition code are not touched at all, so resuming is
        instantaneous.
        """
        service = self._ocr_service
        if service is None or self._ocr_engine is None:
            logger.debug("the OCR toggle is ignored: the screen pipeline is unavailable")
            if self._overlay is not None:
                try:
                    self._overlay.set_notice(_tr(self._config, "notice.ocr_unavailable"))
                except Exception:
                    logger.debug("overlay notice failed", exc_info=True)
            return
        try:
            enabled = not bool(service.is_enabled)
            service.set_enabled(enabled)
            logger.info("screen OCR %s from the tray", "resumed" if enabled else "paused")
            self._notify_state("notice.ocr_resumed" if enabled else "notice.ocr_paused")
        except Exception:
            logger.exception("the OCR toggle failed")
        finally:
            self._tray_sync_ocr()

    def _tray_sync_ocr(self) -> None:
        """Mirror the screen-pipeline state onto the tray icon and menu."""
        if self._tray is None:
            return
        service = self._ocr_service
        available = bool(service is not None and self._ocr_engine is not None)
        active = bool(available and service is not None and service.is_enabled)
        try:
            self._tray.set_ocr_active(active, available)
        except Exception:
            logger.debug("the tray OCR state could not be refreshed", exc_info=True)

    # -- fixed-region OCR (v0.1.13) ---------------------------------------- #

    def _toggle_region_ocr(self) -> None:
        """
        Fixed-region OCR on/off hotkey.

        Three behaviours, in this order: while the loop runs it stops; with a
        saved region it starts; without one it arms the picker, so the first
        press still leads somewhere useful.
        """
        service = self._ocr_service
        if service is None or self._ocr_engine is None:
            logger.debug("fixed-region OCR ignored: the screen pipeline is unavailable")
            self._notify_state("notice.region_ocr_unavailable")
            return
        try:
            if service.is_fixed_running:
                service.set_fixed_enabled(False)
                self._notify_state("notice.region_ocr_off")
                return
            if service.fixed_region is None:
                self._pick_fixed_region()
                return
            if service.set_fixed_enabled(True):
                self._notify_state("notice.region_ocr_on")
            else:
                self._notify_state("notice.region_ocr_none")
        except Exception:
            logger.exception("the fixed-region toggle failed")

    def _pick_fixed_region(self) -> None:
        """Arm the picker so the next drag saves the fixed rectangle."""
        if self._picker is None:
            self._notify_state("notice.region_ocr_unavailable")
            return
        if self._fix_pick_armed:
            self._fix_pick_armed = False
            self._fix_pick_saved = False
            self._picker.cancel("pick-cancelled")
            return
        self._fix_pick_armed = True
        self._fix_pick_saved = False
        if self._ocr_service is not None:
            self._ocr_service.begin_fixed_pick()
        # The panel is modeless and would sit on top of the full-screen picker,
        # so it steps aside for the drag and comes back afterwards.
        self._settings_hidden_for_pick = bool(self._settings is not None
                                              and self._settings.isVisible())
        if self._settings_hidden_for_pick:
            try:
                self._settings.hide()
            except Exception:
                logger.debug("the settings panel could not be hidden", exc_info=True)
        self._notify_state("notice.region_ocr_pick")
        if not self._picker.begin():
            self._fix_pick_armed = False
            if self._ocr_service is not None:
                self._ocr_service.end_fixed_pick()
            self._restore_settings_after_pick()

    def _restore_settings_after_pick(self) -> None:
        """Bring the settings panel back once the fixed-region drag is over."""
        if not self._settings_hidden_for_pick:
            return
        self._settings_hidden_for_pick = False
        try:
            if self._settings is not None:
                self._settings.show()
                self._settings.raise_()
        except Exception:
            logger.debug("the settings panel could not be shown again", exc_info=True)

    def _on_fixed_region_committed(self, region: Any) -> None:
        """
        The drag ended with a rectangle: save it, keep the window open.

        The picker is a *session*, not a one-shot dialog: every left-button
        release re-saves the rectangle, the window stays up so the box can be
        refined with another drag, and Esc ends the session.
        """
        if not self._fix_pick_armed:
            return
        if self._save_fixed_region(region):
            self._fix_pick_saved = True

    def _on_fixed_region_finished(self, region: Any, reason: str) -> None:
        """Leave pick mode whatever the outcome (commit, Esc, cancel, shutdown)."""
        if not self._fix_pick_armed:
            return
        already_saved = self._fix_pick_saved
        self._fix_pick_armed = False
        self._fix_pick_saved = False
        if self._ocr_service is not None:
            self._ocr_service.end_fixed_pick()
        self._restore_settings_after_pick()
        if not already_saved and region is not None:
            self._save_fixed_region(region)
        elif region is None:
            logger.debug("fixed-region pick ended without a selection (%s)", reason)

    def _save_fixed_region(self, region: Any) -> bool:
        """Persist one rectangle and refresh the settings panel if it is open."""
        service = self._ocr_service
        if service is None:
            return False
        if not service.set_fixed_region(region, save=True):
            return False
        self._notify_state("notice.region_ocr_saved")
        refresher = getattr(self._settings, "refresh_fixed_region", None)
        if callable(refresher):
            try:
                refresher()
            except Exception:
                logger.debug("the settings panel could not refresh the fixed region",
                             exc_info=True)
        return True

    def _toggle_asr(self) -> None:
        """
        Pause or resume the speech pipeline from the ASR on/off hotkey (BL-2).

        Pausing releases the loopback device, so a paused SubMirror holds no
        audio device and does no transcription work at all.  While it is off the
        "choose a Whisper model folder" hint is suppressed as well: the user
        turned speech recognition off on purpose, so warning about a missing
        model would be noise.
        """
        service = self._asr_service
        if service is None:
            logger.debug("ASR toggle ignored: the audio pipeline is unavailable")
            return
        try:
            if service.is_running:
                self._asr_off = True
                service.stop()
                self._clear_asr_hint()
                logger.info("speech recognition paused from the global hotkey")
                self._notify_state("notice.asr_paused")
                return
            self._asr_off = False
            started = bool(service.start())
            logger.info("speech recognition resumed from the global hotkey (started=%s)",
                        started)
            if started:
                self._notify_state("notice.asr_resumed")
        except Exception:
            logger.exception("the ASR toggle failed")

    def _clear_asr_hint(self) -> None:
        """Drop the ASR hint from the overlay once speech recognition is off."""
        overlay = self._overlay
        if overlay is None:
            return
        try:
            current = ""
            for reader in ("stats", "snapshot"):
                probe = getattr(overlay, reader, None)
                if not callable(probe):
                    continue
                try:
                    current = str((probe() or {}).get("notice", "") or "")
                except Exception:
                    current = ""
                if current:
                    break
            if not current:
                current = str(getattr(overlay, "_notice", "") or "")
            if current and current == _tr(self._config, "notice.asr_unavailable"):
                overlay.clear_notice()
        except Exception:
            logger.debug("the ASR hint could not be cleared", exc_info=True)

    def _notify_state(self, text_key: str) -> None:
        """Show a short overlay notice and mirror it to the tray balloon."""
        message = _tr(self._config, text_key)
        if self._overlay is not None:
            try:
                self._overlay.set_notice(message)
            except Exception:
                logger.debug("overlay notice failed", exc_info=True)
        if self._tray is not None:
            try:
                self._tray.notify(message)
            except Exception:
                logger.debug("tray notification failed", exc_info=True)

    def _show_about(self) -> None:
        if not _QT_AVAILABLE:
            return
        try:
            box = QMessageBox()
            box.setWindowTitle(_tr(self._config, "dialog.about.title"))
            box.setText(_tr(self._config, "dialog.about.body", version=APP_VERSION))
            box.setIcon(QMessageBox.Icon.Information)
            box.exec()
        except Exception:
            logger.exception("the about dialog failed")

    def _on_quit_requested(self) -> None:
        logger.info("quit requested from the tray")
        self.shutdown()
        if self._app is not None:
            self._app.quit()

    # -- settings ---------------------------------------------------------- #

    def open_settings(self) -> None:
        if not self._settings_ok or not _QT_AVAILABLE:
            logger.warning("the settings panel is not available")
            return
        try:
            if self._settings is None:
                self._settings = SettingsDialog(
                    self._config, context=self._context, overlay=self._overlay,
                    hotkey_manager=self._hotkeys, scheduler=self._scheduler,
                    audio_capture=self._capture)
                self._settings.settings_applied.connect(self._on_settings_applied)
                self._settings.ui_language_changed.connect(self._on_ui_language_changed)
                self._settings.tasks_reset.connect(self._on_tasks_reset)
                self._settings.pick_requested.connect(self._pick_fixed_region)
                # v0.1.19: a mode switch takes effect immediately, without Save.
                self._settings.mode_selected.connect(self._on_mode_selected)
                self._settings.failed.connect(
                    lambda reason: logger.warning("settings panel: %s", reason))
            loader = getattr(self._settings, "load_from_config", None)
            if callable(loader):
                loader()
            # The panel must not refuse to save over a Whisper folder that is not
            # in use: while ASR is switched off the path is irrelevant.
            setter = getattr(self._settings, "set_asr_enabled", None)
            if callable(setter):
                setter(not self._asr_off)
            self._settings.show()
            self._settings.raise_()
            self._settings.activateWindow()
        except Exception:
            logger.exception("the settings panel could not be opened")

    def _on_settings_applied(self, values: Any) -> None:
        logger.info("settings applied: %d key(s)", len(values or {}))
        # The new level has to take effect now: re-level before anything else
        # logs, so a user who just turned on debug logging actually sees it.
        try:
            set_log_level(resolve_log_level(self._config))
        except Exception:
            logger.debug("the log level could not be applied", exc_info=True)
        self._apply_runtime_config()
        # 0.1.18: the logon entry must follow app.autostart / libre_autostart.
        self._sync_silent_start()

    def _on_mode_selected(self, mode: str) -> None:
        """v0.1.19: adopt a work mode the moment the user picks it."""
        logger.info("work mode switched to %s", mode)
        try:
            self._apply_runtime_config()
        except Exception:
            logger.exception("the work mode could not be applied at runtime")

    def _on_ui_language_changed(self, language: str) -> None:
        logger.info("UI language switched to %s", language)
        try:
            self._overlay.apply_config()
        except Exception:
            logger.debug("overlay retranslation failed", exc_info=True)
        if self._tray is not None:
            self._tray.retranslate()

    def _on_tasks_reset(self) -> None:
        """Spec item 10: reset audio + OCR background work without a restart."""
        logger.info("resetting background tasks on request")
        try:
            if self._scheduler is not None:
                self._scheduler.reset_all()
        except Exception:
            logger.exception("the scheduler could not be reset")
        if self._asr_service is not None:
            self._asr_service.reset_background_tasks()
        if self._ocr_service is not None:
            self._ocr_service.reset_background_tasks()
        if self._overlay is not None:
            self._overlay.set_notice(_tr(self._config, "notice.tasks_reset"))

    def _apply_mode_params(self) -> None:
        """
        v0.1.19: (re-)load the selected work mode's recommended parameters.

        Runs before anything reads the config so a mode change made in the panel
        is in force for the very same save. It is applied when the mode itself
        changes (or on the first call after start): re-applying on every settings
        save would silently discard parameters the user edited by hand in the
        capture/ASR pages. With ``app.mode_auto_params`` off the config file wins
        and nothing is overwritten.
        """
        try:
            auto = bool(_cfg_get(self._config, "app.mode_auto_params", True))
            if not auto:
                return
            mode = app_modes.normalize_mode(_cfg_get(self._config, "app.mode", ""))
            if mode == getattr(self, "_applied_mode", None):
                return
            applied = app_modes.apply_mode(self._config, mode)
            self._applied_mode = mode
            logger.debug("work mode %s: %d parameter(s) applied", mode, len(applied or {}))
        except Exception:
            logger.exception("the work mode parameters could not be applied")

    def _apply_runtime_config(self) -> None:
        """Push the new config into every component that caches a value."""
        self._apply_mode_params()
        for name, target in (("subtitles", self._subtitles), ("overlay", self._overlay),
                             ("text", self._text), ("translator", self._translator),
                             ("asr engine", self._asr), ("capture", self._capture)):
            if target is None:
                continue
            applier = getattr(target, "apply_config", None)
            if not callable(applier):
                continue
            try:
                applier()
            except Exception:
                logger.exception("%s could not apply the new settings", name)
        for service in (self._asr_service, self._ocr_service):
            if service is None:
                continue
            try:
                service.apply_config()
            except Exception:
                logger.exception("a pipeline could not apply the new settings")
        if self._ocr_service is not None:
            try:
                self._ocr_service.sync_fixed_region()
            except Exception:
                logger.exception("the fixed-region loop could not be re-synced")

    # -- shutdown ---------------------------------------------------------- #

    def shutdown(self) -> None:
        """Stop the pipelines, drain the threads, then release the hotkeys."""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("SubMirror is shutting down")
        if self._overlay is not None:
            try:
                self._overlay.set_status("unknown", "")
            except Exception:
                logger.debug("could not set the final overlay status", exc_info=True)

        # 1) audio + OCR background work
        for name, service in (("OCR", self._ocr_service), ("audio", self._asr_service)):
            if service is None:
                continue
            try:
                service.stop(SHUTDOWN_TIMEOUT_S)
            except Exception:
                logger.exception("the %s pipeline did not stop cleanly", name)

        # 2) drain the worker threads
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(timeout=SHUTDOWN_TIMEOUT_S, cancel_pending=True)
            except Exception:
                logger.exception("the task scheduler did not shut down cleanly")

        # 3) release the hotkeys last, so no new work can be triggered meanwhile
        if self._hotkeys is not None:
            try:
                self._hotkeys.shutdown(timeout=SHUTDOWN_TIMEOUT_S)
            except Exception:
                logger.exception("the hotkey manager did not shut down cleanly")

        # 4) the rest of the UI
        if self._filter is not None and self._app is not None:
            try:
                self._app.removeNativeEventFilter(self._filter)
            except Exception:
                logger.debug("could not remove the activation filter", exc_info=True)
            self._filter = None
        if self._settings is not None:
            try:
                self._settings.close()
            except Exception:
                logger.debug("settings teardown failed", exc_info=True)
            self._settings = None
        if self._tray is not None:
            self._tray.shutdown()
        if self._overlay is not None:
            try:
                self._overlay.shutdown()
            except Exception:
                logger.debug("overlay teardown failed", exc_info=True)
        if self._subtitles is not None:
            try:
                self._subtitles.stop(timeout=2.0)
            except Exception:
                logger.debug("subtitle queues did not stop cleanly", exc_info=True)
        if self._translator is not None:
            try:
                self._translator.close()
            except Exception:
                logger.debug("translator teardown failed", exc_info=True)
        if self._ocr_engine is not None:
            try:
                self._ocr_engine.unload()
            except Exception:
                logger.debug("OCR engine teardown failed", exc_info=True)
        if self._asr is not None:
            try:
                self._asr.close()
            except Exception:
                logger.debug("Whisper teardown failed", exc_info=True)
        if self._context is not None:
            try:
                self._context.shutdown()
            except Exception:
                logger.debug("context teardown failed", exc_info=True)
        logger.info("SubMirror stopped after %.1f s",
                    max(0.0, time.monotonic() - self._started_at))

    # -- diagnostics ------------------------------------------------------- #

    def stats(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"uptime_s": round(max(0.0, time.monotonic() - self._started_at), 1)}
        for key, value in (("scheduler", self._scheduler), ("audio", self._asr_service),
                           ("ocr", self._ocr_service), ("capture", self._capturer)):
            if value is None:
                continue
            try:
                data[key] = value.stats()
            except Exception:
                data[key] = {}
        return data


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    """Set everything up and run the Qt event loop."""
    args = list(sys.argv[1:] if argv is None else argv)
    # Quiet by default (warnings and errors only); the configured level is
    # applied as soon as the config has been read, a few lines below.
    setup_console_logging(logging.WARNING)

    if "--version" in args:
        print("%s %s" % (APP_NAME, APP_VERSION))
        return EXIT_OK

    if not _QT_AVAILABLE:
        logger.error("PySide6 is not installed; run: pip install PySide6")
        return EXIT_NO_QT

    # QApplication exists before any dialog so failures can still be reported.
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setApplicationVersion(APP_VERSION)
        app.setQuitOnLastWindowClosed(False)
    except Exception:
        logger.exception("Qt could not start")
        return EXIT_NO_QT

    context = init_app_context()
    ok, status = context.initialize()
    if not ok:
        if status == INIT_ALREADY_RUNNING:
            logger.info("another SubMirror instance is already running; activating it")
            try:
                context.single_instance.notify_existing()
            except Exception:
                logger.debug("could not ask the other instance to show up", exc_info=True)
            return EXIT_ALREADY_RUNNING
        logger.error("SubMirror could not initialise: %s", status)
        _warn_dialog(get_config(), "dialog.fatal.title", "dialog.fatal.body",
                     app=APP_NAME, reason=status)
        return EXIT_INIT_FAILED

    config = getattr(context, "config", None) or get_config()
    if config is None:
        logger.error("the configuration could not be loaded")
        _warn_dialog(None, "dialog.fatal.title", "dialog.fatal.body",
                     app=APP_NAME, reason="config-unavailable")
        return EXIT_INIT_FAILED

    # Logging level (item 4): the console stays quiet unless the user raised it.
    set_log_level(resolve_log_level(config))

    # Spec item 6: a corrupt/partially invalid config is repaired and announced.
    if notify_config_reset is not None:
        try:
            notify_config_reset(config)
        except Exception:
            logger.exception("the config notice failed")
    elif str(getattr(config, "status", "")) in (LOAD_CORRUPT, LOAD_PARTIAL):
        _warn_dialog(config, "dialog.notice.title", "dialog.fatal.body",
                     app=APP_NAME, reason=str(getattr(config, "status", "")))

    permission_error = str(getattr(context, "permission_error", "") or "")
    if permission_error:
        _warn_dialog(config, "dialog.permission.title", "dialog.permission.body",
                     path=str(getattr(context, "app_data_dir", "") or permission_error))

    # The disclaimer gates both the overlay and the settings panel.
    if not _run_disclaimer(config):
        logger.warning("the disclaimer was not accepted; SubMirror will not start")
        try:
            context.shutdown()
        except Exception:
            logger.debug("context teardown failed", exc_info=True)
        return EXIT_OK

    controller = SubMirrorApp(app, context, config)
    if not controller.start():
        logger.error("SubMirror failed to start")
        try:
            controller.shutdown()
        except Exception:
            logger.debug("cleanup after a failed start did not finish", exc_info=True)
        _warn_dialog(config, "dialog.fatal.title", "dialog.fatal.body",
                     app=APP_NAME, reason="start-failed")
        return EXIT_START_FAILED

    app.aboutToQuit.connect(controller.shutdown)
    try:
        return int(app.exec())
    except Exception:
        logger.exception("the Qt event loop failed")
        return EXIT_START_FAILED
    finally:
        try:
            controller.shutdown()
        except Exception:
            logger.debug("final shutdown was incomplete", exc_info=True)


if __name__ == "__main__":
    raise SystemExit(main())
