# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - WASAPI loopback capture of system audio.

Why loopback and never a microphone
    The pipeline plays translated speech through TTS. A microphone would record
    that playback and feed it straight back into the recogniser, producing an
    endless echo loop. WASAPI loopback taps the render endpoint instead, so we
    hear exactly what the game outputs.

Backends, best first
    * ``pyaudiowpatch`` - PyAudio fork exposing WASAPI loopback devices.
    * ``soundcard``      - ``include_loopback=True`` recorder.
    * ``NullBackend``    - synthetic silence so the UI can boot and self-test
      without any audio hardware; reports ``is_synthetic = True``.

Threading
    Capture callbacks run on the PortAudio / soundcard thread. They only copy
    samples into the ring buffer, update counters and (throttled to 20 Hz) emit
    the level signal. Qt marshals those signals to the UI thread automatically.

Safety: user-mode only. Documented public audio APIs, no injection, no hooks,
no reading of another process's memory.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:  # pragma: no cover - optional Qt
    from PySide6.QtCore import QObject, Signal
    _HAS_QT = True
except Exception:  # pragma: no cover
    _HAS_QT = False

    class _FallbackSignal:  # minimal stand-in, mirrors src/core/subtitle_queue.py
        def __init__(self, *_a, **_k) -> None:
            self._slots: List[Any] = []

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
                    logging.getLogger("submirror.audio").exception("slot failed")

    def Signal(*_a, **_k):  # type: ignore[misc]
        return _FallbackSignal()

    class QObject:  # type: ignore[no-redef]
        pass

from src.audio.ring_buffer import (
    AudioRingBuffer,
    LevelMeter,
    Resampler,
    TARGET_SAMPLE_RATE,
)

__all__ = [
    "AudioDeviceInfo",
    "CaptureStats",
    "LoopbackCapture",
    "CaptureError",
    "DeviceNotFoundError",
    "BackendUnavailableError",
]

logger = logging.getLogger("submirror.audio")

DEFAULT_BLOCK_SECONDS = 0.05        # 50 ms tap size
MAX_CONSECUTIVE_ERRORS = 5
DEFAULT_POLL_SECONDS = 5.0          # default-device watchdog interval
LEVEL_THROTTLE_S = 0.05             # 20 Hz overlay updates


# --------------------------------------------------------------------------- #
# Data structures and errors
# --------------------------------------------------------------------------- #

@dataclass
class AudioDeviceInfo:
    """One render endpoint usable for loopback capture."""

    index: int
    name: str
    channels: int = 2
    sample_rate: int = 48_000
    is_loopback: bool = True
    is_default: bool = False

    def matches(self, selector: Any) -> bool:
        """True when *selector* (index, or name substring) picks this device."""
        if selector in (None, "", "default"):
            return self.is_default
        if isinstance(selector, int):
            return self.index == selector
        text = str(selector).strip()
        if text.isdigit():
            return self.index == int(text)
        return text.lower() in self.name.lower()


@dataclass
class CaptureStats:
    """Counters surfaced by the diagnostics page."""

    frames: int = 0
    samples: int = 0
    seconds: float = 0.0
    errors: int = 0
    restarts: int = 0
    started_at: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "frames": self.frames,
            "samples": self.samples,
            "seconds": round(self.seconds, 2),
            "errors": self.errors,
            "restarts": self.restarts,
        }


class CaptureError(RuntimeError):
    """Base class for capture problems."""


class BackendUnavailableError(CaptureError):
    """No loopback-capable backend could be used."""


class DeviceNotFoundError(CaptureError):
    """The configured device does not exist."""


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class _BaseBackend:
    """Interface shared by the loopback backends."""

    name = "base"
    is_synthetic = False

    @classmethod
    def available(cls) -> bool:
        return False

    def list_devices(self) -> List[AudioDeviceInfo]:
        return []

    def default_device(self) -> Optional[AudioDeviceInfo]:
        devices = self.list_devices()
        for device in devices:
            if device.is_default:
                return device
        return devices[0] if devices else None

    # -- microphone (input) endpoints, v0.1.19 ----------------------------- #
    # A loopback endpoint is opened as an *input* stream on the render device,
    # so the very same backend that records the system output can record the
    # microphone: only the enumeration differs. Backends that cannot tell the
    # two apart answer an empty list and the caller falls back to silence.

    def list_input_devices(self) -> List[AudioDeviceInfo]:
        """Real (non-loopback) input endpoints available to this backend."""
        return []

    def default_input_device(self) -> Optional[AudioDeviceInfo]:
        devices = self.list_input_devices()
        for device in devices:
            if device.is_default:
                return device
        return devices[0] if devices else None

    def open(self, device: AudioDeviceInfo,
             on_audio: Callable[[Any, int, int], None],
             block_frames: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _PyaudioWpBackend(_BaseBackend):
    """pyaudiowpatch (or PyAudio) WASAPI loopback backend."""

    name = "pyaudiowpatch"
    is_synthetic = False

    def __init__(self) -> None:
        self._pa = None
        self._stream = None

    @classmethod
    def _module(cls):
        try:
            import pyaudiowpatch as module  # preferred fork
            return module
        except Exception:
            try:
                import pyaudio as module  # type: ignore
                if hasattr(module, "get_loopback_device_info_generator"):
                    return module
            except Exception:
                pass
        return None

    @classmethod
    def available(cls) -> bool:
        return cls._module() is not None

    def _audio(self):
        module = self._module()
        if module is None:
            raise BackendUnavailableError("pyaudiowpatch / pyaudio not installed")
        if self._pa is None:
            self._pa = module.PyAudio()
        return module, self._pa

    def list_devices(self) -> List[AudioDeviceInfo]:
        try:
            _module, pa = self._audio()
        except CaptureError:
            return []

        devices: List[AudioDeviceInfo] = []
        default_index = -1
        try:
            default_info = pa.get_default_wasapi_loopback()
            default_index = int(default_info["index"])
        except Exception:
            logger.debug("no default WASAPI loopback endpoint reported")

        generator = getattr(pa, "get_loopback_device_info_generator", None)
        if generator is None:
            return devices
        try:
            for info in generator():
                index = int(info["index"])
                devices.append(AudioDeviceInfo(
                    index=index,
                    name=str(info.get("name", "loopback %d" % index)),
                    channels=int(info.get("maxInputChannels") or 2),
                    sample_rate=int(info.get("defaultSampleRate") or 48_000),
                    is_loopback=True,
                    is_default=(index == default_index),
                ))
        except Exception:
            logger.exception("failed to enumerate loopback devices")

        if devices and default_index < 0:
            devices[0].is_default = True
        return devices

    def list_input_devices(self) -> List[AudioDeviceInfo]:
        """
        Real (non-loopback) capture endpoints: microphones.

        PyAudioWPatch lists WASAPI endpoints of both directions, so the input
        side is the same enumeration with two filters: the device must accept
        input channels and must not be a loopback mirror of a render device.
        """
        try:
            _module, pa = self._audio()
        except CaptureError:
            return []

        devices: List[AudioDeviceInfo] = []
        default_index = -1
        try:
            default_info = pa.get_default_input_device_info()
            default_index = int(default_info["index"])
        except Exception:
            logger.debug("no default WASAPI input endpoint reported")

        infos: List[Any] = []
        try:
            generator = getattr(pa, "get_device_info_generator", None)
            if generator is not None:
                infos = list(generator())
            else:  # pragma: no cover - very old builds
                infos = [pa.get_device_info_by_index(i)
                         for i in range(int(pa.get_device_count()))]
        except Exception:
            logger.exception("failed to enumerate input devices")
            return devices

        for info in infos:
            try:
                if int(info.get("maxInputChannels") or 0) <= 0:
                    continue
                name = str(info.get("name", ""))
                if bool(info.get("isLoopbackDevice", False)):
                    continue
                if "loopback" in name.lower():
                    continue
                index = int(info["index"])
                devices.append(AudioDeviceInfo(
                    index=index,
                    name=name or "input %d" % index,
                    channels=int(info.get("maxInputChannels") or 1),
                    sample_rate=int(info.get("defaultSampleRate") or 48_000),
                    is_loopback=False,
                    is_default=(index == default_index),
                ))
            except Exception:
                logger.debug("skipping malformed input device %r", info)

        if devices and not any(d.is_default for d in devices):
            devices[0].is_default = True
        return devices

    def open(self, device: AudioDeviceInfo,
             on_audio: Callable[[Any, int, int], None],
             block_frames: int) -> None:
        module, pa = self._audio()

        def _callback(in_data, _frame_count, _time_info, status):
            if status:
                logger.debug("portaudio status: %s", status)
            try:
                on_audio(in_data, device.channels, device.sample_rate)
            except Exception:
                logger.exception("audio callback failed")
            return (None, module.paContinue)

        self._stream = pa.open(
            format=module.paInt16,
            channels=max(1, int(device.channels)),
            rate=int(device.sample_rate),
            input=True,
            frames_per_buffer=max(256, int(block_frames)),
            input_device_index=int(device.index),
            stream_callback=_callback,
        )
        self._stream.start_stream()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                logger.debug("stream close failed", exc_info=True)
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                logger.debug("portaudio terminate failed", exc_info=True)
            self._pa = None


class _SoundcardBackend(_BaseBackend):
    """soundcard backend driven by a dedicated reader thread."""

    name = "soundcard"
    is_synthetic = False

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._recorder = None

    @classmethod
    def available(cls) -> bool:
        try:
            import soundcard  # noqa: F401
            return True
        except Exception:
            return False

    def list_devices(self) -> List[AudioDeviceInfo]:
        try:
            import soundcard as sc
        except Exception:
            return []

        devices: List[AudioDeviceInfo] = []
        default_name = ""
        try:
            speaker = sc.default_speaker()
            default_name = getattr(speaker, "name", "")
        except Exception:
            logger.debug("no default speaker reported")

        try:
            microphones = sc.all_microphones(include_loopback=True)
        except Exception:
            logger.exception("failed to enumerate soundcard loopback devices")
            return devices

        for index, mic in enumerate(microphones):
            if not getattr(mic, "isloopback", False):
                continue
            name = str(getattr(mic, "name", "loopback %d" % index))
            devices.append(AudioDeviceInfo(
                index=index,
                name=name,
                channels=2,
                sample_rate=48_000,
                is_loopback=True,
                is_default=bool(default_name and default_name in name),
            ))
        if devices and not any(d.is_default for d in devices):
            devices[0].is_default = True
        return devices

    def list_input_devices(self) -> List[AudioDeviceInfo]:
        """Real (non-loopback) microphones reported by ``soundcard``."""
        try:
            import soundcard as sc
        except Exception:
            return []

        devices: List[AudioDeviceInfo] = []
        default_name = ""
        try:
            default_name = str(getattr(sc.default_microphone(), "name", ""))
        except Exception:
            logger.debug("no default microphone reported")

        try:
            microphones = sc.all_microphones(include_loopback=False)
        except Exception:
            logger.exception("failed to enumerate soundcard microphones")
            return devices

        for index, mic in enumerate(microphones):
            name = str(getattr(mic, "name", "microphone %d" % index))
            devices.append(AudioDeviceInfo(
                index=index,
                name=name,
                channels=1,
                sample_rate=48_000,
                is_loopback=False,
                is_default=bool(default_name and default_name in name),
            ))
        if devices and not any(d.is_default for d in devices):
            devices[0].is_default = True
        return devices

    def open(self, device: AudioDeviceInfo,
             on_audio: Callable[[Any, int, int], None],
             block_frames: int) -> None:
        import soundcard as sc

        microphones = sc.all_microphones(include_loopback=True)
        target = None
        for mic in microphones:
            if device.name and device.name == str(getattr(mic, "name", "")):
                target = mic
                break
        if target is None:
            target = sc.get_microphone(device.name, include_loopback=True)

        self._stop.clear()
        rate = int(device.sample_rate)
        channels = max(1, int(device.channels))

        def _reader() -> None:
            try:
                with target.recorder(samplerate=rate, channels=channels,
                                     blocksize=max(256, int(block_frames))) as recorder:
                    while not self._stop.is_set():
                        data = recorder.record(numframes=max(256, int(block_frames)))
                        on_audio(data, channels, rate)
            except Exception:
                logger.exception("soundcard reader thread stopped")

        self._thread = threading.Thread(target=_reader,
                                        name="SubMirrorSoundcard", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(2.0)


class _NullBackend(_BaseBackend):
    """
    Synthetic silence source.

    Keeps the application runnable and testable on machines without loopback
    support; the UI must show "no audio device" whenever ``is_synthetic`` is
    true, so silent subtitles are never mistaken for a working capture.
    """

    name = "null"
    is_synthetic = True

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @classmethod
    def available(cls) -> bool:
        return True

    def list_devices(self) -> List[AudioDeviceInfo]:
        return [AudioDeviceInfo(index=-1, name="(synthetic silence)",
                                channels=2, sample_rate=48_000,
                                is_loopback=True, is_default=True)]

    def list_input_devices(self) -> List[AudioDeviceInfo]:
        return [AudioDeviceInfo(index=-1, name="(synthetic microphone)",
                                channels=1, sample_rate=48_000,
                                is_loopback=False, is_default=True)]

    def open(self, device: AudioDeviceInfo,
             on_audio: Callable[[Any, int, int], None],
             block_frames: int) -> None:
        self._stop.clear()
        rate = int(device.sample_rate)
        frames = max(256, int(block_frames))
        silence = b"\x00\x00" * (frames * max(1, int(device.channels)))

        def _loop() -> None:
            interval = frames / float(rate)
            while not self._stop.wait(interval):
                on_audio(silence, device.channels, rate)

        self._thread = threading.Thread(target=_loop, name="SubMirrorNullAudio", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(2.0)


# --------------------------------------------------------------------------- #
# Capture driver
# --------------------------------------------------------------------------- #

def _list_endpoints(backend: _BaseBackend, kind: str) -> List[AudioDeviceInfo]:
    """Endpoints of one direction offered by a backend (v0.1.19 helper)."""
    if str(kind).lower().startswith("mic"):
        return backend.list_input_devices()
    return backend.list_devices()


def _default_endpoint(backend: _BaseBackend,
                      kind: str) -> Optional[AudioDeviceInfo]:
    """Backend default endpoint for one direction (v0.1.19 helper)."""
    if str(kind).lower().startswith("mic"):
        return backend.default_input_device()
    return backend.default_device()


class LoopbackCapture(QObject):
    """
    Owns the loopback stream and feeds the ring buffer.

    Signals
        started(str)         backend name, emitted once audio flows
        stopped()            capture ended (stop(), error or device loss)
        error(str)           human-readable failure reason
        level(float)         smoothed 0.0-1.0 input level (20 Hz)
        device_changed(str)  default endpoint switched and capture restarted
    """

    started = Signal(str)
    stopped = Signal()
    error = Signal(str)
    level = Signal(float)
    device_changed = Signal(str)

    def __init__(self, config: Any = None, ring_buffer: Optional[AudioRingBuffer] = None,
                 parent=None, kind: str = "loopback") -> None:
        super().__init__(parent)
        self._config = config
        self._kind = "microphone" if str(kind).lower().startswith("mic") else "loopback"
        self._lock = threading.RLock()
        self._running = False
        self._backend: Optional[_BaseBackend] = None
        self._device: Optional[AudioDeviceInfo] = None
        self._stats = CaptureStats()
        self._meter = LevelMeter()
        self._last_level_emit = 0.0
        self._consecutive_errors = 0
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._device_selector: Any = None
        #: v0.1.19: optional raw-block consumer. The mixing chain uses it to
        #: receive the frames of a secondary source (e.g. the microphone while
        #: the speaker is the primary stream).
        self._block_sink: Optional[Callable[[Any, int, int], None]] = None

        self.ring_buffer = ring_buffer or self._make_ring_buffer()

    # -- configuration ----------------------------------------------------- #

    def _make_ring_buffer(self) -> AudioRingBuffer:
        seconds = 30.0
        if self._config is not None:
            try:
                seconds = float(self._config.get("audio.ring_buffer_seconds", 30.0))
            except (TypeError, ValueError):
                pass
        return AudioRingBuffer(capacity_seconds=seconds, sample_rate=48_000)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_synthetic(self) -> bool:
        return bool(self._backend and self._backend.is_synthetic)

    @property
    def device(self) -> Optional[AudioDeviceInfo]:
        return self._device

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else ""

    @property
    def stats(self) -> Dict[str, Any]:
        return self._stats.as_dict()

    @property
    def kind(self) -> str:
        """``"loopback"`` (system sound) or ``"microphone"`` (v0.1.19)."""
        return self._kind

    @property
    def is_microphone(self) -> bool:
        return self._kind == "microphone"

    # -- device listing ---------------------------------------------------- #

    def _device_list(self, backend: _BaseBackend) -> List[AudioDeviceInfo]:
        """Endpoints this driver may open, in the direction it was built for."""
        return _list_endpoints(backend, self._kind)

    def _default_of(self, backend: _BaseBackend) -> Optional[AudioDeviceInfo]:
        return _default_endpoint(backend, self._kind)

    def _device_key(self) -> str:
        """Config key holding the endpoint selector for this direction."""
        return "audio.mic_device" if self._kind == "microphone" else "audio.device"

    @staticmethod
    def enumerate_devices(kind: str = "loopback") -> List[AudioDeviceInfo]:
        """All endpoints of one direction across available backends (best-first)."""
        for backend in (_PyaudioWpBackend(), _SoundcardBackend()):
            if not backend.available():
                continue
            devices = _list_endpoints(backend, kind)
            if devices:
                return devices
        return _list_endpoints(_NullBackend(), kind)

    def _select_backend(self) -> _BaseBackend:
        for backend in (_PyaudioWpBackend(), _SoundcardBackend()):
            if backend.available() and self._device_list(backend):
                return backend
        direction = "microphone" if self.is_microphone else "loopback"
        logger.warning("no %s backend available, falling back to synthetic silence", direction)
        return _NullBackend()

    def _resolve_device(self, backend: _BaseBackend) -> AudioDeviceInfo:
        selector = self._device_selector
        if selector is None and self._config is not None:
            try:
                selector = self._config.get(self._device_key(), "")
            except Exception:
                selector = ""
        self._device_selector = selector

        devices = self._device_list(backend)
        if not devices:
            raise DeviceNotFoundError("backend %s reported no %s device"
                                       % (backend.name, self._kind))

        if selector in (None, "", "default"):
            device = self._default_of(backend)
            if device is None:
                raise DeviceNotFoundError("no default %s device" % self._kind)
            return device

        for device in devices:
            if device.matches(selector):
                return device
        raise DeviceNotFoundError("audio device %r not found" % (selector,))

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> bool:
        """
        Open the loopback stream. Returns False when capture could not start;
        an ``error`` signal explains why.
        """
        with self._lock:
            if self._running:
                return True
            try:
                backend = self._select_backend()
                device = self._resolve_device(backend)
            except CaptureError as exc:
                logger.error("capture start failed: %s", exc)
                self.error.emit(str(exc))
                return False
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("unexpected capture start failure")
                self.error.emit("audio start failed: %s" % exc)
                return False

            # Ring buffer keeps the device's native rate; conversion happens later.
            if self.ring_buffer.sample_rate != device.sample_rate:
                self.ring_buffer = AudioRingBuffer(
                    capacity_seconds=self.ring_buffer.available_seconds or 30.0,
                    sample_rate=device.sample_rate,
                )

            block_frames = int(device.sample_rate * DEFAULT_BLOCK_SECONDS)
            self._backend = backend
            self._device = device
            self._stats = CaptureStats(started_at=time.monotonic())
            self._consecutive_errors = 0

            try:
                backend.open(device, self._on_audio, block_frames)
            except Exception as exc:
                logger.exception("backend %s failed to open", backend.name)
                self._backend = None
                self._device = None
                self.error.emit("无法打开音频设备: %s" % exc)
                return False

            self._running = True

        logger.info("capture started via %s on %r (%d Hz, %d ch) [%s]%s",
                    backend.name, device.name, device.sample_rate, device.channels,
                    self._kind, " [synthetic]" if backend.is_synthetic else "")
        self.started.emit(backend.name)
        if backend.is_synthetic:
            if self.is_microphone:
                self.error.emit("未检测到可用的麦克风设备，正在使用静音占位源")
            else:
                self.error.emit("未检测到可用的系统声音设备，正在使用静音占位源")
        self._start_watchdog()
        return True

    def stop(self, reason: str = "") -> None:
        """Close the stream and stop auxiliary threads; idempotent."""
        self._stop_watchdog()
        with self._lock:
            if not self._running and self._backend is None:
                return
            backend, self._backend = self._backend, None
            self._running = False
            self._device = None

        if backend is not None:
            try:
                backend.close()
            except Exception:
                logger.debug("backend close failed", exc_info=True)
        self._meter.reset()
        logger.info("capture stopped (%s)", reason or "requested")
        self.stopped.emit()

    def restart(self, reason: str = "") -> bool:
        """Stop then start, used on device change or settings reload."""
        self.stop(reason or "restart")
        self._stats.restarts += 1
        time.sleep(0.1)
        return self.start()

    # -- audio ingress ----------------------------------------------------- #

    def _on_audio(self, raw: Any, channels: int, sample_rate: int) -> None:
        """Backend callback: down-mix, store, meter and (throttled) emit."""
        try:
            mono = Resampler.to_mono(raw, channels)
            written = self.ring_buffer.write(mono)
        except Exception:
            self._consecutive_errors += 1
            self._stats.errors += 1
            logger.exception("failed to ingest audio block")
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self._fail("音频采集持续失败，已停止采集")
            return

        self._consecutive_errors = 0
        self._stats.frames += 1
        self._stats.samples += written
        self._stats.seconds += written / float(sample_rate or TARGET_SAMPLE_RATE)

        level = self._meter.update(mono)
        now = time.monotonic()
        if now - self._last_level_emit >= LEVEL_THROTTLE_S:
            self._last_level_emit = now
            self.level.emit(level)

        # A secondary consumer (the v0.1.19 mixing chain) sees the raw block
        # after the ring buffer and the meter, so its latency matches the
        # primary stream exactly.
        sink = self._block_sink
        if sink is not None:
            try:
                sink(raw, channels, sample_rate)
            except Exception:
                logger.exception("block sink failed")

    def set_block_sink(self, sink: Optional[Callable[[Any, int, int], None]]) -> None:
        """Register a raw-block consumer (``None`` clears it)."""
        self._block_sink = sink

    def _fail(self, message: str) -> None:
        """Report a fatal capture problem and shut the stream down."""
        logger.error("capture failure: %s", message)
        self.error.emit(message)
        self.stop("failure")

    # -- default-device watchdog ------------------------------------------- #

    def _start_watchdog(self) -> None:
        """Watch for endpoint switches (headset plug/unplug) when following default."""
        selector = self._device_selector
        if selector not in (None, "", "default"):
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._watch_loop,
                                          name="SubMirrorAudioWatch", daemon=True)
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread, self._watchdog = self._watchdog, None
        if thread and thread.is_alive():
            thread.join(1.0)

    def _watch_loop(self) -> None:
        previous = self._device.name if self._device else ""
        while not self._watchdog_stop.wait(DEFAULT_POLL_SECONDS):
            with self._lock:
                backend = self._backend
                running = self._running
            if not running or backend is None:
                return
            try:
                current = backend.default_device()
            except Exception:
                continue
            if current is None or current.name == previous:
                continue
            previous = current.name
            logger.info("default loopback device changed to %r", current.name)
            self.device_changed.emit(current.name)
            self.restart("default device changed")

    # -- consumers --------------------------------------------------------- #

    def take_audio(self, seconds: float = 5.0,
                   sample_rate: int = TARGET_SAMPLE_RATE):
        """Resampled mono float32 window for the recogniser (16 kHz by default)."""
        return self.ring_buffer.read_window(seconds, rate=sample_rate)

    def current_level(self) -> float:
        """Latest smoothed level without subscribing to the signal."""
        return self._meter.level
