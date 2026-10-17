# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Global hotkey manager.

Design
    * Primary mechanism is the native Windows RegisterHotKey API, used with
      MOD_NOREPEAT so a held hotkey fires exactly once.
    * RegisterHotKey(hwnd=NULL) binds the hotkey to the *calling thread*, so a
      dedicated thread owns registration and runs the GetMessageW pump.
      Register / unregister / reload therefore always run on that thread; a
      reload is requested through PostThreadMessageW(WM_APP_RELOAD).
    * KEYUP detection intentionally avoids any global hook (no pynput, no
      WH_KEYBOARD_LL). After the press fires, a lightweight watcher thread polls
      GetAsyncKeyState() for the keys of the combination and stops as soon as any
      of them is released. That is a read-only poll, not a hook.
    * UI is notified exclusively through Qt signals; the worker threads never
      touch widgets. Cross-thread emits are queued automatically by Qt.

Safety: user-mode only. No process injection, no memory access, no hooks, no
drivers, no simulated input.

Graceful degradation: on a non-Windows host, or when PySide6 is unavailable, the
module still imports and can parse/validate hotkeys; registration simply fails
and is reported through the logger instead of raising.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.core.config import normalize_hotkey

logger = logging.getLogger("submirror.hotkey")

__all__ = [
    "GlobalHotkeyManager",
    "parse_hotkey",
    "check_hotkey_conflict",
    "HOTKEY_REGION",
    "HOTKEY_FULLSCREEN",
    "HOTKEY_TOGGLE_ASR",
    "HOTKEY_TOGGLE_REGION",
    "MOD_NOREPEAT",
]

# --------------------------------------------------------------------------- #
# Qt import with a minimal degraded fallback
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - import guard
    from PySide6.QtCore import QObject, Signal
    _HAS_QT = True
except Exception:  # pragma: no cover - degraded, non-Qt environments
    _HAS_QT = False

    class _FallbackSignal:
        """Minimal signal substitute used only when PySide6 is missing."""

        def __init__(self, *_args, **_kwargs) -> None:
            self._slots: List[object] = []

        def connect(self, slot) -> None:
            self._slots.append(slot)

        def disconnect(self, slot=None) -> None:
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args) -> None:
            for slot in list(self._slots):
                try:
                    slot(*args)
                except Exception:
                    logger.exception("hotkey signal slot failed")

    def Signal(*_args, **_kwargs):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()


# --------------------------------------------------------------------------- #
# Win32 constants
# --------------------------------------------------------------------------- #

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_RELOAD = 0x8000 + 1

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B

# Global hotkey ids (process local).
HOTKEY_REGION = 0xA001
HOTKEY_FULLSCREEN = 0xA002
HOTKEY_TOGGLE_ASR = 0xA003
HOTKEY_TOGGLE_REGION = 0xA004

# KEYUP watcher poll interval. Small enough to feel instant, cheap enough to
# stay well under any noticeable CPU cost.
KEYUP_POLL_INTERVAL_S = 0.015

_MOD_FLAGS = {
    "Ctrl": MOD_CONTROL,
    "Alt": MOD_ALT,
    "Shift": MOD_SHIFT,
    "Win": MOD_WIN,
}

# Physical key used by GetAsyncKeyState() to track the modifier state.
_MOD_TRACK_VK = {
    "Ctrl": VK_CONTROL,
    "Alt": VK_MENU,
    "Shift": VK_SHIFT,
    "Win": VK_LWIN,
}

_NAMED_VK = {
    "SPACE": 0x20, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B,
    "ENTER": 0x0D, "RETURN": 0x0D, "BACKSPACE": 0x08, "DELETE": 0x2E,
    "INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21,
    "PAGEDOWN": 0x22, "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25,
    "RIGHT": 0x27, "CAPSLOCK": 0x14, "PRINTSCREEN": 0x2C, "PAUSE": 0x13,
    "NUMLOCK": 0x90, "SCROLLLOCK": 0x91, "MINUS": 0xBD, "PLUS": 0xBB,
    "EQUAL": 0xBB, "COMMA": 0xBC, "PERIOD": 0xBE, "SLASH": 0xBF,
    "SEMICOLON": 0xBA, "QUOTE": 0xDE, "BACKQUOTE": 0xC0,
    "LBRACKET": 0xDB, "RBRACKET": 0xDD, "BACKSLASH": 0xDC,
}

# --------------------------------------------------------------------------- #
# Win32 API bindings
# --------------------------------------------------------------------------- #

_IS_WINDOWS = os.name == "nt"
user32 = None
kernel32 = None

if _IS_WINDOWS:  # pragma: no branch
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL

        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
        ]
        user32.GetMessageW.restype = ctypes.c_int

        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.PostThreadMessageW.restype = wintypes.BOOL

        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    except Exception:  # pragma: no cover - unexpected environment problem
        logger.exception("Win32 hotkey bindings unavailable; hotkeys disabled")
        user32 = None
        kernel32 = None


# --------------------------------------------------------------------------- #
# Pure-python hotkey parsing helpers
# --------------------------------------------------------------------------- #

def _vk_for_name(name: str) -> Optional[int]:
    """Map a canonical key name to a virtual-key code, or None."""
    upper = name.upper()
    if len(upper) == 1 and (upper.isalpha() or upper.isdigit()):
        return ord(upper)
    if upper.startswith("F") and upper[1:].isdigit():
        index = int(upper[1:])
        if 1 <= index <= 24:
            return 0x70 + index - 1
        return None
    return _NAMED_VK.get(upper)


def parse_hotkey(text: str) -> Optional[Tuple[int, int, Tuple[int, ...]]]:
    """
    Parse a hotkey string into Win32 registration data.

    Returns (modifier_flags, virtual_key, tracked_vkeys) where *tracked_vkeys*
    contains every virtual key of the combination (modifiers included) and is
    used by the KEYUP watcher. Returns None for an unusable hotkey.
    """
    normalized = normalize_hotkey(text)
    if not normalized:
        return None

    flags = 0
    main_vk: Optional[int] = None
    tracked: List[int] = []

    for part in normalized.split("+"):
        if part in _MOD_FLAGS:
            flags |= _MOD_FLAGS[part]
            tracked.append(_MOD_TRACK_VK[part])
            continue
        vk = _vk_for_name(part)
        if vk is None:
            return None
        main_vk = vk
        tracked.append(vk)

    if main_vk is None or flags == 0:
        return None
    return flags, main_vk, tuple(dict.fromkeys(tracked))


def check_hotkey_conflict(area_key: str, fullscreen_key: str,
                          toggle_key: Optional[str] = None,
                          region_toggle_key: Optional[str] = None) -> bool:
    """
    Return True when any two of the given hotkeys resolve to the same combination.

    The comparison runs on normalized form, so "ctrl+alt+a" and "Ctrl+Alt+A"
    are correctly reported as a conflict. An unset (empty) or invalid hotkey is
    ignored, which is what makes the optional ASR / fixed-region hotkeys
    optional: leaving them blank can never collide with the two OCR hotkeys.
    """
    normalized = [
        value
        for value in (normalize_hotkey(area_key),
                      normalize_hotkey(fullscreen_key),
                      normalize_hotkey(toggle_key) if toggle_key is not None else None,
                      normalize_hotkey(region_toggle_key) if region_toggle_key is not None else None)
        if value is not None
    ]
    for index, value in enumerate(normalized):
        if value in normalized[index + 1:]:
            return True
    return False


# --------------------------------------------------------------------------- #
# Bindings and manager
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Binding:
    """One successfully registered global hotkey."""

    name: str
    key: str
    vk: int
    tracked_vkeys: Tuple[int, ...]


class GlobalHotkeyManager(QObject):
    """
    Owns the two OCR global hotkeys and the optional ASR on/off hotkey.

    Signals
        region_pressed()      region OCR hotkey went down
        region_released()     region OCR hotkey came back up
        fullscreen_pressed()  fullscreen OCR hotkey fired (rate limited)
        asr_toggled()         ASR on/off hotkey fired (optional binding)
        region_toggled()      fixed-region OCR on/off hotkey fired (optional)
        registered(name, key)         a hotkey was registered
        register_failed(name, key)    a hotkey is occupied or invalid
    """

    region_pressed = Signal()
    region_released = Signal()
    fullscreen_pressed = Signal()
    asr_toggled = Signal()
    region_toggled = Signal()
    registered = Signal(str, str)
    register_failed = Signal(str, str)

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._loop_thread: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._state_lock = threading.Lock()
        self._bindings: Dict[int, _Binding] = {}
        self._region_active = False
        self._last_fullscreen = 0.0
        self._min_fullscreen_interval_s = 0.8
        self._refresh_limits()

    # -- public API -------------------------------------------------------- #

    @property
    def is_running(self) -> bool:
        """True while the registration thread is alive."""
        thread = self._loop_thread
        return bool(thread and thread.is_alive())

    @property
    def active_keys(self) -> Dict[str, str]:
        """{hotkey name: key string} for every currently registered hotkey."""
        with self._state_lock:
            return {b.name: b.key for b in self._bindings.values()}

    def start(self) -> bool:
        """
        Launch the registration thread and register the configured hotkeys.

        Returns True when at least one hotkey registered. Failures are reported
        through the register_failed signal so the UI can prompt the user, and
        never raise into the caller.
        """
        if not _IS_WINDOWS or user32 is None or kernel32 is None:
            logger.warning("global hotkeys unavailable on this platform")
            return False
        if self.is_running:
            return True

        self._refresh_limits()
        self._stop_event.clear()
        self._ready_event.clear()

        self._loop_thread = threading.Thread(
            target=self._thread_main, name="SubMirrorHotkeys", daemon=True
        )
        self._loop_thread.start()
        self._ready_event.wait(timeout=3.0)

        with self._state_lock:
            return bool(self._bindings)

    def reload(self) -> None:
        """
        Re-read the hotkeys from config and register them again.

        The work is posted to the owning thread, because a thread-bound hotkey
        must be (un)registered by the thread that created it.
        """
        if not self.is_running:
            self.start()
            return
        thread_id = self._thread_id
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_APP_RELOAD, 0, 0)

    def shutdown(self, timeout: float = 3.0) -> None:
        """
        Stop the watcher, release every hotkey and join the owning thread.

        Called during application exit so the process never leaves a hotkey or
        the audio device behind.
        """
        self._stop_event.set()
        thread_id = self._thread_id
        if thread_id and user32 is not None:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

        loop_thread = self._loop_thread
        if loop_thread and loop_thread.is_alive():
            loop_thread.join(timeout)

        watch_thread = self._watch_thread
        if watch_thread and watch_thread.is_alive():
            watch_thread.join(timeout)

        with self._state_lock:
            self._bindings.clear()
        self._thread_id = 0
        self._loop_thread = None
        self._watch_thread = None
        logger.debug("hotkey manager shut down")

    # -- internals --------------------------------------------------------- #

    def _refresh_limits(self) -> None:
        """Re-read the fullscreen rate limit from config (ms -> seconds)."""
        try:
            interval_ms = int(self._config.get("ocr.fullscreen_min_interval_ms", 800))
        except (TypeError, ValueError):
            interval_ms = 800
        self._min_fullscreen_interval_s = max(interval_ms, 0) / 1000.0

    def _thread_main(self) -> None:
        """Registration thread entry point: register, then pump messages."""
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            self._register_from_config()
        except Exception:
            logger.exception("hotkey registration thread failed to start")
        finally:
            self._ready_event.set()

        try:
            self._pump_messages()
        except Exception:
            logger.exception("hotkey message pump crashed")
        finally:
            self._unregister_all()
            self._thread_id = 0

    def _pump_messages(self) -> None:
        """Blocking GetMessage loop until WM_QUIT."""
        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0 or result == -1:
                break
            try:
                if msg.message == WM_HOTKEY:
                    self._handle_hotkey(int(msg.wParam))
                elif msg.message == WM_APP_RELOAD:
                    self._unregister_all()
                    self._refresh_limits()
                    self._register_from_config()
            except Exception:
                logger.exception("hotkey dispatch failed")

    def _register_from_config(self) -> None:
        """Register the configured hotkeys, reporting each outcome."""
        area_key = str(self._config.get("hotkeys.area_ocr", "Ctrl+Alt+A"))
        full_key = str(self._config.get("hotkeys.fullscreen_ocr", "Ctrl+Alt+S"))
        toggle_key = str(self._config.get("hotkeys.toggle_asr", "") or "").strip()
        region_key = str(self._config.get("hotkeys.toggle_region_ocr", "") or "").strip()

        self._register_one("area_ocr", area_key, HOTKEY_REGION)
        self._register_one("fullscreen_ocr", full_key, HOTKEY_FULLSCREEN)
        if toggle_key:
            # An unbound optional hotkey is skipped rather than reported as a
            # failure; only a configured-but-unusable one raises the signal.
            self._register_one("toggle_asr", toggle_key, HOTKEY_TOGGLE_ASR)
        if region_key:
            self._register_one("toggle_region_ocr", region_key, HOTKEY_TOGGLE_REGION)

        if check_hotkey_conflict(area_key, full_key, toggle_key, region_key):
            # Both cannot be registered at once; the settings UI blocks this, but
            # a hand-edited config could still contain duplicates.
            logger.warning("configured hotkeys are not unique: %s / %s / %s / %s",
                           area_key, full_key, toggle_key, region_key)

    def _register_one(self, name: str, key: str, hotkey_id: int) -> bool:
        """Register a single hotkey and emit the matching signal."""
        parsed = parse_hotkey(key)
        if parsed is None:
            logger.warning("invalid hotkey for %s: %r", name, key)
            self.register_failed.emit(name, key)
            return False

        flags, vk, tracked = parsed
        if not user32.RegisterHotKey(None, hotkey_id, flags | MOD_NOREPEAT, vk):
            error_code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            logger.warning(
                "RegisterHotKey failed for %s (%s), winerror=%s", name, key, error_code
            )
            self.register_failed.emit(name, key)
            return False

        with self._state_lock:
            self._bindings[hotkey_id] = _Binding(name, key, vk, tracked)
        logger.info("registered hotkey %s = %s", name, key)
        self.registered.emit(name, key)
        return True

    def _unregister_all(self) -> None:
        """Release every registered hotkey on the owning thread."""
        with self._state_lock:
            ids = list(self._bindings.keys())
            self._bindings.clear()
        for hotkey_id in ids:
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                logger.exception("failed to unregister hotkey id=%s", hotkey_id)
        if ids:
            logger.debug("released %d global hotkey(s)", len(ids))

    def _handle_hotkey(self, hotkey_id: int) -> None:
        """Dispatch a WM_HOTKEY message on the owning thread."""
        with self._state_lock:
            binding = self._bindings.get(hotkey_id)
            if binding is None:
                return

            if hotkey_id == HOTKEY_REGION:
                if self._region_active:
                    return
                self._region_active = True
                tracked = binding.tracked_vkeys
                self.region_pressed.emit()
                self._watch_thread = threading.Thread(
                    target=self._watch_region_release,
                    args=(tracked,),
                    name="SubMirrorRegionKey",
                    daemon=True,
                )
                self._watch_thread.start()
                return

            if hotkey_id == HOTKEY_FULLSCREEN:
                now = time.monotonic()
                if now - self._last_fullscreen < self._min_fullscreen_interval_s:
                    logger.debug("fullscreen OCR trigger ignored (debounced)")
                    return
                self._last_fullscreen = now
                self.fullscreen_pressed.emit()
                return

            if hotkey_id == HOTKEY_TOGGLE_ASR:
                self.asr_toggled.emit()
                return

            if hotkey_id == HOTKEY_TOGGLE_REGION:
                self.region_toggled.emit()

    def _watch_region_release(self, tracked_vkeys: Tuple[int, ...]) -> None:
        """
        Poll the combination keys until any of them is released, then notify.

        This replaces a global keyboard hook: it only reads key state through
        GetAsyncKeyState() and exits immediately once the hotkey is let go.
        """
        try:
            while not self._stop_event.is_set():
                pressed = all(
                    (user32.GetAsyncKeyState(vk) & 0x8000) != 0
                    for vk in tracked_vkeys
                )
                if not pressed:
                    break
                time.sleep(KEYUP_POLL_INTERVAL_S)
        except Exception:
            logger.exception("region KEYUP watcher failed")
        finally:
            with self._state_lock:
                self._region_active = False
            if not self._stop_event.is_set():
                self.region_released.emit()
