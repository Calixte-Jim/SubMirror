# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Online translation backend (official entry, item 12).

Layering
    BaseTranslator        (src/ai/base_translator.py)  - validation, cache,
                                                         circuit breaker, retry
        OnlineTranslator  (this module)                - endpoint failover chain
            HttpClient                                 - one HTTP transport, two
                                                         back ends
            _Endpoint x3                               - request building and
                                                         payload parsing only

Why this shape
    * Zero hard new dependencies: the HTTP layer runs on the standard library.
      ``requests`` is used automatically when importable, purely for connection
      reuse; without it everything still works.
    * Three keyless public endpoints are chained by priority. The endpoint that
      succeeded is promoted to the front ("sticky success") and a failing one is
      demoted to the back, so the common case never pays a timeout first.
    * ``timeout_seconds`` is a TOTAL budget split across the remaining endpoints,
      so a fully dead chain still costs about 3 s instead of N x 3 s. The base
      class then counts one failed call and its circuit breaker does the rest.
    * All error semantics live in HttpClient, so an endpoint only has to build a
      request and parse a payload. That keeps the breaker's notion of a "failed
      call" unambiguous.

Privacy / safety
    Only the subtitle text and the two language codes leave the machine. No user
    identifier, no telemetry, no API key, no local log file. The endpoints are
    public, keyless and may be unavailable - that is exactly why the abstract
    layer exists and why PRIVACY.md documents them explicitly.

v0.2 note
    ``register_translator()`` / ``available_translators()`` / ``create_translator()``
    exist so the multi-source UI can grow, but v0.1 ships exactly one backend.
"""

from __future__ import annotations

import abc
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

try:  # project-internal import; the file also works stand-alone
    from src.ai.base_translator import (
        DEFAULT_TIMEOUT_S,
        BaseTranslator,
        SUPPORTED_LANGUAGES,
        TranslationResponseError,
        TranslationTimeoutError,
        TranslationUnavailableError,
        normalize_language,
    )
except Exception:  # pragma: no cover - degraded import path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from src.ai.base_translator import (  # type: ignore[no-redef]
        DEFAULT_TIMEOUT_S,
        BaseTranslator,
        SUPPORTED_LANGUAGES,
        TranslationResponseError,
        TranslationTimeoutError,
        TranslationUnavailableError,
        normalize_language,
    )

__all__ = [
    "USER_AGENT_DEFAULT",
    "MAX_RESPONSE_BYTES",
    "HttpResponse",
    "HttpClient",
    "OnlineTranslator",
    "available_endpoints",
    "register_translator",
    "available_translators",
    "create_translator",
]

logger = logging.getLogger("submirror.ai.online_translator")

USER_AGENT_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 512 * 1024          # a translation payload is tiny; cap it
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
DEFAULT_LIBRE_BASE = "https://translate.astian.org"
ENDPOINT_SLICE_FLOOR_S = 0.15            # never give an endpoint less than this
DEFAULT_ENDPOINT_ORDER = ("google-gtx", "mymemory", "libretranslate")


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #

@dataclass
class HttpResponse:
    """One HTTP response with lazy JSON decoding."""

    status: int = 200
    body: bytes = b""
    url: str = ""
    elapsed_ms: float = 0.0

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        raw = self.text().strip()
        if not raw:
            raise TranslationResponseError("empty response body")
        if raw.startswith("<"):
            raise TranslationResponseError("html response (endpoint blocked?)")
        try:
            return json.loads(raw)
        except Exception as exc:                      # noqa: BLE001
            raise TranslationResponseError(f"invalid json: {exc}") from exc


class HttpClient:
    """
    Blocking HTTP client with a stdlib back end and an optional requests back end.

    Every network exception is mapped to the low level Translation* errors here,
    so callers (and the circuit breaker) never deal with urllib internals.
    """

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S,
                 user_agent: str = USER_AGENT_DEFAULT,
                 prefer_requests: bool = True) -> None:
        self._lock = threading.Lock()
        self._timeout = max(0.2, float(timeout_s or DEFAULT_TIMEOUT_S))
        self._user_agent = user_agent or USER_AGENT_DEFAULT
        self._session: Any = None
        self.backend = "urllib"
        if prefer_requests:
            try:                                     # pragma: no cover - optional
                import requests

                session = requests.Session()
                session.headers.update({
                    "User-Agent": self._user_agent,
                    "Accept": "application/json, text/plain, */*",
                    # Ask for identity so no gzip branch is ever needed.
                    "Accept-Encoding": "identity",
                })
                self._session = session
                self.backend = "requests"
            except Exception:
                self._session = None

    # -- configuration ----------------------------------------------------- #

    @property
    def timeout_s(self) -> float:
        return self._timeout

    def set_timeout(self, timeout_s: float) -> None:
        with self._lock:
            self._timeout = max(0.2, float(timeout_s or DEFAULT_TIMEOUT_S))

    def set_user_agent(self, user_agent: str) -> None:
        with self._lock:
            self._user_agent = user_agent or USER_AGENT_DEFAULT
        if self._session is not None:
            try:
                self._session.headers.update({"User-Agent": self._user_agent})
            except Exception:
                logger.debug("could not update requests user-agent", exc_info=True)

    # -- requests ---------------------------------------------------------- #

    def get(self, url: str, timeout_s: Optional[float] = None,
            headers: Optional[Dict[str, str]] = None) -> HttpResponse:
        return self._request("GET", url, timeout_s=timeout_s, headers=headers)

    def post_json(self, url: str, payload: Any,
                  timeout_s: Optional[float] = None,
                  headers: Optional[Dict[str, str]] = None) -> HttpResponse:
        merged = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            merged.update(headers)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request("POST", url, timeout_s=timeout_s, headers=merged,
                             data=body)

    def _request(self, method: str, url: str, timeout_s: Optional[float] = None,
                 headers: Optional[Dict[str, str]] = None,
                 data: Optional[bytes] = None) -> HttpResponse:
        timeout = float(timeout_s or self._timeout)
        timeout = max(0.1, timeout)
        started = time.monotonic()
        if self._session is not None:
            response = self._request_requests(method, url, headers, data, timeout)
        else:
            response = self._request_urllib(method, url, headers, data, timeout)
        response.url = url
        response.elapsed_ms = (time.monotonic() - started) * 1000.0
        return response

    def _request_urllib(self, method: str, url: str,
                        headers: Optional[Dict[str, str]],
                        data: Optional[bytes], timeout: float) -> HttpResponse:
        merged = {
            "User-Agent": self._user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",
        }
        if headers:
            merged.update(headers)
        request = urllib.request.Request(url, data=data, headers=merged,
                                         method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as handle:
                status = int(getattr(handle, "status", 200) or 200)
                body = handle.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            raise self._http_status_error(code, url) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TranslationTimeoutError(
                    f"request to {urllib.parse.urlsplit(url).netloc} exceeded "
                    f"{timeout:.1f}s") from exc
            if isinstance(reason, ssl.SSLError):
                raise TranslationUnavailableError(f"tls error: {reason}") from exc
            raise TranslationUnavailableError(f"network error: {reason}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TranslationTimeoutError(
                f"request to {urllib.parse.urlsplit(url).netloc} exceeded "
                f"{timeout:.1f}s") from exc
        except ssl.SSLError as exc:
            raise TranslationUnavailableError(f"tls error: {exc}") from exc
        except Exception as exc:                          # noqa: BLE001
            raise TranslationUnavailableError(
                f"{exc.__class__.__name__}: {exc}") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise TranslationResponseError("response too large")
        return HttpResponse(status=status, body=body)

    def _request_requests(self, method: str, url: str,            # pragma: no cover
                          headers: Optional[Dict[str, str]],
                          data: Optional[bytes], timeout: float) -> HttpResponse:
        session = self._session
        try:
            response = session.request(method.upper(), url, data=data,
                                       headers=headers or {}, timeout=timeout)
        except Exception as exc:                          # noqa: BLE001
            name = exc.__class__.__name__
            if name in ("Timeout", "ReadTimeout", "ConnectTimeout"):
                raise TranslationTimeoutError(
                    f"request to {urllib.parse.urlsplit(url).netloc} exceeded "
                    f"{timeout:.1f}s") from exc
            if name in ("SSLError",):
                raise TranslationUnavailableError(f"tls error: {exc}") from exc
            if name in ("ConnectionError", "ProxyError", "TooManyRedirects"):
                raise TranslationUnavailableError(f"network error: {exc}") from exc
            raise TranslationUnavailableError(f"{name}: {exc}") from exc
        if response.status_code >= 400:
            raise self._http_status_error(int(response.status_code), url)
        body = response.content[:MAX_RESPONSE_BYTES + 1]
        if len(body) > MAX_RESPONSE_BYTES:
            raise TranslationResponseError("response too large")
        return HttpResponse(status=int(response.status_code), body=body)

    @staticmethod
    def _http_status_error(code: int, url: str):
        host = urllib.parse.urlsplit(url).netloc
        if code == 429:
            return TranslationUnavailableError(f"{host} rate-limited (http 429)")
        if code in (401, 403):
            return TranslationUnavailableError(f"{host} refused the request (http {code})")
        if 500 <= code <= 599:
            return TranslationUnavailableError(f"{host} server error (http {code})")
        return TranslationResponseError(f"{host} returned http {code}")

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                logger.debug("requests session close failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

class _Endpoint(abc.ABC):
    """Builds one provider request and parses its payload. Nothing else."""

    name: str = "endpoint"
    display_name: str = "Endpoint"
    priority: int = 100
    supports_auto_source: bool = True
    note: str = ""

    def __init__(self, client: HttpClient, config: Any = None) -> None:
        self._client = client
        self._config = config

    @classmethod
    def available(cls) -> Tuple[bool, str]:
        return True, ""

    @abc.abstractmethod
    def translate(self, text: str, source_lang: str, target_lang: str,
                  timeout_s: float) -> str:
        """Return the translated text or raise a Translation* error."""

    def _map_language(self, mapping: Dict[str, str], code: str) -> str:
        canonical = normalize_language(code)
        value = mapping.get(canonical)
        if value is None:
            raise TranslationUnavailableError(
                f"{self.name} does not support language '{code}'")
        return value

    def _get(self, key: str, default: Any) -> Any:
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def info(self) -> Dict[str, Any]:
        available, reason = self.available()
        return {"name": self.name, "display_name": self.display_name,
                "priority": self.priority,
                "supports_auto_source": self.supports_auto_source,
                "available": available, "note": self.note or reason}


class _GoogleGtxEndpoint(_Endpoint):
    """translate.googleapis.com/translate_a/single?client=gtx - keyless, stable."""

    name = "google-gtx"
    display_name = "Google (gtx)"
    priority = 10
    supports_auto_source = True
    note = "keyless public endpoint"

    LANGS = {code: code for code in SUPPORTED_LANGUAGES}

    def translate(self, text: str, source_lang: str, target_lang: str,
                  timeout_s: float) -> str:
        params = {
            "client": "gtx",
            "sl": self._map_language(self.LANGS, source_lang),
            "tl": self._map_language(self.LANGS, target_lang),
            "dt": "t",
            "dj": "1",              # object form; the array form is also handled
            "ie": "UTF-8",
            "oe": "UTF-8",
            "q": text,
        }
        url = GOOGLE_URL + "?" + urllib.parse.urlencode(params)
        response = self._client.get(url, timeout_s=timeout_s)
        return _google_payload_to_text(response.json())


def _google_payload_to_text(data: Any) -> str:
    """Accept both the classic array form and the ``dj=1`` object form."""
    pieces: List[str] = []
    if isinstance(data, dict):
        for sentence in (data.get("sentences") or []):
            if isinstance(sentence, dict):
                piece = sentence.get("trans") or sentence.get("transl")
                if piece:
                    pieces.append(str(piece))
    elif isinstance(data, (list, tuple)):
        # [[["译文","原文",...], ["译文2","原文2",...]], ...]
        for segment in (data[0] if data else []):
            if isinstance(segment, (list, tuple)) and segment:
                piece = segment[0]
                if isinstance(piece, str) and piece:
                    pieces.append(piece)
    if not pieces:
        raise TranslationResponseError("google payload contained no translation")
    return "".join(pieces).strip()


class _MyMemoryEndpoint(_Endpoint):
    """api.mymemory.translated.net/get - keyless, but needs an explicit source."""

    name = "mymemory"
    display_name = "MyMemory"
    priority = 20
    supports_auto_source = False        # no autodetect: skipped when source=auto
    note = "keyless public endpoint; requires an explicit source language"

    LANGS = {code: code for code in SUPPORTED_LANGUAGES}

    def translate(self, text: str, source_lang: str, target_lang: str,
                  timeout_s: float) -> str:
        source = normalize_language(source_lang)
        if source == "auto":
            raise TranslationUnavailableError(
                "mymemory needs an explicit source language")
        params = {
            "q": text,
            "langpair": f"{self._map_language(self.LANGS, source)}|"
                        f"{self._map_language(self.LANGS, target_lang)}",
            "mt": "1",
        }
        email = str(self._get("translate.mymemory_email", "") or "").strip()
        if email:
            params["de"] = email
        url = MYMEMORY_URL + "?" + urllib.parse.urlencode(params)
        response = self._client.get(url, timeout_s=timeout_s)
        data = response.json()
        if not isinstance(data, dict):
            raise TranslationResponseError("mymemory payload was not an object")
        status = data.get("responseStatus")
        if status is not None:
            try:
                if int(status) != 200:
                    detail = str(data.get("responseDetails") or status)[:120]
                    raise TranslationResponseError(f"mymemory status {status}: {detail}")
            except (TypeError, ValueError):
                pass
        translated = ""
        payload = data.get("responseData")
        if isinstance(payload, dict):
            translated = str(payload.get("translatedText") or "")
        translated = translated.strip()
        if not translated:
            raise TranslationResponseError("mymemory returned no translation")
        if "MYMEMORY WARNING" in translated.upper():
            raise TranslationResponseError("mymemory quota warning")
        return translated


class _LibreTranslateEndpoint(_Endpoint):
    """Self-hosted or public LibreTranslate instance (POST /translate)."""

    name = "libretranslate"
    display_name = "LibreTranslate"
    priority = 30
    supports_auto_source = True
    note = "keyless public/self-hosted instance; configurable base url"

    LANGS = {
        "auto": "auto", "zh-CN": "zh", "zh-TW": "zh", "en": "en", "ja": "ja",
        "ko": "ko", "ru": "ru", "fr": "fr", "de": "de", "es": "es", "pt": "pt",
        "it": "it", "ar": "ar", "th": "th", "vi": "vi",
    }

    def __init__(self, client: HttpClient, config: Any = None) -> None:
        super().__init__(client, config)
        base = str(self._get("translate.libre_base_url", DEFAULT_LIBRE_BASE) or
                   DEFAULT_LIBRE_BASE).strip()
        self.base_url = base.rstrip("/") or DEFAULT_LIBRE_BASE
        self.endpoint_url = self.base_url + "/translate"

    def translate(self, text: str, source_lang: str, target_lang: str,
                  timeout_s: float) -> str:
        payload = {
            "q": text,
            "source": self._map_language(self.LANGS, source_lang),
            "target": self._map_language(self.LANGS, target_lang),
            "format": "text",
        }
        response = self._client.post_json(self.endpoint_url, payload,
                                         timeout_s=timeout_s)
        data = response.json()
        if not isinstance(data, dict):
            raise TranslationResponseError("libretranslate payload was not an object")
        if data.get("error"):
            raise TranslationResponseError(f"libretranslate: {data['error']}")
        translated = str(data.get("translatedText") or "").strip()
        if not translated:
            raise TranslationResponseError("libretranslate returned no translation")
        return translated


_ENDPOINT_CLASSES: Tuple[Type[_Endpoint], ...] = (
    _GoogleGtxEndpoint,
    _MyMemoryEndpoint,
    _LibreTranslateEndpoint,
)
_ENDPOINT_BY_NAME: Dict[str, Type[_Endpoint]] = {
    cls.name: cls for cls in _ENDPOINT_CLASSES
}


def available_endpoints() -> List[Dict[str, Any]]:
    """Cheap, side-effect free description of the built-in endpoints."""
    result: List[Dict[str, Any]] = []
    for cls in sorted(_ENDPOINT_CLASSES, key=lambda c: c.priority):
        ok, reason = (False, "unavailable")
        try:
            ok, reason = cls.available()
        except Exception:                                # noqa: BLE001
            logger.debug("endpoint availability probe failed for %s", cls.name,
                         exc_info=True)
        result.append({"name": cls.name, "display_name": cls.display_name,
                       "priority": cls.priority,
                       "supports_auto_source": cls.supports_auto_source,
                       "available": bool(ok), "note": cls.note or reason})
    return result


# --------------------------------------------------------------------------- #
# Online translator
# --------------------------------------------------------------------------- #

class OnlineTranslator(BaseTranslator):
    """
    Failover chain over the keyless public endpoints described above.

    BaseTranslator owns validation, caching, retries and the circuit breaker;
    this class only decides *which endpoint* to ask and in what order.
    """

    name = "online"
    display_name = "Online public endpoints"
    requires_api_key = False
    supported_languages = SUPPORTED_LANGUAGES
    default_timeout_s = DEFAULT_TIMEOUT_S

    def __init__(self, config: Any = None, parent=None,
                 endpoints: Optional[Sequence[str]] = None) -> None:
        # Attributes the overridden apply_config() relies on must exist before
        # BaseTranslator.__init__ triggers it.
        self._client: Optional[HttpClient] = None
        self._order: List[_Endpoint] = []
        self._explicit_endpoints: List[str] = [str(x) for x in (endpoints or [])]
        self._endpoint_failures: Dict[str, int] = {}
        self.last_endpoint: str = ""
        super().__init__(config, parent)

    # -- availability ------------------------------------------------------ #

    @classmethod
    def check_available(cls) -> Tuple[bool, str]:
        transport = "urllib (stdlib)"
        try:
            import importlib.util

            if importlib.util.find_spec("requests") is not None:
                transport = "requests"
        except Exception:
            transport = "urllib (stdlib)"
        usable = [cls.name for cls in sorted(_ENDPOINT_CLASSES,
                                             key=lambda c: c.priority)]
        return True, (f"{len(usable)} keyless endpoints "
                      f"({', '.join(usable)}) via {transport}")

    # -- configuration ----------------------------------------------------- #

    def apply_config(self) -> None:
        super().apply_config()
        prefer_requests = bool(self._get("translate.prefer_requests", True))
        user_agent = str(self._get("translate.http_user_agent",
                                   USER_AGENT_DEFAULT) or USER_AGENT_DEFAULT)
        previous, self._client = self._client, HttpClient(
            timeout_s=self.timeout_s, user_agent=user_agent,
            prefer_requests=prefer_requests)
        if previous is not None:
            previous.close()
        self._order = self._build_endpoints()

    def _build_endpoints(self) -> List[_Endpoint]:
        client = self._client
        if client is None or isinstance(client, type(None)):
            return []
        names = self._explicit_endpoints or self._configured_order()
        built: List[_Endpoint] = []
        for name in names:
            cls = _ENDPOINT_BY_NAME.get(str(name).strip().lower())
            if cls is None:
                logger.warning("unknown translation endpoint '%s' ignored", name)
                continue
            try:
                ok, reason = cls.available()
            except Exception:                              # noqa: BLE001
                ok, reason = False, "probe failed"
            if not ok:
                logger.warning("translation endpoint %s unavailable: %s",
                               cls.name, reason)
                continue
            try:
                built.append(cls(client, self._config))
            except Exception:                              # noqa: BLE001
                logger.exception("could not build translation endpoint %s", cls.name)
        if not built:
            logger.error("no usable translation endpoint; translations will fail")
        return built

    def _configured_order(self) -> List[str]:
        raw = self._get("translate.endpoints", None)
        if isinstance(raw, str):
            raw = [part for part in raw.replace(";", ",").split(",") if part.strip()]
        if isinstance(raw, (list, tuple)) and raw:
            return [str(item) for item in raw]
        return list(DEFAULT_ENDPOINT_ORDER)

    @property
    def endpoint_names(self) -> List[str]:
        return [endpoint.name for endpoint in self._order]

    @property
    def transport_name(self) -> str:
        return self._client.backend if self._client is not None else "none"

    # -- translation ------------------------------------------------------- #

    def _translate(self, text: str, source_lang: str, target_lang: str,
                   timeout_s: float) -> str:
        order = list(self._order)
        if not order:
            raise TranslationUnavailableError("no translation endpoint available")

        started = time.monotonic()
        errors: List[str] = []
        timed_out = 0

        for index, endpoint in enumerate(order):
            left = timeout_s - (time.monotonic() - started)
            if left <= 0.02:
                errors.append("time budget exhausted")
                break
            # Split the remaining budget across the endpoints still to try, so
            # the total stays inside ``timeout_s`` no matter how many fail.
            slots = max(1, len(order) - index)
            slice_s = min(left, max(ENDPOINT_SLICE_FLOOR_S, left / slots))

            if normalize_language(source_lang) == "auto" and \
                    not endpoint.supports_auto_source:
                errors.append(f"{endpoint.name}: needs an explicit source language")
                continue

            try:
                value = endpoint.translate(text, source_lang, target_lang, slice_s)
            except TranslationTimeoutError:
                timed_out += 1
                errors.append(f"{endpoint.name}: timeout")
                logger.debug("endpoint %s timed out after %.2fs", endpoint.name,
                             slice_s)
                continue
            except (TranslationUnavailableError, TranslationResponseError) as exc:
                errors.append(f"{endpoint.name}: {exc}")
                self._note_failure(endpoint)
                logger.debug("endpoint %s failed: %s", endpoint.name, exc)
                continue
            except Exception as exc:                        # noqa: BLE001
                errors.append(f"{endpoint.name}: {exc.__class__.__name__}")
                self._note_failure(endpoint)
                logger.exception("endpoint %s raised unexpectedly", endpoint.name)
                continue

            if value:
                self._promote(endpoint)
                self.last_endpoint = endpoint.name
                return value
            errors.append(f"{endpoint.name}: empty response")
            self._note_failure(endpoint)

        message = "; ".join(errors) or "all translation endpoints failed"
        if errors and timed_out == len(errors):
            raise TranslationTimeoutError(message)
        raise TranslationUnavailableError(message)

    # -- endpoint ordering ------------------------------------------------- #

    def _promote(self, endpoint: _Endpoint) -> None:
        """Sticky success: the endpoint that worked becomes the first choice."""
        try:
            self._order.remove(endpoint)
        except ValueError:
            return
        self._order.insert(0, endpoint)

    def _demote(self, endpoint: _Endpoint) -> None:
        try:
            self._order.remove(endpoint)
        except ValueError:
            return
        self._order.append(endpoint)

    def _note_failure(self, endpoint: _Endpoint) -> None:
        self._endpoint_failures[endpoint.name] = \
            self._endpoint_failures.get(endpoint.name, 0) + 1
        self._demote(endpoint)

    # -- lifecycle and diagnostics ----------------------------------------- #

    def _on_close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._order = []

    def stats(self) -> Dict[str, Any]:
        data = super().stats()
        data["transport"] = self.transport_name
        data["endpoints"] = self.endpoint_names
        data["last_endpoint"] = self.last_endpoint
        data["endpoint_failures"] = dict(self._endpoint_failures)
        return data


# --------------------------------------------------------------------------- #
# v0.2 seam: translator registry (v0.1 ships exactly one backend)
# --------------------------------------------------------------------------- #

_TRANSLATOR_REGISTRY: Dict[str, Type[BaseTranslator]] = {OnlineTranslator.name: OnlineTranslator}
_REGISTRY_LOCK = threading.Lock()


def register_translator(cls: Type[BaseTranslator]) -> Type[BaseTranslator]:
    """Register a translator class so the v0.2 source picker can list it."""
    name = str(getattr(cls, "name", "") or "").strip()
    if not name:
        raise ValueError("translator class must define a non-empty 'name'")
    with _REGISTRY_LOCK:
        _TRANSLATOR_REGISTRY[name] = cls
    return cls


def available_translators() -> List[Dict[str, Any]]:
    with _REGISTRY_LOCK:
        classes = list(_TRANSLATOR_REGISTRY.values())
    result: List[Dict[str, Any]] = []
    for cls in classes:
        ok, reason = True, ""
        probe = getattr(cls, "check_available", None)
        if callable(probe):
            try:
                ok, reason = probe()
            except Exception:                              # noqa: BLE001
                ok, reason = False, "availability probe failed"
        result.append({"name": cls.name,
                       "display_name": getattr(cls, "display_name", cls.name),
                       "requires_api_key": bool(getattr(cls, "requires_api_key", False)),
                       "available": bool(ok), "note": reason})
    return result


def create_translator(name: str = OnlineTranslator.name,
                      config: Any = None) -> Optional[BaseTranslator]:
    """Build a registered translator; returns None instead of raising."""
    with _REGISTRY_LOCK:
        cls = _TRANSLATOR_REGISTRY.get(str(name).strip().lower())
    if cls is None:
        logger.error("unknown translator '%s'", name)
        return None
    try:
        return cls(config)                                 # type: ignore[call-arg]
    except Exception:                                      # noqa: BLE001
        logger.exception("could not build translator '%s'", name)
        return None
