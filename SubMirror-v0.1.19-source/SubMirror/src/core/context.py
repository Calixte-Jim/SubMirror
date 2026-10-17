# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Application context: resource paths, single instance, logging.

Responsibilities
    * Resolve absolute resource paths that work both from the repository and from
      a PyInstaller bundle, while staying safe for Chinese characters and spaces.
    * Enforce a single running instance through a named Windows mutex, and let a
      second launch activate the window of the first one via a broadcast message.
      No lock files are used, so an abruptly killed process leaves nothing behind.
    * Configure console-only logging. v0.1 intentionally writes NO log file.
    * Report permission problems as return values so the UI can show a dialog
      instead of the process dying.

Safety: user-mode only. Plain path handling, a kernel mutex and a posted window
message; no process injection, no memory access, no hooks, no drivers.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from typing import Any, List, Optional, Tuple

from src.core.config import (
    APP_NAME,
    APP_VERSION,
    ConfigManager,
    is_portable,
    resolve_config_path,
)

logger = logging.getLogger("submirror.context")

__all__ = [
    "AppContext",
    "SingleInstanceGuard",
    "init_app_context",
    "get_app_context",
    "reset_app_context",
    "setup_console_logging",
    "resolve_log_level",
    "set_log_level",
    "get_log_level",
    "LOG_LEVELS",
    "DEFAULT_LOG_LEVEL",
    "get_logger",
    "ensure_directory",
    "is_frozen",
    "get_app_root",
    "get_resource_root",
    "resource_path",
    "get_assets_dir",
    "asset_path",
    "get_app_data_dir",
    "get_activate_message_id",
    "ACTIVATE_MESSAGE_NAME",
    "INIT_OK",
    "INIT_ALREADY_RUNNING",
]

# --------------------------------------------------------------------------- #
# Init result codes
# --------------------------------------------------------------------------- #

INIT_OK = "ok"
INIT_ALREADY_RUNNING = "already-running"

MUTEX_NAME = "Local\\%s.SingleInstance.v1" % APP_NAME
ACTIVATE_MESSAGE_NAME = "%s.Activate.v1" % APP_NAME

_HWND_BROADCAST = 0xFFFF
_ATTACH_PARENT_PROCESS = -1
_ERROR_ALREADY_EXISTS = 183

_IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------- #
# Win32 bindings (ctypes only, no extra dependency)
# --------------------------------------------------------------------------- #

_user32 = None
_kernel32 = None

if _IS_WINDOWS:  # pragma: no branch
    try:
        from ctypes import wintypes

        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        _kernel32.CreateMutexW.restype = wintypes.HANDLE

        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL

        _kernel32.AttachConsole.argtypes = [wintypes.DWORD]
        _kernel32.AttachConsole.restype = wintypes.BOOL

        _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        _user32.RegisterWindowMessageW.restype = wintypes.UINT

        _user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        _user32.PostMessageW.restype = wintypes.BOOL
    except Exception:  # pragma: no cover - unexpected environment problem
        logger.exception("Win32 context bindings unavailable; degrading")
        _user32 = None
        _kernel32 = None


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #

def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def get_app_root() -> str:
    """
    Writable application directory.

    Frozen: the folder that contains the executable.
    Source: the repository root (three levels above src/core/context.py).
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def get_resource_root() -> str:
    """
    Root used to locate bundled read-only resources (assets, model files).

    Frozen one-file builds extract into sys._MEIPASS; one-folder and source
    builds read from the application root.
    """
    if is_frozen():
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            return os.path.abspath(bundle)
        return os.path.dirname(os.path.abspath(sys.executable))
    return get_app_root()


def resource_path(*parts: str) -> str:
    """
    Build an absolute resource path.

    Returns native, normalized absolute paths, so folders containing Chinese
    characters or spaces work without any shell quoting.
    """
    root = get_resource_root()
    if not parts:
        return root
    return os.path.normpath(os.path.join(root, *parts))


def get_assets_dir() -> str:
    """Directory that holds assets/slang_dict.json and similar files."""
    return resource_path("assets")


def asset_path(name: str) -> str:
    """Absolute path of one asset file."""
    return os.path.normpath(os.path.join(get_assets_dir(), name))


def get_app_data_dir() -> str:
    """
    Per-user writable directory for configuration and future model storage.

    Installed build: %APPDATA%\\SubMirror. Portable build (disabled in v0.1):
    <app>\\config. Falls back to ~/.submirror when APPDATA is unavailable.
    """
    if is_portable():
        return os.path.normpath(os.path.join(get_app_root(), "config"))
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return os.path.normpath(os.path.join(appdata, APP_NAME))
    return os.path.normpath(os.path.join(os.path.expanduser("~"), "." + APP_NAME.lower()))


def default_config_path() -> str:
    """Convenience wrapper around the config module's own resolver."""
    return resolve_config_path()


def ensure_directory(path: str) -> Tuple[bool, str]:
    """
    Create *path* when missing.

    Returns (True, "") on success, or (False, detail) when the directory cannot
    be created (typically a permission problem). Never raises.
    """
    if not path:
        return False, "empty path"
    try:
        os.makedirs(path, exist_ok=True)
        return True, ""
    except OSError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)


# --------------------------------------------------------------------------- #
# Console logging (no log files in v0.1)
# --------------------------------------------------------------------------- #

_LOG_CONFIGURED = False
_LOG_LOCK = threading.Lock()
_LOG_LEVEL = logging.WARNING

# Accepted "logging.level" values. The default is deliberately WARNING: an
# ordinary session then prints only warnings and errors, so a busy ASR loop
# cannot flood the console (or a redirected log file) with debug traffic.
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "silent": logging.CRITICAL + 10,
}
DEFAULT_LOG_LEVEL = "warning"


def resolve_log_level(config: Any = None, default: Any = DEFAULT_LOG_LEVEL) -> int:
    """
    Map a configured log level (name or number) to a ``logging`` constant.

    Anything unrecognised falls back to the default instead of raising, so a
    hand-edited config can never stop the process from logging.
    """
    value: Any = default
    if config is not None:
        try:
            value = config.get("logging.level", default)
        except Exception:
            value = default
    if isinstance(value, bool):
        value = default
    if isinstance(value, int):
        return int(value)
    name = str(value or "").strip().lower()
    if name.isdigit():
        return int(name)
    return LOG_LEVELS.get(name, LOG_LEVELS[DEFAULT_LOG_LEVEL])


def get_log_level() -> int:
    """Level currently applied to the "submirror" logger tree."""
    return int(_LOG_LEVEL)


def set_log_level(level: int) -> bool:
    """
    Re-level the "submirror" logger tree at runtime.

    Called when the settings panel is saved so the new level takes effect
    without a restart. Returns False only if the logger could not be touched.
    """
    global _LOG_LEVEL
    try:
        logging.getLogger(APP_NAME.lower()).setLevel(int(level))
    except Exception:  # pragma: no cover - defensive
        logger.debug("log level could not be applied", exc_info=True)
        return False
    _LOG_LEVEL = int(level)
    return True


def _resolve_console_stream():
    """
    Return a writable console stream, or None when the process has no console.

    A windowed frozen build has no stdout; attaching to the parent console gives
    the user the v0.1 "console log" experience without ever creating a file.
    """
    if sys.stdout is not None:
        return sys.stdout
    if _IS_WINDOWS and _kernel32 is not None:
        try:
            if _kernel32.AttachConsole(_ATTACH_PARENT_PROCESS):
                stream = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                sys.stdout = stream
                return stream
        except Exception:
            pass
    if sys.stderr is not None:
        return sys.stderr
    return None


def setup_console_logging(level: int = logging.INFO) -> None:
    """
    Configure the "submirror" logger tree to print to the console only.

    Idempotent: repeated calls keep a single handler, but a later call still
    applies its level, so startup can install the console sink early (quiet,
    default WARNING) and adjust it once the config has been read.
    No FileHandler is ever installed, honouring the v0.1 requirement that logs
    are not written to disk.
    """
    global _LOG_CONFIGURED, _LOG_LEVEL
    with _LOG_LOCK:
        if _LOG_CONFIGURED:
            _LOG_LEVEL = int(level)
            try:
                logging.getLogger(APP_NAME.lower()).setLevel(int(level))
            except Exception:  # pragma: no cover - defensive
                logger.debug("log level could not be applied", exc_info=True)
            return
        root = logging.getLogger(APP_NAME.lower())
        root.setLevel(level)
        root.propagate = False

        stream = _resolve_console_stream()
        if stream is not None:
            handler = logging.StreamHandler(stream)
            handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            root.addHandler(handler)
        else:
            # No console available (e.g. detached windowed process): drop records
            # rather than crash on every log call.
            root.addHandler(logging.NullHandler())

        _LOG_CONFIGURED = True
        _LOG_LEVEL = int(level)


def get_logger(name: str = "") -> logging.Logger:
    """Return a child of the application logger, e.g. get_logger("audio")."""
    base = APP_NAME.lower()
    return logging.getLogger("%s.%s" % (base, name) if name else base)


# --------------------------------------------------------------------------- #
# Single instance guard
# --------------------------------------------------------------------------- #

_activate_message_id = 0


def get_activate_message_id() -> int:
    """
    Return the system-wide id of the activation message.

    RegisterWindowMessageW yields the same id for the same string in every
    process, so a second instance can wake the first one.
    """
    global _activate_message_id
    if _activate_message_id:
        return _activate_message_id
    if not _IS_WINDOWS or _user32 is None:
        return 0
    try:
        _activate_message_id = int(_user32.RegisterWindowMessageW(ACTIVATE_MESSAGE_NAME))
    except Exception:
        logger.exception("failed to register the activation message")
        _activate_message_id = 0
    return _activate_message_id


class SingleInstanceGuard:
    """
    Named-mutex based single instance guard.

    The kernel mutex disappears automatically when the owning process exits, so
    a crashed instance never blocks the next launch.
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle: Optional[int] = None
        self._primary = False

    @property
    def is_primary(self) -> bool:
        """True when this process is the first, running instance."""
        return self._primary

    def acquire(self) -> bool:
        """
        Try to become the primary instance.

        Returns True when this process may continue. On any unexpected Win32
        failure the guard fails open, because blocking the application is worse
        than allowing a second window.
        """
        if not _IS_WINDOWS or _kernel32 is None:
            self._primary = True
            return True
        try:
            ctypes.set_last_error(0)
            handle = _kernel32.CreateMutexW(None, False, self._name)
            if not handle:
                logger.warning("CreateMutexW failed; continuing without guard")
                self._primary = True
                return True
            last_error = ctypes.get_last_error()
            self._handle = handle
            if last_error == _ERROR_ALREADY_EXISTS:
                self._primary = False
                logger.info("another SubMirror instance is already running")
                return False
            self._primary = True
            return True
        except Exception:
            logger.exception("single instance check failed; continuing")
            self._primary = True
            return True

    def notify_existing(self) -> bool:
        """
        Ask the already running instance to show itself.

        Uses HWND_BROADCAST with the registered activation message; the running
        instance reacts through a Qt native event filter installed by main.py.
        """
        if not _IS_WINDOWS or _user32 is None:
            return False
        message_id = get_activate_message_id()
        if not message_id:
            return False
        try:
            result = _user32.PostMessageW(_HWND_BROADCAST, message_id, 0, 0)
            if not result:
                logger.warning("failed to broadcast the activation message")
            return bool(result)
        except Exception:
            logger.exception("activation broadcast failed")
            return False

    def release(self) -> None:
        """Close the mutex handle; the kernel frees the object if we were last."""
        handle = self._handle
        self._handle = None
        self._primary = False
        if handle and _kernel32 is not None:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                logger.exception("failed to close the single instance handle")


# --------------------------------------------------------------------------- #
# Application context
# --------------------------------------------------------------------------- #

class AppContext:
    """
    Small service locator assembled in main.py.

    Holds the configuration manager, the single instance guard and the resolved
    writable directories. Heavy subsystems (audio, OCR, UI) are attached by
    main.py so this module stays dependency free.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config = ConfigManager(config_path)
        self.single_instance = SingleInstanceGuard()
        self.app_data_dir = get_app_data_dir()
        self.assets_dir = get_assets_dir()
        self.config_status = ""
        self.config_issues: List[Tuple[str, str]] = []
        self.permission_error = ""
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def initialize(self) -> Tuple[bool, str]:
        """
        Prepare paths, load configuration and claim the single instance slot.

        Returns (should_continue, code). A permission problem is recorded in
        self.permission_error and does NOT abort startup, so main.py can show the
        dialog and keep running with in-memory defaults.

        Idempotent on purpose: main.py calls init_app_context() (which already
        initialises this context) and then calls initialize() again on the
        returned object. Without this guard the second pass would make the single
        instance guard meet the mutex it is already holding, CreateMutexW would
        report ERROR_ALREADY_EXISTS and the very first launch would exit with a
        phantom "another SubMirror instance is already running".
        """
        if self._initialized:
            return True, INIT_OK
        setup_console_logging()
        logger.info(
            "%s v%s starting (frozen=%s)", APP_NAME, APP_VERSION, is_frozen()
        )

        ok, detail = ensure_directory(self.app_data_dir)
        if not ok:
            self.permission_error = detail
            logger.warning("application data directory unusable: %s", detail)

        self.config_status, self.config_issues = self.config.load()
        if self.config_issues:
            logger.warning(
                "configuration repaired, %d entr%s adjusted",
                len(self.config_issues),
                "y" if len(self.config_issues) == 1 else "ies",
            )

        if not self.single_instance.acquire():
            self.single_instance.notify_existing()
            return False, INIT_ALREADY_RUNNING

        self._initialized = True
        logger.info("application context initialized, config=%s", self.config.path)
        return True, INIT_OK

    def asset(self, name: str) -> str:
        """Absolute path of one bundled asset file."""
        return os.path.normpath(os.path.join(self.assets_dir, name))

    def shutdown(self) -> None:
        """Release the single instance slot; safe to call more than once."""
        try:
            self.single_instance.release()
        except Exception:
            logger.exception("failed to release the single instance guard")
        self._initialized = False
        logger.info("application context shut down")


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #

_context: Optional[AppContext] = None
_context_lock = threading.Lock()


def init_app_context(config_path: Optional[str] = None) -> AppContext:
    """Create (or return) the process-wide AppContext and initialize it."""
    global _context
    with _context_lock:
        if _context is None:
            context = AppContext(config_path)
            context.initialize()
            _context = context
        return _context


def get_app_context() -> AppContext:
    """Return the shared AppContext, creating it when needed."""
    with _context_lock:
        if _context is None:
            return init_app_context()
        return _context


def reset_app_context() -> None:
    """Drop the shared context (used by tests and full restarts)."""
    global _context
    with _context_lock:
        if _context is not None:
            _context.shutdown()
        _context = None
