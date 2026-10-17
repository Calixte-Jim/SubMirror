# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Translation abstraction layer (official entry, item 11).

Why this module exists
    Every online translation backend shares the same hard problems: a hard
    timeout, retrying without hammering the service, a circuit breaker so a dead
    network cannot cost 3 s per subtitle line forever, and a result object the UI
    can render without knowing which service answered. This class owns all of
    that; a concrete backend only implements one method::

        _translate(text, source_lang, target_lang, timeout_s) -> str

    and raises TranslationTimeoutError / TranslationUnavailableError /
    TranslationResponseError when a single attempt fails.

Contract used by the rest of the program
    ``OnlineTranslator`` (item 12) subclasses this class, calls
    ``super().apply_config()`` and relies on ``self._get()``, ``self._config``,
    ``self.timeout_s``, ``self.stats()``, ``self._on_close()`` and the class
    attributes below. ``main.py`` calls ``translate(text, target_lang=...)`` and
    reads ``result.ok`` / ``result.translated`` / ``result.error``, plus the
    ``failed(object)`` and ``status_changed(str, str)`` signals.

Threading / UI rules
    ``translate()`` is safe to call from any worker lane of the task scheduler.
    It never touches a widget; it only emits Qt signals, and Qt delivers them to
    the GUI thread through queued connections. No log file, no telemetry.
"""

from __future__ import annotations

import abc
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:  # Qt is optional: the module stays importable for tests / CLI tools
    from PySide6.QtCore import QObject, Signal  # type: ignore[import-not-found]

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - degraded (no Qt) import path
    _QT_AVAILABLE = False

    class _FallbackSignal:
        """Minimal stand-in so signal names still exist without PySide6."""

        def __init__(self, *types: Any) -> None:
            self._slots: List[Any] = []

        def connect(self, slot: Any) -> None:
            self._slots.append(slot)

        def disconnect(self, slot: Any = None) -> None:
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args: Any) -> None:
            for slot in list(self._slots):
                try:
                    slot(*args)
                except Exception:                     # noqa: BLE001
                    logger.debug("signal slot raised", exc_info=True)

    class _FallbackObject:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    QObject = _FallbackObject                         # type: ignore[assignment,misc]
    Signal = _FallbackSignal                          # type: ignore[assignment,misc]

__all__ = [
    "STATUS_OK",
    "STATUS_PASSTHROUGH",
    "STATUS_FAILED",
    "STATUS_CIRCUIT_OPEN",
    "STATUS_CANCELLED",
    "CIRCUIT_CLOSED",
    "CIRCUIT_OPEN",
    "CIRCUIT_HALF_OPEN",
    "SUPPORTED_LANGUAGES",
    "LANGUAGE_ALIASES",
    "LANGUAGE_DISPLAY",
    "normalize_language",
    "is_same_language",
    "language_display_name",
    "normalize_text",
    "TranslationError",
    "TranslationTimeoutError",
    "TranslationUnavailableError",
    "TranslationResponseError",
    "TranslationCancelledError",
    "CircuitOpenError",
    "TranslationRequest",
    "TranslationResult",
    "CircuitBreaker",
    "RetryPolicy",
    "CallThrottle",
    "BaseTranslator",
]

logger = logging.getLogger("submirror.ai.base_translator")

# --------------------------------------------------------------------------- #
# Status / state vocabulary
# --------------------------------------------------------------------------- #

STATUS_OK = "ok"                       # translated successfully
STATUS_PASSTHROUGH = "passthrough"     # nothing to do (same language / nothing)
STATUS_FAILED = "failed"               # every attempt failed
STATUS_CIRCUIT_OPEN = "circuit-open"   # breaker rejected the call
STATUS_CANCELLED = "cancelled"         # the caller's cancel token fired

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half-open"

# --------------------------------------------------------------------------- #
# Defaults (mirrored by src/core/config.py)
# --------------------------------------------------------------------------- #

DEFAULT_TIMEOUT_S = 3.0                # one user visible request, spec item 1
DEFAULT_FAILURE_THRESHOLD = 3          # consecutive failures before the breaker
DEFAULT_CIRCUIT_BREAK_S = 10.0         # breaker cool-down
DEFAULT_MAX_ATTEMPTS = 2               # attempts per call (1 retry)
DEFAULT_RETRY_DELAY_S = 0.25
DEFAULT_BACKOFF = 2.0
DEFAULT_MAX_RETRY_DELAY_S = 1.0
DEFAULT_TOTAL_BUDGET_S = 6.5           # hard ceiling for one translate() call
DEFAULT_CACHE_TTL_S = 300.0
DEFAULT_CACHE_MAX_ENTRIES = 256
DEFAULT_MAX_TEXT_CHARS = 500
DEFAULT_MIN_REQUEST_CHARS = 1
# One translate() call must never outlive the subtitle it belongs to: a game
# overlay that lags behind the speech is worse than a missing line, so a task
# deadline caps the whole call (v0.1.18 item 3).
DEFAULT_TASK_TIMEOUT_MS = 700
# Upper bound on the rolling sentence context handed to prompt-capable
# backends. Bigger context is not automatically better: the request must stay
# small so the reply still arrives inside the task deadline (item 4).
DEFAULT_CONTEXT_MAX_CHARS = 240

# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #

SUPPORTED_LANGUAGES: Tuple[str, ...] = (
    "auto", "zh-CN", "zh-TW", "en", "ja", "ko",
    "ru", "fr", "de", "es", "pt", "it",
)

LANGUAGE_ALIASES: Dict[str, str] = {
    "": "auto",
    "auto": "auto",
    "detect": "auto",
    "autodetect": "auto",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-sg": "zh-CN",
    "chs": "zh-CN",
    "chinese": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-hk": "zh-TW",
    "cht": "zh-TW",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "eng": "en",
    "english": "en",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kr": "ko",
    "kor": "ko",
    "korean": "ko",
    "ru": "ru",
    "rus": "ru",
    "french": "fr",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "es": "es",
    "spa": "es",
    "pt": "pt",
    "por": "pt",
    "it": "it",
    "ita": "it",
}

# code -> (zh-CN label, en label); used by the settings panel only
LANGUAGE_DISPLAY: Dict[str, Tuple[str, str]] = {
    "auto": ("自动检测", "Auto detect"),
    "zh-CN": ("简体中文", "Chinese (Simplified)"),
    "zh-TW": ("繁體中文", "Chinese (Traditional)"),
    "en": ("英语", "English"),
    "ja": ("日语", "Japanese"),
    "ko": ("韩语", "Korean"),
    "ru": ("俄语", "Russian"),
    "fr": ("法语", "French"),
    "de": ("德语", "German"),
    "es": ("西班牙语", "Spanish"),
    "pt": ("葡萄牙语", "Portuguese"),
    "it": ("意大利语", "Italian"),
}


def normalize_language(code: Any, default: str = "auto") -> str:
    """Map any language spelling onto a SUPPORTED_LANGUAGES entry."""
    text = str(code or "").strip()
    if not text:
        return default
    key = text.replace("_", "-").lower()
    if key in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[key]
    head = key.split("-")[0]
    if head in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[head]
    if text in SUPPORTED_LANGUAGES:
        return text
    return default


def is_same_language(left: Any, right: Any) -> bool:
    """True when both codes name the same concrete language ('auto' never does)."""
    first = normalize_language(left)
    second = normalize_language(right)
    if first == "auto" or second == "auto":
        return False
    return first == second


def language_display_name(code: Any, ui_language: str = "zh-CN") -> str:
    """Human readable language name in the current UI language."""
    normalized = normalize_language(code)
    labels = LANGUAGE_DISPLAY.get(normalized)
    if labels is None:
        return str(code or normalized)
    return labels[1] if str(ui_language).lower().startswith("en") else labels[0]


_INVISIBLE_RANGES = (
    (0x200B, 0x200F),     # zero width space .. RTL mark
    (0x202A, 0x202E),     # bidi embedding controls
    (0x2060, 0x2064),     # word joiner / invisible operators
    (0x2066, 0x2069),     # bidi isolates
    (0xFEFF, 0xFEFF),     # BOM / zero width no-break space
    (0xFFF9, 0xFFFB),     # interlinear annotation
)
_WHITESPACE_RE = re.compile(r"[ \t\u00a0\u3000\v\f]+")
_LEFT_WS_RE = re.compile(r" *\n *")


def normalize_text(text: Any) -> str:
    """
    Canonical text form: NFKC, no invisible/control/private-use characters and
    collapsed whitespace. The cache key and the length checks both use this, so
    a result must never be computed against a different spelling than the key.
    """
    if text is None:
        return ""
    raw = str(text)
    raw = unicodedata.normalize("NFKC", raw)
    kept: List[str] = []
    for char in raw:
        code = ord(char)
        if any(low <= code <= high for low, high in _INVISIBLE_RANGES):
            continue
        category = unicodedata.category(char)
        if category in ("Cc", "Cf", "Co", "Cs"):
            if char in ("\n", "\r", "\t"):
                kept.append(" " if char != "\n" else "\n")
            continue
        kept.append(char)
    cleaned = "".join(kept).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _LEFT_WS_RE.sub("\n", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class TranslationError(Exception):
    """Base class: a single attempt could not produce a translation."""


class TranslationTimeoutError(TranslationError):
    """The attempt exceeded its slice of the timeout budget."""


class TranslationUnavailableError(TranslationError):
    """The service could not be reached, refused us or is rate limited."""


class TranslationResponseError(TranslationError):
    """A response arrived but was unusable (bad JSON, HTML block page, empty)."""


class TranslationCancelledError(TranslationError):
    """The caller's cancel token fired mid-flight."""


class CircuitOpenError(TranslationError):
    """The breaker is open; ``retry_after`` holds the remaining cool-down."""

    def __init__(self, message: str = "translation circuit is open",
                 retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after or 0.0))


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #

@dataclass
class TranslationRequest:
    """What the caller asked for, before any attempt was made."""

    text: str = ""
    source_lang: str = "auto"
    target_lang: str = "auto"
    request_id: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def cache_key(self) -> Tuple[str, str, str]:
        return (self.text, self.source_lang, self.target_lang)

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "source_lang": self.source_lang,
                "target_lang": self.target_lang, "request_id": self.request_id}


@dataclass
class TranslationResult:
    """
    One finished translation call. ``translated`` is the only field the pipeline
    renders; ``status``/``error`` drive the overlay notice and the logs.
    """

    text: str = ""
    translated: str = ""
    source_lang: str = "auto"
    target_lang: str = "auto"
    provider: str = ""
    status: str = STATUS_OK
    error: str = ""
    attempts: int = 0
    from_cache: bool = False
    elapsed_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_PASSTHROUGH)

    @property
    def is_failed(self) -> bool:
        return self.status in (STATUS_FAILED, STATUS_CIRCUIT_OPEN)

    @property
    def is_empty(self) -> bool:
        return not (self.translated or "").strip()

    @property
    def circuit_open(self) -> bool:
        return self.status == STATUS_CIRCUIT_OPEN

    def display_text(self, show_original: bool = False) -> str:
        """Same semantics as SubtitleItem.display_text() for the ASR line."""
        original = (self.text or "").strip()
        translated = (self.translated or "").strip()
        if not self.ok or not translated or translated == original:
            return original
        if show_original:
            return f"{original}\n{translated}"
        return translated

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "translated": self.translated,
                "source_lang": self.source_lang, "target_lang": self.target_lang,
                "provider": self.provider, "status": self.status,
                "error": self.error, "attempts": self.attempts,
                "from_cache": self.from_cache,
                "elapsed_ms": round(self.elapsed_ms, 2)}

    @classmethod
    def failure(cls, text: str = "", error: str = "", source_lang: str = "auto",
                target_lang: str = "auto", provider: str = "",
                status: str = STATUS_FAILED, attempts: int = 0,
                elapsed_ms: float = 0.0) -> "TranslationResult":
        return cls(text=text, translated="", source_lang=source_lang,
                   target_lang=target_lang, provider=provider, status=status,
                   error=error, attempts=attempts, elapsed_ms=elapsed_ms)

    @classmethod
    def passthrough(cls, text: str = "", source_lang: str = "auto",
                    target_lang: str = "auto", provider: str = "",
                    elapsed_ms: float = 0.0) -> "TranslationResult":
        """Nothing to translate: the original text is the answer."""
        return cls(text=text, translated=text, source_lang=source_lang,
                   target_lang=target_lang, provider=provider,
                   status=STATUS_PASSTHROUGH, elapsed_ms=elapsed_ms)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #

class CircuitBreaker:
    """
    Consecutive-failure breaker: spec item 1 (three failures -> 10 s cool-down).

    One ``translate()`` call counts once, no matter how many internal attempts it
    made, so "three failures" always means three user visible requests. In the
    half-open state exactly one probe is allowed through; a success closes the
    breaker, a failure re-opens it with a fresh cool-down.
    """

    def __init__(self, failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 reset_seconds: float = DEFAULT_CIRCUIT_BREAK_S) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_seconds = max(0.1, float(reset_seconds))
        self._lock = threading.RLock()
        self._state = CIRCUIT_CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._trips = 0

    # -- state ------------------------------------------------------------- #

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == CIRCUIT_OPEN and self.retry_after() <= 0.0:
                return CIRCUIT_HALF_OPEN
            return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CIRCUIT_OPEN

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def retry_after(self) -> float:
        with self._lock:
            if self._state != CIRCUIT_OPEN:
                return 0.0
            remaining = self.reset_seconds - (time.monotonic() - self._opened_at)
            return max(0.0, remaining)

    # -- decisions --------------------------------------------------------- #

    def allow(self) -> bool:
        """True when a call may be attempted right now."""
        with self._lock:
            if self._state == CIRCUIT_CLOSED:
                return True
            if self._state == CIRCUIT_OPEN:
                if time.monotonic() - self._opened_at < self.reset_seconds:
                    return False
                # Cool-down elapsed: open the door for exactly one probe.
                self._state = CIRCUIT_HALF_OPEN
                self._probe_in_flight = True
                return True
            if self._probe_in_flight:                 # a probe is already out
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._probe_in_flight = False
            self._state = CIRCUIT_CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state == CIRCUIT_HALF_OPEN:
                self._trip()
                return
            if self._failures >= self.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = CIRCUIT_OPEN
        self._opened_at = time.monotonic()
        self._trips += 1
        self._failures = self.failure_threshold

    def reset(self) -> None:
        with self._lock:
            self._state = CIRCUIT_CLOSED
            self._failures = 0
            self._opened_at = 0.0
            self._probe_in_flight = False

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"state": self.state, "failures": self._failures,
                    "threshold": self.failure_threshold,
                    "reset_seconds": self.reset_seconds, "trips": self._trips,
                    "retry_after": round(self.retry_after(), 3)}


# --------------------------------------------------------------------------- #
# Retry policy / throttle / cache
# --------------------------------------------------------------------------- #

@dataclass
class RetryPolicy:
    """Bounded retry schedule: a small delay, a hard total budget."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    delay_seconds: float = DEFAULT_RETRY_DELAY_S
    backoff: float = DEFAULT_BACKOFF
    max_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_S
    total_budget_seconds: float = DEFAULT_TOTAL_BUDGET_S

    def delay_for(self, attempt: int) -> float:
        """Delay to wait *after* ``attempt`` failed (1-based)."""
        if attempt < 1 or self.delay_seconds <= 0.0:
            return 0.0
        grown = self.delay_seconds * (self.backoff ** max(0, attempt - 1))
        return max(0.0, min(grown, self.max_delay_seconds))

    @classmethod
    def from_config(cls, config: Any) -> "RetryPolicy":
        def value(key: str, default: float) -> float:
            getter = getattr(config, "get", None)
            if not callable(getter):
                return default
            try:
                raw = getter(key, default)
            except Exception:                              # noqa: BLE001
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        attempts = int(value("translate.max_retries", DEFAULT_MAX_ATTEMPTS))
        return cls(
            max_attempts=max(1, min(attempts, 5)),
            delay_seconds=max(0.0, value("translate.retry_delay_seconds",
                                         DEFAULT_RETRY_DELAY_S)),
            max_delay_seconds=max(0.0, value("translate.retry_max_delay_seconds",
                                             DEFAULT_MAX_RETRY_DELAY_S)),
            total_budget_seconds=max(1.0, value("translate.total_budget_seconds",
                                                DEFAULT_TOTAL_BUDGET_S)),
        )


class CallThrottle:
    """
    Minimum spacing between outbound calls. Guards against a burst of nearly
    identical subtitles turning into an API flood (spec: rate-limit protection).
    """

    def __init__(self, min_interval_seconds: float = 0.0) -> None:
        self._lock = threading.RLock()
        self._interval = max(0.0, float(min_interval_seconds or 0.0))
        self._last_call = 0.0
        self._waited = 0.0
        self._waits = 0

    @property
    def interval(self) -> float:
        with self._lock:
            return self._interval

    @property
    def is_enabled(self) -> bool:
        return self.interval > 0.0

    def configure(self, min_interval_seconds: float) -> None:
        with self._lock:
            self._interval = max(0.0, float(min_interval_seconds or 0.0))

    def wait(self, token: Any = None) -> float:
        """Sleep just long enough to respect the interval; returns the wait."""
        with self._lock:
            if self._interval <= 0.0:
                self._last_call = time.monotonic()
                return 0.0
            elapsed = time.monotonic() - self._last_call
            delay = self._interval - elapsed
            if delay > 0.0:
                self._waits += 1
                self._waited += delay
            self._last_call = time.monotonic() + max(0.0, delay)
        if delay > 0.0:
            _cancellable_sleep(delay, token)
        return max(0.0, delay)

    def reset(self) -> None:
        with self._lock:
            self._last_call = 0.0
            self._waited = 0.0
            self._waits = 0

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"interval_s": round(self._interval, 4), "waits": self._waits,
                    "waited_s": round(self._waited, 3)}


class _TranslationCache:
    """Small TTL cache: identical subtitles repeat constantly in games."""

    def __init__(self, ttl_seconds: float = DEFAULT_CACHE_TTL_S,
                 max_entries: int = DEFAULT_CACHE_MAX_ENTRIES) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
        self._ttl = max(0.0, float(ttl_seconds or 0.0))
        self._max = max(0, int(max_entries))
        self._hits = 0

    @property
    def enabled(self) -> bool:
        return self._max > 0 and self._ttl > 0.0

    def configure(self, ttl_seconds: float, max_entries: int) -> None:
        with self._lock:
            self._ttl = max(0.0, float(ttl_seconds or 0.0))
            self._max = max(0, int(max_entries))
            self._entries.clear()

    def get(self, key: Tuple[str, str, str]) -> Optional[str]:
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if now - stored_at > self._ttl:
                self._entries.pop(key, None)
                return None
            self._hits += 1
            # Refresh insertion order so the dict stays LRU-ish.
            self._entries.pop(key, None)
            self._entries[key] = (now, value)
            return value

    def put(self, key: Tuple[str, str, str], value: str) -> None:
        if not self.enabled or not value:
            return
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (time.monotonic(), value)
            while len(self._entries) > self._max:
                self._entries.pop(next(iter(self._entries)))

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "entries": len(self._entries),
                    "max_entries": self._max, "ttl_s": self._ttl,
                    "hits": self._hits}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _cancellable_sleep(seconds: float, token: Any = None, step: float = 0.05) -> None:
    """Sleep in small slices so a cancel token does not have to wait it out."""
    remaining = max(0.0, float(seconds))
    while remaining > 0.0:
        if token is not None and getattr(token, "is_cancelled", False):
            return
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk


# --------------------------------------------------------------------------- #
# Abstract base
# --------------------------------------------------------------------------- #

class BaseTranslator(QObject if _QT_AVAILABLE else object):  # type: ignore[misc]
    """
    Template method over ``_translate()``: validation -> cache -> breaker ->
    retry -> cache store. Subclasses override only ``_translate()`` and, when
    they need to read configuration, ``apply_config()``.

    Style prompt contract (0.1.17): a backend sets ``supports_prompt = True``
    and implements ``default_prompt`` when its endpoint accepts an instruction
    message; ``_translate()`` then reads :attr:`style_prompt` (empty string when
    the user switched prompts off, or the backend cannot carry one).
    """

    name = "base"
    display_name = "Base translator"
    requires_api_key = False
    supported_languages: Tuple[str, ...] = SUPPORTED_LANGUAGES
    default_timeout_s = DEFAULT_TIMEOUT_S
    max_text_chars = DEFAULT_MAX_TEXT_CHARS
    min_request_chars = DEFAULT_MIN_REQUEST_CHARS
    # Style pre-prompt (item 1, 0.1.17). A backend that can honour a prompt sets
    # supports_prompt = True; the keyless HTTP endpoints cannot and ignore it,
    # which is why the shorthand dictionary does the visible work for them.
    supports_prompt = False
    default_prompt = ""

    if _QT_AVAILABLE:
        translated = Signal(object)          # TranslationResult, success only
        failed = Signal(object)              # TranslationResult, failure only
        status_changed = Signal(str, str)    # state, human readable message
        circuit_changed = Signal(str, float)  # state, retry_after seconds

    def __init__(self, config: Any = None, parent: Any = None) -> None:
        if _QT_AVAILABLE:
            super().__init__(parent)
        self._config = config
        self._lock = threading.RLock()
        self._cache = _TranslationCache()
        self._breaker = CircuitBreaker()
        self._throttle = CallThrottle()
        self._retry = RetryPolicy()
        self._timeout = float(self.default_timeout_s)
        self._source_lang = "auto"
        self._target_lang = "zh-CN"
        self._prompt = str(self.default_prompt or "")
        self._prompt_enabled = bool(self.default_prompt) and bool(
            getattr(self, "supports_prompt", False))
        self._max_chars = int(self.max_text_chars)
        self._min_chars = int(self.min_request_chars)
        self._task_timeout_ms = float(DEFAULT_TASK_TIMEOUT_MS)
        self._context_max_chars = int(DEFAULT_CONTEXT_MAX_CHARS)
        # One per-call context slot per thread: two lanes (or a retry) can never
        # read each other's sentence context.
        self._call_local = threading.local()
        self._closed = False
        self._counter = 0
        self._stats: Dict[str, Any] = {
            "calls": 0, "successes": 0, "failures": 0, "passthrough": 0,
            "cancelled": 0, "timeouts": 0, "circuit_rejects": 0,
            "total_ms": 0.0, "last_ms": 0.0, "attempts": 0,
        }
        if not _QT_AVAILABLE:                          # per-instance fallbacks
            self.translated = _FallbackSignal()
            self.failed = _FallbackSignal()
            self.status_changed = _FallbackSignal()
            self.circuit_changed = _FallbackSignal()
        # A subclass may override apply_config() and rely on its own attributes,
        # so every subclass must build them *before* calling super().__init__().
        self.apply_config()

    # -- configuration ----------------------------------------------------- #

    def _get(self, dotted_key: str, default: Any = None) -> Any:
        """Read a dotted config key from a ConfigManager, dict or nothing."""
        config = self._config
        if config is None:
            return default
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                value = getter(dotted_key, default)
            except Exception:                              # noqa: BLE001
                return default
            return default if value is None else value
        if isinstance(config, dict):
            current: Any = config
            for part in str(dotted_key).split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return default if current is None else current
        return default

    def _get_float(self, key: str, default: float) -> float:
        try:
            return float(self._get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return int(default)

    def apply_config(self) -> None:
        """Re-read every value this layer owns; subclasses extend this."""
        self._timeout = max(0.2, self._get_float("translate.timeout_seconds",
                                                 self.default_timeout_s))
        # Whole-call ceiling: min(configured request timeout, task deadline).
        self._task_timeout_ms = max(
            50.0, self._get_float("translate.task_timeout_ms",
                                  float(DEFAULT_TASK_TIMEOUT_MS)))
        self._context_max_chars = max(
            0, self._get_int("translate.context_max_chars",
                             int(DEFAULT_CONTEXT_MAX_CHARS)))
        threshold = max(1, self._get_int("translate.failure_threshold",
                                         DEFAULT_FAILURE_THRESHOLD))
        break_s = self._get_float("translate.circuit_break_seconds",
                                  DEFAULT_CIRCUIT_BREAK_S)
        self._breaker = CircuitBreaker(threshold, break_s)
        self._breaker.failure_threshold = threshold
        self._retry = RetryPolicy.from_config(self._config)
        self._throttle.configure(
            self._get_float("translate.min_interval_ms", 0.0) / 1000.0)
        self._cache.configure(self._get_float("translate.cache_ttl_seconds",
                                             DEFAULT_CACHE_TTL_S),
                              self._get_int("translate.cache_max_entries",
                                            DEFAULT_CACHE_MAX_ENTRIES))
        self._source_lang = normalize_language(
            self._get("translate.source_lang", "auto"))
        self._target_lang = normalize_language(
            self._get("translate.target_lang", "zh-CN"))
        # Subtitle length is capped upstream too; keep the same ceiling here.
        self._max_chars = max(16, self._get_int("asr.max_subtitle_chars",
                                               int(self.max_text_chars)))
        self._min_chars = max(1, self._get_int("translate.min_request_chars",
                                              int(self.min_request_chars)))
        # Prompt support is opt-in per backend: an endpoint that cannot carry a
        # style instruction must not advertise one in the UI.
        configured = self._get("translate.prompt", None)
        if configured is None:
            self._prompt = str(self.default_prompt or "")
        else:
            self._prompt = str(configured or "").strip() or str(
                self.default_prompt or "")
        if getattr(self, "supports_prompt", False):
            self._prompt_enabled = bool(
                self._get("translate.prompt_enabled", True)) and bool(self._prompt)
        else:
            self._prompt_enabled = False

    # -- properties -------------------------------------------------------- #

    @property
    def timeout_s(self) -> float:
        return float(self._timeout)

    @property
    def task_timeout_ms(self) -> float:
        """Hard ceiling (ms) for one translate() call, incl. retries."""
        return float(self._task_timeout_ms)

    @property
    def context_max_chars(self) -> int:
        """Upper bound on the rolling sentence context."""
        return int(self._context_max_chars)

    def call_context(self) -> str:
        """
        Sentence context of the call running on this thread ("" when none).

        Prompt-capable backends read this while building their request. The
        keyless endpoints ignore it on purpose: their API has no instruction
        channel, and inventing one would silently corrupt the query.
        """
        try:
            return str(getattr(self._call_local, "context", "") or "")
        except Exception:                                  # noqa: BLE001
            return ""

    def _sanitize_context(self, context: Any) -> str:
        if not context:
            return ""
        if self._context_max_chars <= 0:
            return ""
        value = normalize_text(str(context))
        if not value:
            return ""
        if len(value) > self._context_max_chars:
            # Keep the tail: the newest sentence is the most relevant one.
            value = value[-self._context_max_chars:].strip()
        return value

    @timeout_s.setter
    def timeout_s(self, value: float) -> None:
        try:
            self._timeout = max(0.2, float(value))
        except (TypeError, ValueError):
            pass

    @property
    def source_lang(self) -> str:
        return self._source_lang

    @property
    def target_lang(self) -> str:
        return self._target_lang

    @property
    def supports_style_prompt(self) -> bool:
        """True when the backend can actually carry a style pre-prompt."""
        return bool(getattr(self, "supports_prompt", False))

    @property
    def style_prompt(self) -> str:
        """Current style pre-prompt, or "" for a backend without prompt support."""
        if not self.supports_style_prompt:
            return ""
        return self._prompt if self._prompt_enabled else ""

    @style_prompt.setter
    def style_prompt(self, value: Any) -> None:
        self._prompt = str(value or "").strip() or str(self.default_prompt or "")
        self._prompt_enabled = bool(self._prompt) and self.supports_style_prompt

    @property
    def prompt_enabled(self) -> bool:
        return bool(self._prompt_enabled) and bool(self._prompt)

    @property
    def circuit(self) -> CircuitBreaker:
        return self._breaker

    @property
    def is_available(self) -> bool:
        return not self._closed and self.check_available()[0]

    @property
    def is_closed(self) -> bool:
        return self._closed

    def cache_stats(self) -> Dict[str, Any]:
        return self._cache.stats()

    # -- subclass surface -------------------------------------------------- #

    @abc.abstractmethod
    def _translate(self, text: str, source_lang: str, target_lang: str,
                   timeout_s: float) -> str:
        """
        One attempt. Return the translated text (non-empty) or raise one of the
        Translation*Error classes. ``timeout_s`` is this attempt's slice of the
        total budget, already reduced by the time spent on earlier attempts.
        """

    @classmethod
    def check_available(cls) -> Tuple[bool, str]:
        """Cheap, side-effect free availability probe (no network, no I/O)."""
        return True, ""

    def _on_close(self) -> None:
        """Hook for subclasses that hold a session, socket or thread pool."""

    # -- public API -------------------------------------------------------- #

    def translate(self, text: str, source_lang: Optional[str] = None,
                  target_lang: Optional[str] = None,
                  token: Any = None,
                  context: Any = None) -> TranslationResult:
        """
        Translate one subtitle line. Never raises and never returns None: the
        failure is carried in the result so a pipeline lane cannot die here.

        ``context`` is the optional rolling window of recent sentences; only a
        backend with ``supports_prompt`` actually sends it (v0.1.18 item 4).
        """
        started = time.monotonic()
        original = normalize_text(text)
        source = normalize_language(source_lang or self._source_lang)
        target = normalize_language(target_lang or self._target_lang)
        try:
            self._call_local.context = self._sanitize_context(context)
        except Exception:                                  # noqa: BLE001
            pass

        with self._lock:
            self._counter += 1
            self._stats["calls"] += 1

        if self._closed:
            return self._finish_failure(original, source, target, "translator closed",
                                        STATUS_FAILED, started)

        if not original:
            return self._finish_failure(original, source, target, "empty text",
                                        STATUS_FAILED, started)

        if len(original) < self._min_chars:
            result = TranslationResult.passthrough(
                original, source, target, self.name,
                (time.monotonic() - started) * 1000.0)
            with self._lock:
                self._stats["passthrough"] += 1
            self._safe_emit(self.translated, result)
            return result

        if is_same_language(source, target):
            # Nothing to do: the original text *is* the subtitle.
            result = TranslationResult.passthrough(
                original, source, target, self.name,
                (time.monotonic() - started) * 1000.0)
            with self._lock:
                self._stats["passthrough"] += 1
            self._safe_emit(self.translated, result)
            return result

        cache_key = (original, source, target)
        cached = self._cache.get(cache_key)
        if cached:
            result = TranslationResult(
                text=original, translated=cached, source_lang=source,
                target_lang=target, provider=self.name, status=STATUS_OK,
                from_cache=True,
                elapsed_ms=(time.monotonic() - started) * 1000.0)
            with self._lock:
                self._stats["successes"] += 1
            self._record_timing(result.elapsed_ms)
            self._safe_emit(self.translated, result)
            return result

        if not self._breaker.allow():
            wait = self._breaker.retry_after()
            message = f"circuit open, retry in {wait:.1f}s"
            with self._lock:
                self._stats["circuit_rejects"] += 1
            self._safe_emit(self.circuit_changed, CIRCUIT_OPEN, wait)
            self._safe_emit(self.status_changed, STATUS_CIRCUIT_OPEN, message)
            return self._finish_failure(original, source, target, message,
                                        STATUS_CIRCUIT_OPEN, started)

        chunks = self._split_chunks(original)
        deadline = started + max(1.0, self._retry.total_budget_seconds)
        # A long total budget is pointless for live subtitles: the task deadline
        # (default 700 ms) is the real ceiling, so retries cannot stretch a line
        # far past the speech it belongs to.
        task_budget = self._task_timeout_ms / 1000.0
        if task_budget > 0.0:
            deadline = min(deadline, started + max(0.15, task_budget))
        parts: List[str] = []
        attempts = 0
        last_error = ""

        for chunk in chunks:
            # The attempt count is only known once the call returns, so a failing
            # attempt must not leave `used` unbound for the handlers below.
            used = 0
            try:
                translated_chunk, used = self._translate_with_retry(
                    chunk, source, target, token, deadline)
            except TranslationCancelledError as exc:
                with self._lock:
                    self._stats["cancelled"] += 1
                self._safe_emit(self.status_changed, STATUS_CANCELLED, str(exc))
                return self._finish_failure(original, source, target, str(exc),
                                            STATUS_CANCELLED, started, attempts)
            except TranslationTimeoutError as exc:
                with self._lock:
                    self._stats["timeouts"] += 1
                last_error, attempts = f"timeout: {exc}", attempts + used
                break
            except TranslationError as exc:
                last_error, attempts = str(exc), attempts + used
                break
            except Exception as exc:                          # noqa: BLE001
                logger.exception("translator backend raised unexpectedly")
                last_error = f"{exc.__class__.__name__}: {exc}"
                attempts += used
                break
            attempts += used
            parts.append(translated_chunk)

        if last_error or len(parts) != len(chunks):
            self._breaker.record_failure()
            if self._breaker.state == CIRCUIT_OPEN:
                self._safe_emit(self.circuit_changed, CIRCUIT_OPEN,
                                self._breaker.retry_after())
            return self._finish_failure(original, source, target,
                                        last_error or "incomplete translation",
                                        STATUS_FAILED, started, attempts)

        joined = " ".join(part for part in parts if part).strip() or original
        self._breaker.record_success()
        self._cache.put(cache_key, joined)

        elapsed_ms = (time.monotonic() - started) * 1000.0
        result = TranslationResult(
            text=original, translated=joined, source_lang=source,
            target_lang=target, provider=self.name, status=STATUS_OK,
            attempts=attempts, elapsed_ms=elapsed_ms)
        with self._lock:
            self._stats["successes"] += 1
            self._stats["attempts"] += attempts
        self._record_timing(elapsed_ms)
        self._safe_emit(self.translated, result)
        return result

    def translate_batch(self, texts: Any, source_lang: Optional[str] = None,
                        target_lang: Optional[str] = None,
                        token: Any = None,
                        context: Any = None) -> List[TranslationResult]:
        """Translate several lines in order; failures stay inside their result."""
        results: List[TranslationResult] = []
        for item in list(texts or []):
            if token is not None and getattr(token, "is_cancelled", False):
                break
            results.append(self.translate(item, source_lang, target_lang, token,
                                          context=context))
        return results

    def clear_cache(self) -> int:
        return self._cache.clear()

    def reset_circuit(self) -> None:
        self._breaker.reset()
        self._safe_emit(self.circuit_changed, CIRCUIT_CLOSED, 0.0)

    def close(self) -> None:
        """Release backend resources once; safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        try:
            self._on_close()
        except Exception:                                  # noqa: BLE001
            logger.exception("translator teardown failed")
        # Shutdown must not leave recognised/translated lines in memory: the cache
        # holds user text, so it is emptied here rather than at process exit.
        try:
            self._cache.clear()
        except Exception:                                  # noqa: BLE001
            logger.debug("translator cache clear on close failed", exc_info=True)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._stats)
            calls = max(1, int(data.get("calls", 0)))
        data["name"] = self.name
        data["display_name"] = self.display_name
        data["timeout_s"] = round(self.timeout_s, 3)
        data["task_timeout_ms"] = round(self.task_timeout_ms, 1)
        data["context_max_chars"] = self.context_max_chars
        data["source_lang"] = self._source_lang
        data["target_lang"] = self._target_lang
        data["prompt_supported"] = self.supports_style_prompt
        data["prompt_enabled"] = self.prompt_enabled
        data["prompt_chars"] = len(self._prompt)
        data["avg_ms"] = round(float(data.get("total_ms", 0.0)) / calls, 2)
        data["cache"] = self._cache.stats()
        data["circuit"] = self._breaker.stats()
        data["throttle"] = self._throttle.stats()
        data["closed"] = self._closed
        return data

    # -- internals --------------------------------------------------------- #

    def _translate_with_retry(self, chunk: str, source: str, target: str,
                              token: Any,
                              deadline: float) -> Tuple[str, int]:
        """Run the bounded attempt loop for one chunk. Returns (text, attempts)."""
        last_error = ""
        attempts = 0
        while attempts < self._retry.max_attempts:
            if token is not None and getattr(token, "is_cancelled", False):
                raise TranslationCancelledError("translation cancelled")
            now = time.monotonic()
            if now >= deadline:
                last_error = last_error or "total time budget exhausted"
                break
            attempts += 1
            slice_s = max(0.15, min(self._timeout, deadline - now))
            self._throttle.wait(token)
            try:
                value = self._translate(chunk, source, target, slice_s)
            except TranslationCancelledError:
                raise
            except TranslationTimeoutError as exc:
                last_error = f"timeout: {exc}"
            except TranslationUnavailableError as exc:
                last_error = f"unavailable: {exc}"
            except TranslationResponseError as exc:
                last_error = f"bad response: {exc}"
            except Exception as exc:                          # noqa: BLE001
                logger.exception("translator backend raised unexpectedly")
                last_error = f"{exc.__class__.__name__}: {exc}"
            else:
                cleaned = normalize_text(value)
                if cleaned:
                    return cleaned, attempts
                last_error = "empty translation"
            if attempts < self._retry.max_attempts:
                _cancellable_sleep(self._retry.delay_for(attempts), token)
        if last_error.startswith("timeout"):
            raise TranslationTimeoutError(last_error)
        raise TranslationUnavailableError(last_error or "translation failed")

    def _split_chunks(self, text: str) -> List[str]:
        """Split an over-long line on sentence boundaries, then hard-split."""
        limit = max(16, int(self._max_chars))
        if len(text) <= limit:
            return [text]
        sentence_re = re.compile(r"(?<=[.!?。！？；;])\s*")
        pieces = [piece for piece in sentence_re.split(text) if piece.strip()]
        if not pieces:
            pieces = [text]
        chunks: List[str] = []
        current = ""
        for piece in pieces:
            while len(piece) > limit:                      # no natural boundary
                chunks.append(piece[:limit])
                piece = piece[limit:]
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= limit:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
        if current:
            chunks.append(current)
        return [chunk.strip() for chunk in chunks if chunk.strip()] or [text]

    def _finish_failure(self, original: str, source: str, target: str,
                        error: str, status: str, started: float,
                        attempts: int = 0) -> TranslationResult:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        result = TranslationResult.failure(
            original, error, source, target, self.name, status=status,
            attempts=attempts, elapsed_ms=elapsed_ms)
        with self._lock:
            self._stats["failures"] += 1
            self._stats["last_ms"] = elapsed_ms
            self._stats["total_ms"] += elapsed_ms
        if status not in (STATUS_CIRCUIT_OPEN,):
            self._safe_emit(self.status_changed, status, error)
        self._safe_emit(self.failed, result)
        return result

    def _record_timing(self, elapsed_ms: float) -> None:
        with self._lock:
            self._stats["last_ms"] = elapsed_ms
            self._stats["total_ms"] += elapsed_ms

    @staticmethod
    def _safe_emit(signal: Any, *args: Any) -> None:
        """Emitting must never break a worker lane."""
        try:
            signal.emit(*args)
        except Exception:                                  # noqa: BLE001
            logger.debug("could not emit translator signal", exc_info=True)

    # -- context manager --------------------------------------------------- #

    def __enter__(self) -> "BaseTranslator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False
