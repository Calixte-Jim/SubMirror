# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Text cleaning, filtering and normalisation (official entry 13).

Where it sits
    ASR transcript / OCR text  ->  TextProcessor  ->  translation request
                                    (this module)

What it guarantees
    * a single normalisation path: NFKC, invisible/control/private-use/bidi
      character removal, whitespace collapsing, then truncation to <= 200 chars
      using the SAME helper as the subtitle queue
    * noise rejection with an explicit reason: empty, too short, punctuation
      only, no words, single-character OCR noise, duplicate hallucination
    * burst protection that cannot swallow real subtitles: the cooldown only
      defers SHORT fragments, while a longer line always passes and clears it
    * slang replacement is a declared v0.1 PLACEHOLDER: the dictionary is read
      and counted, but apply() never rewrites text (full logic is v0.2)

Threading
    Pure Python, no Qt. Every public method is protected by an internal lock, so
    the scheduler lanes may call it from their worker threads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Reuse the subtitle queue's truncation so both layers share one definition of
# "collapsed whitespace + ellipsis at the cap".
try:
    from src.core.subtitle_queue import ELLIPSIS, MAX_SUBTITLE_CHARS, truncate_text
except Exception:  # pragma: no cover - stand-alone fallback
    MAX_SUBTITLE_CHARS = 200
    ELLIPSIS = "\u2026"

    def truncate_text(text: str, limit: int = MAX_SUBTITLE_CHARS,
                      ellipsis: str = ELLIPSIS) -> str:
        collapsed = " ".join(str(text or "").split())
        if limit <= 0 or len(collapsed) <= limit:
            return collapsed
        if limit <= len(ellipsis):
            return collapsed[:limit]
        return collapsed[:limit - len(ellipsis)].rstrip() + ellipsis

try:
    from src.core.context import asset_path as _asset_path
except Exception:  # pragma: no cover - stand-alone fallback
    def _asset_path(name: str) -> str:
        import os

        return os.path.join("assets", name)

__all__ = [
    "SOURCE_ASR",
    "SOURCE_OCR",
    "MIN_TEXT_CHARS",
    "MAX_TEXT_CHARS",
    "TextProcessorConfig",
    "TextDecision",
    "TextNormalizer",
    "InvalidBurstLimiter",
    "SlangProcessor",
    "TextProcessor",
    "ASR_HALLUCINATIONS",
    "split_sentences",
    "script_profile",
    "matches_language",
]

logger = logging.getLogger("submirror.ai.text_processor")

SOURCE_ASR = "asr"
SOURCE_OCR = "ocr"

MIN_TEXT_CHARS = 3                 # spec: shorter ASR text is never translated
MAX_TEXT_CHARS = MAX_SUBTITLE_CHARS
INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u061c"

# Reasons a candidate text may be rejected.
REASON_EMPTY = "empty"
REASON_TOO_SHORT = "too-short"
REASON_PUNCT_ONLY = "punctuation-only"
REASON_NO_WORDS = "no-words"
REASON_SINGLE_NOISE = "single-char-noise"
REASON_DUPLICATE = "duplicate"
REASON_RATE_LIMITED = "rate-limited"
REASON_SAME_LANGUAGE = "same-language"
REASON_ASR_NOISE = "asr-noise"
REASON_FILLER_ONLY = "filler-only"

# End-of-sentence marks used by :func:`split_sentences`. Only these hard breaks
# are honoured; everything else stays inside one sentence.
_SENTENCE_END_RE = re.compile(r"[.!?。！？]+(?=\s|$)")
_SENTENCE_WS_RE = re.compile(r"\s+")


def split_sentences(text: Any, max_chars: int = 200) -> List[str]:
    """
    Cut recognised text into short sentences (v0.1.18 item 2).

    The text is split on ``.`` ``!`` ``?`` (and their full-width twins), and a
    remaining fragment longer than *max_chars* is broken at the last space so a
    single translation request never carries a paragraph.
    """
    raw = _SENTENCE_WS_RE.sub(" ", str(text or "")).strip()
    if not raw:
        return []

    pieces: List[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(raw):
        piece = raw[start:match.end()].strip()
        if piece:
            pieces.append(piece)
        start = match.end()
    tail = raw[start:].strip()
    if tail:
        pieces.append(tail)

    limit = max(20, int(max_chars or 200))
    out: List[str] = []
    for piece in pieces:
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                cut = piece.rfind(",", 0, limit + 1)
            if cut < limit // 2:
                cut = limit
            head = piece[:cut].strip(" ,")
            if head:
                out.append(head)
            piece = piece[cut:].strip(" ,")
        if piece:
            out.append(piece)
    return out

# Whisper's "no speech" hallucinations: it emits one of these on silence or on
# pure background noise. They carry no game information, so the ASR lane drops
# them before translation and before they can reach the overlay.
ASR_HALLUCINATIONS = frozenset({
    "thank you", "thank you.", "thanks", "thanks for watching",
    "thank you for watching", "thanks for watching!", "thank you.",
    "please subscribe", "like and subscribe", "subscribe to my channel",
    "subtitles by", "subtitled by", "transcription by", "translated by",
    "captions by", "amara.org", "www.amara.org", "www.veed.io",
})

# Non-speech tags some engines wrap around music/applause instead of words.
_ASR_TAG_RE = re.compile(
    r"^[\[\(（【]\s*(music|música|musica|applause|laughter|silence|"
    r"inaudible|background noise|sound effect|sound effects|no speech)"
    r"\s*[\]\)）】]$", re.IGNORECASE)

_PUNCT_CATEGORIES = ("Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po",
                     "Sm", "Sc", "Sk", "So")
_MULTISPACE_RE = re.compile(r"[ \t\u00a0\u3000]{2,}")

_CJK_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)
_KANA_RANGES = ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF))
_HANGUL_RANGES = ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF))
_CYRILLIC_RANGES = ((0x0400, 0x04FF), (0x0500, 0x052F))


def _in_ranges(code: int, ranges: Tuple[Tuple[int, int], ...]) -> bool:
    for low, high in ranges:
        if low <= code <= high:
            return True
    return False


def script_of(char: str) -> str:
    """Coarse script bucket for one character (used for filtering only)."""
    if not char:
        return "other"
    code = ord(char)
    if char.isdigit():
        return "digit"
    if _in_ranges(code, _CJK_RANGES):
        return "cjk"
    if _in_ranges(code, _KANA_RANGES):
        return "kana"
    if _in_ranges(code, _HANGUL_RANGES):
        return "hangul"
    if _in_ranges(code, _CYRILLIC_RANGES):
        return "cyrillic"
    if unicodedata.category(char).startswith("P"):
        return "punct"
    if char.isalpha():
        return "latin"
    if unicodedata.category(char).startswith("S"):
        return "symbol"
    if char.isspace():
        return "space"
    return "other"


def script_profile(text: str) -> Dict[str, int]:
    """Character counts per script bucket; useful for logging and heuristics."""
    profile: Dict[str, int] = {}
    for char in str(text or ""):
        key = script_of(char)
        profile[key] = profile.get(key, 0) + 1
    return profile


def matches_language(text: str, language: str) -> bool:
    """
    Very coarse "is this text already in the given language" check.

    Deliberately conservative: it only recognises a few scripts, and ``auto``
    always answers True (meaning "cannot tell, translate it").
    """
    code = str(language or "auto").strip().lower()
    if code in ("", "auto"):
        return True
    profile = script_profile(text)
    letters = sum(profile.get(key, 0) for key in
                  ("cjk", "kana", "hangul", "latin", "cyrillic"))
    if letters <= 0:
        return False
    def ratio(keys) -> float:
        return sum(profile.get(key, 0) for key in keys) / float(letters)

    if code.startswith("zh"):
        return ratio(("cjk",)) >= 0.5
    if code.startswith("ja"):
        return (ratio(("kana", "cjk"))) >= 0.5
    if code.startswith("ko"):
        return (ratio(("hangul", "cjk"))) >= 0.5
    if code.startswith("ru"):
        return ratio(("cyrillic",)) >= 0.5
    if code.startswith("en"):
        return ratio(("latin",)) >= 0.5
    return True


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class TextProcessorConfig:
    """Runtime knobs; every value is clamped in ``from_config``."""

    asr_min_chars: int = MIN_TEXT_CHARS
    ocr_min_chars: int = 2
    max_chars: int = MAX_TEXT_CHARS
    collapse_whitespace: bool = True
    unicode_form: str = "NFKC"
    keep_newlines: bool = False
    remove_control_chars: bool = True
    ocr_allow_single_cjk: bool = True
    duplicate_window_s: float = 3.0
    invalid_window_s: float = 10.0
    invalid_limit: int = 12
    cooldown_s: float = 5.0
    cooldown_bypass_chars: int = 6
    slang_path: str = "assets/slang_dict.json"
    slang_enabled: bool = True
    # ASR-only: junk that Whisper emits on noise/silence (whistles, "♪♪",
    # repeated symbols, "thanks for watching") never reaches the translator.
    asr_noise_filter: bool = True
    # ASR-only: filler sounds ("um", "ah", ...) carry no meaning and are
    # dropped before the text is split into sentences (v0.1.18).
    filler_filter: bool = True
    filler_words: str = "um,uh,ah,er,erm,hmm,mm,mmm,uhm,ahh,eh,ha"
    # ASR-only: recognised text is cut on . ! ? so one subtitle is one short
    # sentence; no fragment longer than this is ever sent as a single request.
    sentence_split: bool = True
    max_sentence_chars: int = 200
    filter_same_language: bool = False

    @classmethod
    def from_config(cls, config: Any = None) -> "TextProcessorConfig":
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

        data.asr_min_chars = as_int("text.asr_min_chars", MIN_TEXT_CHARS, 1, 32)
        data.ocr_min_chars = as_int("text.ocr_min_chars", 2, 1, 32)
        # The subtitle cap is owned by asr.max_subtitle_chars (spec: <= 200).
        data.max_chars = as_int("asr.max_subtitle_chars", MAX_TEXT_CHARS, 16,
                                MAX_TEXT_CHARS)
        data.collapse_whitespace = bool(read("text.collapse_whitespace", True))
        data.keep_newlines = bool(read("text.keep_newlines", False))
        data.remove_control_chars = bool(read("text.remove_control_chars", True))
        data.ocr_allow_single_cjk = bool(read("text.ocr_allow_single_cjk", True))
        data.duplicate_window_s = as_float("text.duplicate_window_s", 3.0, 0.0, 60.0)
        data.invalid_window_s = as_float("text.invalid_window_s", 10.0, 1.0, 120.0)
        data.invalid_limit = as_int("text.invalid_limit", 12, 1, 1000)
        data.cooldown_s = as_float("text.cooldown_s", 5.0, 0.0, 120.0)
        data.cooldown_bypass_chars = as_int("text.cooldown_bypass_chars", 6, 1, 64)
        data.slang_path = str(read("text.slang_path", "assets/slang_dict.json"))
        data.slang_enabled = bool(read("text.slang_enabled", True))
        data.asr_noise_filter = bool(read("text.asr_noise_filter", True))
        data.filler_filter = bool(read("text.filler_filter", True))
        data.filler_words = str(read("text.filler_words",
                                     "um,uh,ah,er,erm,hmm,mm,mmm,uhm,ahh,eh,ha"))
        data.sentence_split = bool(read("asr.sentence_split", True))
        data.max_sentence_chars = as_int("text.max_sentence_chars", 200, 20, 500)
        data.filter_same_language = bool(read("text.filter_same_language", False))
        return data

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- #
# Decision object
# --------------------------------------------------------------------------- #

@dataclass
class TextDecision:
    """Outcome of one ``TextProcessor.process()`` call."""

    raw: str = ""
    text: str = ""
    accepted: bool = False
    reason: str = ""
    source: str = SOURCE_ASR
    truncated: bool = False
    removed_chars: int = 0
    scripts: Dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def is_noise(self) -> bool:
        return not self.accepted

    def display_text(self) -> str:
        """Text to hand to the translator / subtitle queue ("" when rejected)."""
        return self.text if self.accepted else ""

    def as_dict(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason,
                "source": self.source, "chars": len(self.text),
                "truncated": self.truncated, "removed_chars": self.removed_chars,
                "scripts": dict(self.scripts),
                "elapsed_ms": round(self.elapsed_ms, 2)}


# --------------------------------------------------------------------------- #
# Normaliser
# --------------------------------------------------------------------------- #

class TextNormalizer:
    """Unicode clean-up; no filtering decisions, no translation knowledge."""

    def __init__(self, config: Optional[TextProcessorConfig] = None) -> None:
        self.config = config or TextProcessorConfig()

    def apply_config(self, config: TextProcessorConfig) -> None:
        self.config = config

    def normalize(self, text: Any) -> str:
        if text is None:
            return ""
        value = str(text)
        config = self.config
        if config.unicode_form:
            try:
                value = unicodedata.normalize(config.unicode_form, value)
            except Exception:
                logger.debug("unicode normalisation failed", exc_info=True)

        kept: List[str] = []
        for char in value:
            if char in INVISIBLE_CHARS:
                continue
            category = unicodedata.category(char)
            if category in ("Cc", "Cf", "Co", "Cs"):
                if char in "\r\n\t":
                    if config.keep_newlines:
                        kept.append("\n" if char in "\r\n" else " ")
                    else:
                        kept.append(" ")
                    continue
                if not config.remove_control_chars:
                    kept.append(char)
                continue
            kept.append(char)
        value = "".join(kept)

        if config.collapse_whitespace:
            if config.keep_newlines:
                lines = [" ".join(line.split()) for line in value.splitlines()]
                value = "\n".join(line for line in lines if line)
            else:
                value = _MULTISPACE_RE.sub(" ", value)
                value = " ".join(value.split())
        return value.strip()

    @staticmethod
    def visible_length(text: str) -> int:
        return len(str(text or "").strip())

    @staticmethod
    def is_punctuation_only(text: str) -> bool:
        """True when there is at least one char and none of them is a letter/digit."""
        stripped = str(text or "").strip()
        if not stripped:
            return False
        for char in stripped:
            if char.isalnum():
                return False
            if char.isspace():
                continue
            if unicodedata.category(char) not in _PUNCT_CATEGORIES:
                return False
        return True

    @staticmethod
    def has_word(text: str) -> bool:
        return any(char.isalnum() for char in str(text or ""))


# --------------------------------------------------------------------------- #
# Burst protection
# --------------------------------------------------------------------------- #

class InvalidBurstLimiter:
    """
    Counts invalid results and opens a short cooldown when they flood in.

    The cooldown deliberately does NOT block long text: it exists to shed the
    short fragments that dominate a noise burst, while a real subtitle sentence
    always passes through and immediately clears the cooldown.
    """

    def __init__(self, window_s: float = 10.0, limit: int = 12,
                 cooldown_s: float = 5.0, bypass_chars: int = 6) -> None:
        self._lock = threading.Lock()
        self._window_s = max(1.0, float(window_s))
        self._limit = max(1, int(limit))
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._bypass_chars = max(1, int(bypass_chars))
        self._invalid_times: List[float] = []
        self._cooldown_until = 0.0
        self._cooldowns = 0

    def configure(self, window_s: float, limit: int, cooldown_s: float,
                  bypass_chars: int) -> None:
        with self._lock:
            self._window_s = max(1.0, float(window_s))
            self._limit = max(1, int(limit))
            self._cooldown_s = max(0.0, float(cooldown_s))
            self._bypass_chars = max(1, int(bypass_chars))
            self._invalid_times.clear()

    @property
    def bypass_chars(self) -> int:
        return self._bypass_chars

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        if self._invalid_times and self._invalid_times[0] < cutoff:
            self._invalid_times = [t for t in self._invalid_times if t >= cutoff]

    def note_invalid(self) -> bool:
        """Record one invalid result; True when this engaged the cooldown."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._invalid_times.append(now)
            if len(self._invalid_times) >= self._limit and \
                    self._cooldown_s > 0 and now >= self._cooldown_until:
                self._cooldown_until = now + self._cooldown_s
                self._invalid_times.clear()
                self._cooldowns += 1
                logger.info("text filter cooldown engaged for %.1fs after %d "
                            "invalid results", self._cooldown_s, self._limit)
                return True
        return False

    def note_valid(self) -> None:
        """A usable line was seen: clear the burst window (and any cooldown)."""
        with self._lock:
            self._invalid_times.clear()
            self._cooldown_until = 0.0

    def allow(self, length: int = 0) -> Tuple[bool, float]:
        """
        Return (allowed, retry_after_seconds).

        Inside a cooldown only text shorter than ``bypass_chars`` is deferred.
        """
        now = time.monotonic()
        with self._lock:
            if now >= self._cooldown_until:
                return True, 0.0
            if int(length) >= self._bypass_chars:
                self._cooldown_until = 0.0
                return True, 0.0
            return False, max(0.0, self._cooldown_until - now)

    def reset(self) -> None:
        with self._lock:
            self._invalid_times.clear()
            self._cooldown_until = 0.0

    def stats(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {"window_s": self._window_s, "limit": self._limit,
                    "cooldown_s": self._cooldown_s,
                    "bypass_chars": self._bypass_chars,
                    "invalid_in_window": len(self._invalid_times),
                    "cooling_down": now < self._cooldown_until,
                    "retry_after": round(max(0.0, self._cooldown_until - now), 2),
                    "cooldowns": self._cooldowns}


# --------------------------------------------------------------------------- #
# Shorthand / slang dictionary (0.1.17: replacement is ACTIVE)
# --------------------------------------------------------------------------- #

class SlangProcessor:
    """
    Word-boundary substitution of FPS shorthand before translation.

    The dictionary maps an English shorthand ("gg") to the Chinese meaning the
    translator should work from ("打得好"), so a keyless endpoint no longer has
    to guess at it. Rules are compiled longest-key-first and matched case
    insensitively, and a key only matches when it stands alone - ``u`` never
    eats the ``u`` inside ``you``.

    ``load()`` stays best effort: a missing or malformed file leaves the
    dictionary empty and ``apply()`` falls back to the identity function.
    """

    def __init__(self, config: Optional[TextProcessorConfig] = None) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, Any] = {}
        self._rules: List[Tuple[Any, str, int, str]] = []
        self._loaded = False
        self._path = ""
        self._loaded_path = ""
        self._enabled = True
        self._applied = 0
        self._replacements = 0
        self._hits: Dict[str, int] = {}
        if config is not None:
            self.configure(config)

    def configure(self, config: TextProcessorConfig) -> None:
        with self._lock:
            self._path = str(config.slang_path or "")
            self._enabled = bool(config.slang_enabled)

    def _compile(self) -> None:
        """Build the replacement rules, longest key first."""
        rules: List[Tuple[Any, str, int, str]] = []
        for key, value in self._entries.items():
            if not isinstance(key, str) or not key or key.startswith("_"):
                continue
            if not isinstance(value, str) or not value:
                continue
            try:
                pattern = re.compile(
                    r"(?<![0-9A-Za-z])" + re.escape(key) + r"(?![0-9A-Za-z])",
                    re.IGNORECASE)
            except re.error:  # pragma: no cover - re.escape cannot break this
                logger.debug("slang rule skipped (%r)", key)
                continue
            rules.append((pattern, value, len(key), key))
        rules.sort(key=lambda item: (-item[2], item[3]))
        self._rules = rules

    @staticmethod
    def resolve_path(target: str) -> str:
        """
        Absolute path of a configured dictionary location.

        Relative names ("assets/slang_dict.json") are resolved inside the app's
        assets directory rather than against the process working directory, so
        a shortcut with a foreign "start in" folder cannot break the lookup.
        """
        raw = str(target or "").strip()
        if not raw:
            return ""
        if os.path.isabs(raw):
            return os.path.normpath(raw)
        name = os.path.basename(raw.replace("\\", "/"))
        try:
            return _asset_path(name)
        except Exception:  # pragma: no cover - context fallback already plain
            return raw

    def load(self, path: Optional[str] = None) -> bool:
        """Best-effort load; a missing or empty file is NOT an error."""
        target = str(path or self._path or "assets/slang_dict.json")
        resolved = self.resolve_path(target)
        with self._lock:
            self._path = target
            self._entries = {}
            self._rules = []
            self._loaded = False
            self._loaded_path = ""
        try:
            with open(resolved, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            logger.info("slang dictionary not found (%s); shorthand disabled",
                        resolved)
            return False
        except Exception:
            logger.warning("slang dictionary could not be read (%s)", resolved,
                           exc_info=True)
            return False
        if not isinstance(data, dict) or not data:
            logger.info("slang dictionary is empty; shorthand disabled")
            return False
        with self._lock:
            self._entries = dict(data)
            self._compile()
            self._loaded = True
            self._loaded_path = target
            count = len(self._rules)
        logger.info("slang dictionary loaded: %d replacement rules", count)
        return True

    def matches(self, text: str) -> bool:
        """True when a rule would rewrite ``text`` (no stats side effects)."""
        source = str(text or "")
        if not source:
            return False
        with self._lock:
            enabled = self._enabled
            rules = list(self._rules)
        if not enabled or not rules:
            return False
        for pattern, _replacement, _length, _key in rules:
            try:
                if pattern.search(source):
                    return True
            except Exception:  # pragma: no cover - regex on hostile input
                continue
        return False

    def apply(self, text: str) -> str:
        """Replace known shorthand; the identity when disabled or empty."""
        source = str(text or "")
        if not source:
            return source
        with self._lock:
            enabled = self._enabled
            rules = list(self._rules)
        if not enabled or not rules:
            return source
        result = source
        hits: Dict[str, int] = {}
        total = 0
        for pattern, replacement, _length, key in rules:
            result, count = pattern.subn(replacement, result)
            if count:
                hits[key] = hits.get(key, 0) + int(count)
                total += int(count)
        if total:
            with self._lock:
                self._applied += 1
                self._replacements += total
                for key, count in hits.items():
                    self._hits[key] = self._hits.get(key, 0) + count
        return result

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def rule_count(self) -> int:
        with self._lock:
            return len(self._rules)

    @property
    def loaded_path(self) -> str:
        """Path of the dictionary currently in memory ("" when none)."""
        with self._lock:
            return self._loaded_path

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            top = sorted(self._hits.items(), key=lambda kv: (-kv[1], kv[0]))
            return {"path": self._path, "entries": len(self._entries),
                    "rules": len(self._rules), "loaded": self._loaded,
                    "enabled": bool(self._enabled),
                    "applied": self._applied,
                    "replacements": self._replacements,
                    "top_hits": dict(top[:5])}


# --------------------------------------------------------------------------- #
# Processor
# --------------------------------------------------------------------------- #

class TextProcessor:
    """
    Normalisation + filtering + truncation for one candidate text.

    ``process()`` is the only entry point the pipeline needs; the convenience
    wrappers ``process_asr()`` / ``process_ocr()`` just fix the source name.
    """

    def __init__(self, config: Any = None) -> None:
        self._lock = threading.RLock()
        self._config_obj = config
        self.config = TextProcessorConfig.from_config(config)
        self.normalizer = TextNormalizer(self.config)
        self.limiter = InvalidBurstLimiter(
            self.config.invalid_window_s, self.config.invalid_limit,
            self.config.cooldown_s, self.config.cooldown_bypass_chars)
        self.slang = SlangProcessor(self.config)
        self._last_text: Dict[str, Tuple[str, float]] = {}
        self._stats: Dict[str, Any] = {
            "processed": 0, "accepted": 0, "rejected": 0, "rejected_reasons": {},
            "truncated": 0, "cooldowns": 0, "removed_chars": 0,
            "filler_removed": 0, "sentences": 0,
        }
        # Compiled filler pattern cache: (configured word list, pattern).
        self._filler_cache: Tuple[str, Optional[re.Pattern]] = ("", None)
        if self.config.slang_enabled:
            # 0.1.17: the dictionary is applied to every accepted ASR line.
            try:
                self.slang.load(self.config.slang_path)
            except Exception:
                logger.debug("slang preload failed", exc_info=True)

    # -- configuration ----------------------------------------------------- #

    def apply_config(self, config: Any = None) -> None:
        with self._lock:
            if config is not None:
                self._config_obj = config
            self.config = TextProcessorConfig.from_config(self._config_obj)
            self.normalizer.apply_config(self.config)
            self.limiter.configure(self.config.invalid_window_s,
                                   self.config.invalid_limit,
                                   self.config.cooldown_s,
                                   self.config.cooldown_bypass_chars)
            self.slang.configure(self.config)
            self._filler_cache = ("", None)   # the word list may have changed
            if self.config.slang_enabled and \
                    self.slang.stats()["path"] != self.slang.loaded_path:
                # A newly enabled / repointed dictionary must take effect now.
                try:
                    self.slang.load(self.config.slang_path)
                except Exception:
                    logger.debug("slang reload failed", exc_info=True)

    def reset(self) -> None:
        """Clear duplicate memory and burst state (settings "reset" button)."""
        with self._lock:
            self._last_text.clear()
            self.limiter.reset()

    # -- helpers ----------------------------------------------------------- #

    def _min_chars(self, source: str) -> int:
        if source == SOURCE_OCR:
            return self.config.ocr_min_chars
        return self.config.asr_min_chars

    def _reject(self, raw: str, text: str, reason: str, source: str,
                removed: int, elapsed_ms: float,
                truncated: bool = False) -> TextDecision:
        self._stats["rejected"] += 1
        reasons = self._stats["rejected_reasons"]
        reasons[reason] = int(reasons.get(reason, 0)) + 1
        return TextDecision(raw=raw, text=text, accepted=False, reason=reason,
                            source=source, truncated=truncated,
                            removed_chars=removed, elapsed_ms=elapsed_ms)

    def _is_duplicate(self, text: str, source: str, now: float) -> bool:
        previous = self._last_text.get(source)
        if previous is None:
            return False
        last_text, last_at = previous
        if last_text != text:
            return False
        return (now - last_at) <= self.config.duplicate_window_s

    def _remember(self, text: str, source: str, now: float) -> None:
        self._last_text[source] = (text, now)

    def _is_ocr_single_noise(self, text: str) -> bool:
        stripped = text.strip()
        if len(stripped) != 1:
            return False
        char = stripped
        if not char.isalnum():
            return False
        if script_of(char) == "cjk":
            return not self.config.ocr_allow_single_cjk
        return True          # a lone latin/digit character is OCR noise

    def _is_asr_noise(self, text: str) -> bool:
        """
        ASR garbage that must never reach the translator or the overlay.

        Covers the four shapes Whisper actually produces on noise: a no-speech
        hallucination, a music/applause tag, a symbol-only burst ("♪♪♪", "///")
        and a stutter run ("aaaa", "yeah yeah yeah yeah"). Deliberately
        conservative: real short voice lines ("ok", "no", "nice") survive.
        """
        if not self.config.asr_noise_filter:
            return False
        stripped = text.strip()
        if not stripped:
            return True
        lowered = stripped.lower().strip(" .!?,;:-_~")
        if lowered in ASR_HALLUCINATIONS:
            return True
        if _ASR_TAG_RE.match(stripped):
            return True
        letters = sum(1 for char in stripped if char.isalnum())
        if letters == 0:
            return True
        # Marks dominate: "♪♪ oh ♪♪" and similar junk bursts.
        if len(stripped) >= 4 and letters / len(stripped) < 0.34:
            return True
        compact = "".join(char for char in lowered if char.isalnum() or
                          char in "'")
        if len(compact) >= 4 and len(set(compact)) == 1 and compact.isascii():
            return True
        tokens = [token for token in re.split(r"[^\w']+", lowered) if token]
        if len(tokens) >= 4 and len(set(tokens)) == 1 and tokens[0].isascii():
            return True
        return False

    # -- sentence shaping (v0.1.18) ---------------------------------------- #

    def _filler_pattern(self) -> Optional[re.Pattern]:
        """Compiled whole-word pattern of the configured filler sounds."""
        signature = str(self.config.filler_words or "")
        cached_signature, cached_pattern = self._filler_cache
        if cached_signature == signature:
            return cached_pattern
        words = [word.strip().lower()
                 for word in re.split(r"[,;\s]+", signature) if word.strip()]
        pattern: Optional[re.Pattern] = None
        if words:
            words.sort(key=len, reverse=True)
            body = "|".join(re.escape(word) for word in words)
            pattern = re.compile(r"(?<![0-9A-Za-z])(" + body + r")(?![0-9A-Za-z])",
                                 re.IGNORECASE)
        self._filler_cache = (signature, pattern)
        return pattern

    def strip_fillers(self, text: str) -> str:
        """
        Drop standalone filler sounds from recognised English.

        Whole-word only, so "ha" never eats "hard" and "mm" never eats "mmorpg".
        Returns the text unchanged when the filter is off or has no words.
        """
        if not self.config.filler_filter:
            return text
        pattern = self._filler_pattern()
        if pattern is None:
            return text
        cleaned = pattern.sub(" ", text)
        if cleaned == text:
            return text
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned.strip(" ,;:-")

    def sentences(self, text: Any) -> List[str]:
        """
        Short sentences of *text*: one translation request per sentence.

        With ``asr.sentence_split`` off the text is returned as a single piece,
        so an existing configuration keeps the old behaviour.
        """
        raw = "" if text is None else str(text)
        if not raw.strip():
            return []
        if not self.config.sentence_split:
            pieces = [raw.strip()]
        else:
            pieces = split_sentences(raw, self.config.max_sentence_chars)
        # The counter tracks how many subtitle lines / translation tasks the
        # sentence fan-out produced, so it is bumped by the split itself.
        self._stats["sentences"] += len(pieces)
        return pieces

    # -- main entry point -------------------------------------------------- #

    def process(self, text: Any, source: str = SOURCE_ASR,
                target_lang: Optional[str] = None) -> TextDecision:
        """
        Clean, filter and (if accepted) truncate one candidate text.

        Never raises: an internal failure yields a rejected decision instead.
        """
        started = time.monotonic()
        raw = "" if text is None else str(text)
        source = SOURCE_OCR if str(source).lower() == SOURCE_OCR else SOURCE_ASR
        try:
            with self._lock:
                normalized = self.normalizer.normalize(raw)
                removed = max(0, len(raw) - len(normalized))
                elapsed = lambda: (time.monotonic() - started) * 1000.0  # noqa: E731

                if not normalized:
                    self.limiter.note_invalid()
                    return self._reject(raw, "", REASON_EMPTY, source, removed,
                                        elapsed())

                # Filler sounds ("um", "ah") carry no meaning: drop them before
                # any length/noise test so "um um ah" is discarded as empty
                # rather than being translated (v0.1.18 item 2).
                if source == SOURCE_ASR and self.config.filler_filter:
                    stripped = self.strip_fillers(normalized)
                    if stripped != normalized:
                        if not stripped:
                            self._stats["filler_removed"] += 1
                            self.limiter.note_invalid()
                            return self._reject(raw, "", REASON_FILLER_ONLY,
                                                source, removed, elapsed())
                        normalized = stripped
                        self._stats["filler_removed"] += 1

                minimum = self._min_chars(source)
                if len(normalized) < minimum:
                    # "gg" / "wp" / "u" are shorter than the normal floor but
                    # they are exactly what the shorthand dictionary is for.
                    shorthand = (source == SOURCE_ASR
                                 and self.slang.matches(normalized))
                    if not shorthand and not (
                            source == SOURCE_OCR and self.config.ocr_allow_single_cjk
                            and len(normalized) == 1
                            and script_of(normalized) == "cjk"):
                        self.limiter.note_invalid()
                        return self._reject(raw, normalized, REASON_TOO_SHORT,
                                            source, removed, elapsed())

                if self.normalizer.is_punctuation_only(normalized):
                    self.limiter.note_invalid()
                    return self._reject(raw, normalized, REASON_PUNCT_ONLY,
                                        source, removed, elapsed())

                if not self.normalizer.has_word(normalized):
                    self.limiter.note_invalid()
                    return self._reject(raw, normalized, REASON_NO_WORDS,
                                        source, removed, elapsed())

                if source == SOURCE_OCR and self._is_ocr_single_noise(normalized):
                    self.limiter.note_invalid()
                    return self._reject(raw, normalized, REASON_SINGLE_NOISE,
                                        source, removed, elapsed())

                if source == SOURCE_ASR and self._is_asr_noise(normalized):
                    self.limiter.note_invalid()
                    return self._reject(raw, normalized, REASON_ASR_NOISE,
                                        source, removed, elapsed())

                now = time.monotonic()
                if self._is_duplicate(normalized, source, now):
                    self.limiter.note_invalid()
                    return self._reject(raw, normalized, REASON_DUPLICATE,
                                        source, removed, elapsed())

                if self.config.filter_same_language and target_lang and \
                        matches_language(normalized, target_lang):
                    self.limiter.note_invalid()
                    return self._reject(raw, normalized, REASON_SAME_LANGUAGE,
                                        source, removed, elapsed())

                allowed, retry_after = self.limiter.allow(len(normalized))
                if not allowed:
                    self._stats["cooldowns"] += 1
                    logger.debug("text deferred by burst cooldown (%.1fs left)",
                                 retry_after)
                    return self._reject(raw, normalized, REASON_RATE_LIMITED,
                                        source, removed, elapsed())

                truncated = len(normalized) > self.config.max_chars
                final = truncate_text(normalized, self.config.max_chars)
                final = self.slang.apply(final)          # FPS shorthand (0.1.17)

                self._remember(final, source, now)
                self.limiter.note_valid()

                self._stats["processed"] += 1
                self._stats["accepted"] += 1
                self._stats["removed_chars"] += removed
                if truncated:
                    self._stats["truncated"] += 1

                decision = TextDecision(
                    raw=raw, text=final, accepted=True, reason="", source=source,
                    truncated=truncated, removed_chars=removed,
                    scripts=script_profile(final), elapsed_ms=elapsed())
                return decision
        except Exception:
            # A filter bug must degrade, never kill the ASR/OCR lane.
            logger.exception("text processing failed; dropping candidate")
            self._stats["rejected"] += 1
            return TextDecision(raw=raw, text="", accepted=False,
                                reason="internal-error", source=source,
                                elapsed_ms=(time.monotonic() - started) * 1000.0)

    def process_asr(self, text: Any, target_lang: Optional[str] = None) -> TextDecision:
        return self.process(text, SOURCE_ASR, target_lang)

    def process_ocr(self, text: Any, target_lang: Optional[str] = None) -> TextDecision:
        return self.process(text, SOURCE_OCR, target_lang)

    # -- diagnostics ------------------------------------------------------- #

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._stats)
            data["rejected_reasons"] = dict(self._stats["rejected_reasons"])
            data["limiter"] = self.limiter.stats()
            data["slang"] = self.slang.stats()
            data["config"] = self.config.as_dict()
            return data
