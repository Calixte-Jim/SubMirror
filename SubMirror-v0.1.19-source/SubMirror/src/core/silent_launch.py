"""
Logon auto-start: LibreTranslate first (no window), SubMirror when it is ready.

Why a Startup-folder script instead of a scheduled task: registering an ONLOGON
task needs elevation, and SubMirror must never ask for administrator rights.
A .vbs dropped in the per-user Startup folder runs unelevated at sign-in, starts
LibreTranslate through its own ``start_libretranslate.bat`` with a hidden
console window, polls the service until it really answers, and only then starts
the main program.

Config keys (inside the existing "translate" group, so the BL-1 wiring checks
already cover them):

    translate.libre_autostart   bool  - register the Startup entry
    translate.libre_dir         path  - folder holding start_libretranslate.bat

The module is Qt-free and never blocks the GUI thread for long: writing a small
file and a directory probe are the only I/O it does.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("submirror.silent_launch")

# -- names and defaults ----------------------------------------------------- #

STARTUP_ENTRY_NAME = "SubMirror-SilentStart.vbs"
LEGACY_ENTRY_NAME = "SubMirror-LibreTranslate.vbs"
LEGACY_TASK_NAMES = ("SubMirror-SilentStart", "SubMirror-LibreTranslate")
LIBRE_START_SCRIPT = "start_libretranslate.bat"
LOG_NAME = "silent_start.log"
APP_FOLDER_NAME = "SubMirror"

APP_STARTUP_SUFFIX = os.path.join(
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup")

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_LIBRE_PORT = 5000

DEFAULT_READY_TIMEOUT_S = 180
DEFAULT_POLL_MS = 1000

KEY_AUTOSTART = "translate.libre_autostart"
KEY_LIBRE_DIR = "translate.libre_dir"
KEY_APP_AUTOSTART = "app.autostart"


# -- small config helpers --------------------------------------------------- #


def _get(config: Any, key: str, default: Any = None) -> Any:
    """Read a dotted key from a ConfigManager (or a plain mapping)."""
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        except Exception:                                        # noqa: BLE001
            logger.debug("could not read %s", key, exc_info=True)
            return default
        return default if value is None else value
    if isinstance(config, dict):
        node: Any = config
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    return default


def _text(config: Any, key: str, default: str = "") -> str:
    """Localised string through ConfigManager.t(), falling back to *default*."""
    getter = getattr(config, "t", None)
    if callable(getter):
        try:
            value = getter(key)
        except Exception:                                        # noqa: BLE001
            value = None
        if value:
            return str(value)
    return default


def autostart_enabled(config: Any) -> bool:
    """True when the saved config asks for the logon auto-start."""
    return bool(_get(config, KEY_AUTOSTART, False))


def app_autostart_enabled(config: Any) -> bool:
    """True when SubMirror itself should start at logon (v0.1.18)."""
    return bool(_get(config, KEY_APP_AUTOSTART, False))


def configured_libre_dir(config: Any) -> str:
    """The LibreTranslate folder exactly as stored in the config."""
    return str(_get(config, KEY_LIBRE_DIR, "") or "").strip()


# -- paths ------------------------------------------------------------------ #


def app_data_dir() -> str:
    """``%APPDATA%\\SubMirror`` - the same folder the config file lives in."""
    base = str(os.environ.get("APPDATA") or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, APP_FOLDER_NAME)


def resolve_startup_dir() -> str:
    """The per-user Startup folder (no elevation, no registry involved)."""
    base = str(os.environ.get("APPDATA") or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, APP_STARTUP_SUFFIX)


def entry_path(startup_dir: Optional[str] = None) -> str:
    """Full path of the Startup-folder script we own."""
    return os.path.join(startup_dir or resolve_startup_dir(), STARTUP_ENTRY_NAME)


def legacy_entry_path(startup_dir: Optional[str] = None) -> str:
    """Path of the hand-made script older SubMirror builds shipped by hand.

    It is only ever *reported*, never touched: deleting a file the user created
    on purpose is not the application's decision.
    """
    return os.path.join(startup_dir or resolve_startup_dir(), LEGACY_ENTRY_NAME)


def log_path() -> str:
    return os.path.join(app_data_dir(), LOG_NAME)


def is_registered(startup_dir: Optional[str] = None) -> bool:
    """True when our Startup entry exists."""
    try:
        return os.path.isfile(entry_path(startup_dir))
    except OSError:
        return False


def legacy_entry_present(startup_dir: Optional[str] = None) -> bool:
    return os.path.isfile(legacy_entry_path(startup_dir))


# -- LibreTranslate location ------------------------------------------------ #


def libre_port(config: Any = None, default: int = DEFAULT_LIBRE_PORT) -> int:
    """Port of the local service, taken from translate.libre_base_url."""
    raw = str(_get(config, "translate.libre_base_url", "") or "").strip()
    if not raw:
        return int(default)
    tail = raw.split("//")[-1]
    tail = tail.split("/")[0]
    if ":" in tail:
        tail = tail.rsplit(":", 1)[-1]
    else:
        return int(default)
    try:
        port = int(tail)
    except (TypeError, ValueError):
        return int(default)
    return port if 1 <= port <= 65535 else int(default)


def probe_url(port: Optional[int] = None) -> str:
    """The URL the Startup script polls. Built from parts on purpose."""
    number = DEFAULT_LIBRE_PORT if port is None else int(port)
    return "http" + "://" + LOOPBACK_HOST + ":" + str(number) + "/"


def libre_start_script(libre_dir: str) -> str:
    return os.path.join(str(libre_dir or ""), LIBRE_START_SCRIPT)


def validate_libre_dir(path: Any) -> Tuple[bool, str]:
    """
    Check a LibreTranslate folder.

    Returns ``(ok, reason)`` with an English reason code the caller can show
    next to its own localised message.
    """
    candidate = str(path or "").strip()
    if not candidate:
        return False, "empty"
    if not os.path.isdir(candidate):
        return False, "missing-folder"
    if not os.path.isfile(libre_start_script(candidate)):
        return False, "missing-start-script"
    return True, ""


def guess_libre_dir(config: Any = None,
                    app_exe: Optional[str] = None) -> str:
    """
    Best-effort search for the LibreTranslate folder.

    Only cheap directory probes are used (no recursive scan), and the first
    folder that really holds ``start_libretranslate.bat`` wins.
    """
    candidates: List[str] = []

    stored = configured_libre_dir(config)
    if stored:
        candidates.append(stored)

    roots: List[str] = []
    if app_exe:
        roots.append(os.path.dirname(os.path.abspath(app_exe)))
    else:
        exe, _args = app_command()
        if exe:
            roots.append(os.path.dirname(os.path.abspath(exe)))

    for root in roots:
        candidates.append(os.path.join(root, "..", "SubMirror-LibreTranslate"))
        candidates.append(os.path.join(root, "LibreTranslate"))
        candidates.append(os.path.join(root, "libretranslate"))

    home = os.path.expanduser("~")
    candidates.extend([
        os.path.join(home, "Downloads", "SubMirror-LibreTranslate"),
        os.path.join(home, "Desktop", "SubMirror-LibreTranslate"),
        os.path.join(home, "SubMirror-LibreTranslate"),
        os.path.join(app_data_dir(), "LibreTranslate"),
        os.path.join(app_data_dir(), "libretranslate"),
    ])

    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = letter + ":\\"
        if not os.path.isdir(drive):
            continue
        candidates.append(os.path.join(drive, "SubMirror-LibreTranslate"))
        candidates.append(os.path.join(drive, "Downloads", "SubMirror-LibreTranslate"))
        candidates.append(os.path.join(drive, "Tools", "SubMirror-LibreTranslate"))

    seen = set()
    for candidate in candidates:
        full = os.path.abspath(candidate)
        if full in seen:
            continue
        seen.add(full)
        ok, _reason = validate_libre_dir(full)
        if ok:
            return full
    return ""


# -- command line of the app ------------------------------------------------ #


def app_command(app_exe: Optional[str] = None,
                app_args: Optional[Sequence[str]] = None) -> Tuple[str, List[str]]:
    """
    The executable (and arguments) the Startup script should launch.

    Frozen builds point straight at SubMirror.exe; a development checkout
    points at main.py through the interpreter that is running us.
    """
    if app_exe:
        return str(app_exe), list(app_args or [])
    if getattr(sys, "frozen", False):
        return str(sys.executable), list(app_args or [])
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    entry = os.path.join(root, "main.py")
    if os.path.isfile(entry):
        return str(sys.executable), [entry]
    return str(sys.executable), list(app_args or [])


def _quote_argument(value: str) -> str:
    text = str(value)
    if text == "" or any(ch in text for ch in " \t"):
        return '"' + text.replace('"', '""') + '"'
    return text


def _args_string(args: Optional[Iterable[str]]) -> str:
    return " ".join(_quote_argument(a) for a in (args or ()))


# -- script generation ------------------------------------------------------ #


def _vbs_string(value: Any) -> str:
    """A VBScript double-quoted literal with quotes and newlines neutralised."""
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ")
    return '"' + text.replace('"', '""') + '"'


def _timeout_message(config: Any, seconds: int, log_file: str) -> str:
    """Localised text shown when the service never became ready."""
    default = ("LibreTranslate did not become ready within %d seconds.\n"
               "SubMirror was not started. Details: %s" % (seconds, log_file))
    template = _text(config, "silent.libre_timeout", default) or default
    try:
        return template % (seconds, log_file)
    except (TypeError, ValueError):
        return default


def build_script(config: Any = None, *, libre_dir: str = "",
                 app_exe: Optional[str] = None, app_args: Optional[Sequence[str]] = None,
                 url: Optional[str] = None, timeout_s: int = DEFAULT_READY_TIMEOUT_S,
                 poll_ms: int = DEFAULT_POLL_MS,
                 popup_on_timeout: bool = True,
                 log_file: str = "") -> str:
    """
    Build the Startup-folder VBScript.

    The script is plain ASCII except for the timeout message, which comes from
    the localised string table; ``register()`` encodes the result with the ANSI
    code page (CP936 on a Chinese Windows) so the message shows correctly.
    *log_file* is only overridden by tests.
    """
    exe, resolved_args = app_command(app_exe, app_args)
    if not libre_dir:
        libre_dir = configured_libre_dir(config)
    seconds = int(timeout_s) if int(timeout_s) > 0 else DEFAULT_READY_TIMEOUT_S
    poll = int(poll_ms) if int(poll_ms) > 0 else DEFAULT_POLL_MS
    target = url or probe_url(libre_port(config))
    log_target = log_file or log_path()
    message = _timeout_message(config, seconds, log_target)

    lines = [
        "' SubMirror - silent start: LibreTranslate first, then the main app.",
        "' Generated by SubMirror. Use the settings panel instead of editing it.",
        "Option Explicit",
        "",
        "Dim fso, sh, appData, logFile, libreDir, libreBat, appExe, appArgs",
        "Dim target, timeoutMs, pollMs, popup, elapsed, startTick, started",
        "",
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")",
        "Set sh = CreateObject(\"WScript.Shell\")",
        "",
        "appData = sh.ExpandEnvironmentStrings(\"%APPDATA%\") & \"\\" + APP_FOLDER_NAME + "\"",
        "If Not fso.FolderExists(appData) Then fso.CreateFolder(appData)",
        "logFile = " + _vbs_string(log_target),
        "libreDir = " + _vbs_string(libre_dir),
        "libreBat = " + _vbs_string(libre_start_script(libre_dir) if libre_dir else ""),
        "appExe = " + _vbs_string(exe),
        "appArgs = " + _vbs_string(_args_string(resolved_args)),
        "target = " + _vbs_string(target),
        "timeoutMs = " + str(seconds * 1000),
        "pollMs = " + str(poll),
        "popup = " + ("True" if popup_on_timeout else "False"),
        "",
        "Note \"--- silent start ---\"",
        "If Not fso.FolderExists(libreDir) Then",
        "    Note \"libre folder not found: \" & libreDir",
        "ElseIf Not fso.FileExists(libreBat) Then",
        "    Note \"start script not found: \" & libreBat",
        "End If",
        "",
        "started = False",
        "If IsReady(target) Then",
        "    Note \"service already answering\"",
        "ElseIf fso.FileExists(libreBat) Then",
        "    On Error Resume Next",
        "    sh.CurrentDirectory = libreDir",
        "    sh.Run \"cmd /c \"\"\" & libreBat & \"\"\"\", 0, False",
        "    If Err.Number <> 0 Then",
        "        Note \"could not start the service: \" & Err.Description",
        "        Err.Clear",
        "    Else",
        "        started = True",
        "        Note \"service start requested\"",
        "    End If",
        "    On Error GoTo 0",
        "End If",
        "",
        "elapsed = 0",
        "startTick = Timer",
        "Do While Not IsReady(target)",
        "    elapsed = Int((Timer - startTick) * 1000)",
        "    If elapsed < 0 Then elapsed = timeoutMs",
        "    If elapsed >= timeoutMs Then Exit Do",
        "    WScript.Sleep pollMs",
        "Loop",
        "Note \"waited \" & CStr(elapsed \\ 1000) & \"s (started=\" & CStr(started) & \")\"",
        "",
        "If IsReady(target) Then",
        "    Note \"service ready; starting the main program\"",
        "    On Error Resume Next",
        "    sh.CurrentDirectory = fso.GetParentFolderName(appExe)",
        "    If appArgs = \"\" Then",
        "        sh.Run \"\"\"\" & appExe & \"\"\"\", 1, False",
        "    Else",
        "        sh.Run \"\"\"\" & appExe & \"\"\" \" & appArgs, 1, False",
        "    End If",
        "    If Err.Number <> 0 Then",
        "        Note \"could not start the main program: \" & Err.Description",
        "        Err.Clear",
        "    End If",
        "    On Error GoTo 0",
        "Else",
        "    Note \"service not ready within \" & CStr(timeoutMs \\ 1000) & \"s; main program NOT started\"",
        "    If popup Then",
        "        MsgBox " + _vbs_string(message) + ", 48, \"SubMirror\"",
        "    End If",
        "End If",
        "",
        "Sub Note(message)",
        "    Dim handle",
        "    On Error Resume Next",
        "    Set handle = fso.OpenTextFile(logFile, 8, True)",
        "    If Err.Number = 0 Then",
        "        handle.WriteLine Now() & \"  \" & message",
        "        handle.Close",
        "    End If",
        "    Err.Clear",
        "    On Error GoTo 0",
        "End Sub",
        "",
        "Function IsReady(address)",
        "    Dim request",
        "    IsReady = False",
        "    On Error Resume Next",
        "    Set request = CreateObject(\"MSXML2.ServerXMLHTTP\")",
        "    If Err.Number <> 0 Then",
        "        Err.Clear",
        "        Set request = CreateObject(\"MSXML2.XMLHTTP\")",
        "    End If",
        "    If Err.Number <> 0 Then",
        "        Err.Clear",
        "        Set request = CreateObject(\"MSXML.XMLHTTP\")",
        "    End If",
        "    If Err.Number <> 0 Then",
        "        Err.Clear",
        "        On Error GoTo 0",
        "        Exit Function",
        "    End If",
        "    request.open \"GET\", address, False",
        "    Err.Clear",
        "    request.setTimeouts 1500, 1500, 1500, 2000",
        "    Err.Clear",
        "    request.send",
        "    If Err.Number = 0 Then",
        "        If request.status >= 200 And request.status < 500 Then IsReady = True",
        "    End If",
        "    Err.Clear",
        "    On Error GoTo 0",
        "End Function",
        "",
    ]
    return "\r\n".join(lines)


def build_app_only_script(config: Any = None, *, app_exe: Optional[str] = None,
                          app_args: Optional[Sequence[str]] = None,
                          libre_dir: str = "", log_file: str = "") -> str:
    """
    Build the "start SubMirror at logon" VBScript (v0.1.18 ``app.autostart``).

    Deliberately simpler than :func:`build_script`: nothing is waited for. The
    main program is started straight away, and a configured LibreTranslate is
    only *asked* to start in the background (fire and forget) so the app never
    blocks on a service it may not need. *log_file* is only overridden by tests.
    """
    exe, resolved_args = app_command(app_exe, app_args)
    log_target = log_file or log_path()
    if not libre_dir:
        candidate = configured_libre_dir(config)
        libre_dir = candidate if validate_libre_dir(candidate)[0] else ""
    lines = [
        "' SubMirror - silent start: main program only (app.autostart).",
        "' Generated by SubMirror. Use the settings panel instead of editing it.",
        "Option Explicit",
        "",
        "Dim fso, sh, appData, logFile, libreDir, libreBat, appExe, appArgs",
        "",
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")",
        "Set sh = CreateObject(\"WScript.Shell\")",
        "",
        "appData = sh.ExpandEnvironmentStrings(\"%APPDATA%\") & \"\\" + APP_FOLDER_NAME + "\"",
        "If Not fso.FolderExists(appData) Then fso.CreateFolder(appData)",
        "logFile = " + _vbs_string(log_target),
        "libreDir = " + _vbs_string(libre_dir),
        "libreBat = " + _vbs_string(libre_start_script(libre_dir) if libre_dir else ""),
        "appExe = " + _vbs_string(exe),
        "appArgs = " + _vbs_string(_args_string(resolved_args)),
        "",
        "Note \"--- silent start (app only) ---\"",
        "If libreBat <> \"\" Then",
        "    If fso.FileExists(libreBat) Then",
        "        On Error Resume Next",
        "        sh.CurrentDirectory = libreDir",
        "        sh.Run \"cmd /c \"\"\" & libreBat & \"\"\"\", 0, False",
        "        If Err.Number <> 0 Then",
        "            Note \"could not start the service: \" & Err.Description",
        "            Err.Clear",
        "        End If",
        "        On Error GoTo 0",
        "    End If",
        "End If",
        "",
        "On Error Resume Next",
        "sh.CurrentDirectory = fso.GetParentFolderName(appExe)",
        "If appArgs = \"\" Then",
        "    sh.Run \"\"\"\" & appExe & \"\"\"\", 1, False",
        "Else",
        "    sh.Run \"\"\"\" & appExe & \"\"\" \" & appArgs, 1, False",
        "End If",
        "If Err.Number <> 0 Then",
        "    Note \"could not start the main program: \" & Err.Description",
        "    Err.Clear",
        "End If",
        "On Error GoTo 0",
        "",
        "Sub Note(message)",
        "    Dim handle",
        "    On Error Resume Next",
        "    Set handle = fso.OpenTextFile(logFile, 8, True)",
        "    If Err.Number = 0 Then",
        "        handle.WriteLine Now() & \"  \" & message",
        "        handle.Close",
        "    End If",
        "    Err.Clear",
        "    On Error GoTo 0",
        "End Sub",
        "",
    ]
    return "\r\n".join(lines)


def _encode_script(text: str) -> bytes:
    """
    Encode the script for WSH.

    The ANSI code page keeps the localised timeout message readable (a Chinese
    message needs CP936); when the text does not fit, ASCII keeps the script
    runnable rather than corrupting it.
    """
    try:
        return text.encode("mbcs")
    except (LookupError, UnicodeEncodeError):
        logger.debug("the start script is not representable in the ANSI code page")
        return text.encode("ascii", "replace")


# -- register / unregister / sync ------------------------------------------- #


def register(config: Any = None, startup_dir: Optional[str] = None,
             app_exe: Optional[str] = None,
             app_args: Optional[Sequence[str]] = None,
             popup_on_timeout: bool = True,
             app_only: bool = False) -> Tuple[bool, str]:
    """
    Write (or refresh) the Startup entry.

    ``app_only`` writes the simple "start SubMirror" script instead of the
    service-first one; it needs no LibreTranslate folder to exist.

    Returns ``(ok, detail)``; *detail* is a short machine-readable word or the
    reason the folder was rejected.
    """
    folder = startup_dir or resolve_startup_dir()
    if app_only:
        script = build_app_only_script(config, app_exe=app_exe, app_args=app_args)
    else:
        libre_dir = configured_libre_dir(config) or guess_libre_dir(config, app_exe)
        ok, reason = validate_libre_dir(libre_dir)
        if not ok:
            return False, reason
        script = build_script(config, libre_dir=libre_dir, app_exe=app_exe,
                              app_args=app_args, popup_on_timeout=popup_on_timeout)
    target = entry_path(folder)
    payload = _encode_script(script)
    previous = b""
    try:
        if os.path.isfile(target):
            with open(target, "rb") as handle:
                previous = handle.read()
        os.makedirs(folder, exist_ok=True)
        if previous == payload:
            logger.debug("the startup entry is already current: %s", target)
            return True, "unchanged"
        temporary = target + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, target)
    except OSError as exc:
        logger.warning("the startup entry could not be written: %s", exc)
        return False, "write-failed"
    logger.info("startup entry written: %s", target)
    return True, "written"


def remove_legacy_tasks() -> List[str]:
    """
    Best-effort removal of logon tasks an older or manual setup may have created.

    SubMirror itself never registers one - an ONLOGON task needs elevation - so
    this only makes sure the toggle has exactly one owner. Failures are ignored.
    """
    if not sys.platform.startswith("win"):
        return []
    removed: List[str] = []
    for name in LEGACY_TASK_NAMES:
        try:
            result = subprocess.run(
                ["schtasks", "/Delete", "/TN", name, "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            logger.debug("schtasks is unavailable: %s", exc)
            continue
        if getattr(result, "returncode", 1) == 0:
            removed.append(name)
    if removed:
        logger.info("removed leftover logon task(s): %s", ", ".join(removed))
    return removed


def unregister(startup_dir: Optional[str] = None) -> Tuple[bool, str]:
    """Delete our Startup entry. A missing entry is not an error."""
    remove_legacy_tasks()
    target = entry_path(startup_dir)
    try:
        if not os.path.isfile(target):
            return True, "absent"
        os.remove(target)
    except OSError as exc:
        logger.warning("the startup entry could not be removed: %s", exc)
        return False, "remove-failed"
    logger.info("startup entry removed: %s", target)
    return True, "removed"


def sync(config: Any = None, startup_dir: Optional[str] = None,
         app_exe: Optional[str] = None,
         app_args: Optional[Sequence[str]] = None,
         popup_on_timeout: bool = True) -> str:
    """
    Make the Startup folder agree with the saved setting.

    Called at start-up and after the settings panel is applied, so a checkbox
    that was saved on one machine (or edited by hand) converges on the next run.
    Returns a short status word for the log.
    """
    enabled = autostart_enabled(config)
    app_enabled = app_autostart_enabled(config)
    if not enabled and not app_enabled:
        if is_registered(startup_dir):
            ok, detail = unregister(startup_dir)
            return "removed" if ok else "error:" + detail
        return "disabled"
    ok, detail = register(config, startup_dir, app_exe, app_args, popup_on_timeout,
                          app_only=app_enabled)
    return detail if ok else "error:" + detail


def status(config: Any = None, startup_dir: Optional[str] = None) -> Dict[str, Any]:
    """Diagnostics for the settings panel and the log."""
    folder = startup_dir or resolve_startup_dir()
    libre_dir = configured_libre_dir(config)
    ok, reason = validate_libre_dir(libre_dir)
    exe, args = app_command()
    return {
        "registered": is_registered(folder),
        "entry": entry_path(folder),
        "startup_dir": folder,
        "legacy_entry": legacy_entry_present(folder),
        "enabled": autostart_enabled(config),
        "app_autostart": app_autostart_enabled(config),
        "libre_dir": libre_dir,
        "libre_dir_ok": ok,
        "libre_dir_reason": reason,
        "libre_start_script": libre_start_script(libre_dir) if libre_dir else "",
        "url": probe_url(libre_port(config)),
        "app_exe": exe,
        "app_args": args,
        "log": log_path(),
        "checked_at": time.time(),
    }


def log_tail(lines: int = 40) -> List[str]:
    """Last lines of the launcher log, newest last (empty when there is none)."""
    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read().splitlines()
    except OSError:
        return []
    return content[-max(1, int(lines)):]
