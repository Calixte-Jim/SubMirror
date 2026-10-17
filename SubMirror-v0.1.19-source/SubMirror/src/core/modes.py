"""Work-mode presets (v0.1.19): all-round, game and conversation.

A *mode* is a named bundle of configuration values. Picking a mode in the
settings panel loads that bundle into the form, so the numbers stay visible and
editable instead of hiding behind the dropdown. When ``app.mode_auto_params`` is
on, :func:`apply_mode` also writes the bundle into the config file at start-up,
which is what makes a mode's intent survive a hand-edited config.

The module deliberately imports neither Qt nor :mod:`src.core.config`: it only
needs an object answering ``get``/``set``/``update`` (and optionally ``save``),
which keeps it importable from tests and from the audio thread.

Mode intent
    allround      standard parameters, no extra filtering (the app defaults)
    game          higher speech gate + gunshot/transient rejection + longer
                  minimum utterance (fewer NPC one-liners) and a lower CPU
                  profile so a busy game keeps its frame rate
    conversation  microphone first: both capture endpoints are mixed with the
                  speaker side attenuated, plus background-noise suppression
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("submirror.modes")

__all__ = [
    "MODE_ALLROUND",
    "MODE_GAME",
    "MODE_CONVERSATION",
    "MODE_ORDER",
    "MODE_DEFAULT",
    "MODE_LABELS",
    "MODE_LABEL_KEYS",
    "MODE_DESCRIPTION_KEYS",
    "SOURCE_SPEAKER",
    "SOURCE_MICROPHONE",
    "SOURCE_MIXED",
    "SOURCE_ORDER",
    "PRESETS",
    "ModePreset",
    "normalize_mode",
    "get_preset",
    "mode_params",
    "mode_keys",
    "preset_keys",
    "apply_mode",
    "mode_label",
    "mode_description",
    "mode_summary",
]

MODE_ALLROUND = "allround"
MODE_GAME = "game"
MODE_CONVERSATION = "conversation"

#: Panel order: the default first, then the two specialised modes.
MODE_ORDER: Tuple[str, ...] = (MODE_ALLROUND, MODE_GAME, MODE_CONVERSATION)
MODE_DEFAULT = MODE_ALLROUND

#: English fallbacks; the panel prefers the localized ``*_LABEL_KEYS`` entries.
MODE_LABELS: Dict[str, str] = {
    MODE_ALLROUND: "All-round",
    MODE_GAME: "Game",
    MODE_CONVERSATION: "Conversation",
}

MODE_LABEL_KEYS: Dict[str, str] = {
    MODE_ALLROUND: "settings.mode.allround",
    MODE_GAME: "settings.mode.game",
    MODE_CONVERSATION: "settings.mode.conversation",
}

MODE_DESCRIPTION_KEYS: Dict[str, str] = {
    MODE_ALLROUND: "settings.mode.allround.tip",
    MODE_GAME: "settings.mode.game.tip",
    MODE_CONVERSATION: "settings.mode.conversation.tip",
}

SOURCE_SPEAKER = "speaker"
SOURCE_MICROPHONE = "microphone"
SOURCE_MIXED = "mixed"

SOURCE_ORDER: Tuple[str, ...] = (SOURCE_SPEAKER, SOURCE_MICROPHONE, SOURCE_MIXED)

SOURCE_LABEL_KEYS: Dict[str, str] = {
    SOURCE_SPEAKER: "settings.audio.source.speaker",
    SOURCE_MICROPHONE: "settings.audio.source.microphone",
    SOURCE_MIXED: "settings.audio.source.mixed",
}


# --------------------------------------------------------------------------- #
# Preset table
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ModePreset:
    """One named bundle of dotted configuration values."""

    key: str
    label: str
    label_key: str = ""
    description_key: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)

    def keys(self) -> Tuple[str, ...]:
        """Dotted keys this preset writes, in a stable order."""
        return tuple(self.params.keys())

    def as_dict(self) -> Dict[str, Any]:
        """Copy of the preset parameters (the caller may freely mutate it)."""
        return dict(self.params)


def _allround_params() -> Dict[str, Any]:
    """The app's documented defaults - 全能模式 changes nothing."""
    return {
        # -- speech detection ------------------------------------------------
        # The gate and the minimum utterance live in the ``asr`` group: the
        # detector reads them from there since 0.1.0.
        "asr.vad_threshold_db": -45.0,
        "audio.vad_onset_ms": 120,
        "audio.vad_hangover_ms": 320,
        "asr.vad_min_speech_ms": 300,
        "audio.vad_chunk_seconds": 2.0,
        "audio.vad_chunk_max_seconds": 4.0,
        "audio.vad_engine": "auto",
        # -- capture ---------------------------------------------------------
        "audio.source": SOURCE_SPEAKER,
        "audio.mic_gain": 1.0,
        "audio.speaker_gain": 1.0,
        "audio.noise_suppression": False,
        "audio.burst_filter": False,
        "audio.feed_when_silent": True,
        # -- decoder ---------------------------------------------------------
        "asr.beam_size": 1,
        "asr.adaptive_beam": True,
        "asr.fast_beam_size": 1,
        "asr.preview_translate": True,
        # -- CPU / queues ----------------------------------------------------
        "perf.asr_queue_max": 3,
        "perf.translate_queue_max": 3,
        "perf.translate_workers": 1,
        "perf.pause_capture_on_full": True,
        "perf.overlay_max_fps": 30,
        "perf.overlay_min_interval_ms": 500,
        # -- OCR sampling ----------------------------------------------------
        "ocr.area_fps_normal": 5.0,
        "ocr.fullscreen_min_interval_ms": 800,
    }


def _game_params() -> Dict[str, Any]:
    """Fewer false triggers, cheaper pipeline, protected frame rate."""
    params = _allround_params()
    params.update({
        # A louder gate plus a longer minimum utterance drops short barks and
        # gunfire tails before a Whisper job is ever created.
        "asr.vad_threshold_db": -35.0,
        "audio.vad_onset_ms": 180,
        "audio.vad_hangover_ms": 260,
        "asr.vad_min_speech_ms": 450,
        # Longer, fewer slices: a burst of shots produces one job per 2.5 s
        # instead of one per second.
        "audio.vad_chunk_seconds": 2.5,
        "audio.vad_chunk_max_seconds": 5.0,
        # The energy gate costs a fraction of the Silero model and the burst
        # filter handles the impulses it would otherwise accept.
        "audio.vad_engine": "energy",
        "audio.burst_filter": True,
        "audio.burst_max_ms": 60.0,
        "audio.burst_margin_db": 10.0,
        "audio.feed_when_silent": False,
        # Greedy decode only: accuracy is secondary to a stable frame rate.
        "asr.beam_size": 1,
        "asr.preview_translate": False,
        # A shallower queue plus a slower overlay keeps the CPU headroom.
        "perf.asr_queue_max": 2,
        "perf.translate_queue_max": 2,
        "perf.overlay_max_fps": 15,
        "ocr.area_fps_normal": 1.0,
        "ocr.fullscreen_min_interval_ms": 1500,
    })
    return params


def _conversation_params() -> Dict[str, Any]:
    """Microphone first, speaker attenuated, background noise suppressed."""
    params = _allround_params()
    params.update({
        # Voices in a call are quieter and further from the microphone, so the
        # gate opens lower while the hangover grows to keep phrases together.
        "asr.vad_threshold_db": -48.0,
        "audio.vad_hangover_ms": 400,
        "audio.source": SOURCE_MIXED,
        "audio.mic_gain": 1.0,
        "audio.speaker_gain": 0.35,
        "audio.noise_suppression": True,
        "audio.noise_reduction_db": 12.0,
        # Better accuracy for a low-volume talk: beam 5, with the adaptive path
        # dropping back to greedy as soon as the speech speeds up.
        "asr.beam_size": 5,
    })
    return params


#: ``app.mode`` value -> preset.
PRESETS: Dict[str, ModePreset] = {
    MODE_ALLROUND: ModePreset(
        key=MODE_ALLROUND,
        label=MODE_LABELS[MODE_ALLROUND],
        label_key=MODE_LABEL_KEYS[MODE_ALLROUND],
        description_key=MODE_DESCRIPTION_KEYS[MODE_ALLROUND],
        params=_allround_params(),
    ),
    MODE_GAME: ModePreset(
        key=MODE_GAME,
        label=MODE_LABELS[MODE_GAME],
        label_key=MODE_LABEL_KEYS[MODE_GAME],
        description_key=MODE_DESCRIPTION_KEYS[MODE_GAME],
        params=_game_params(),
    ),
    MODE_CONVERSATION: ModePreset(
        key=MODE_CONVERSATION,
        label=MODE_LABELS[MODE_CONVERSATION],
        label_key=MODE_LABEL_KEYS[MODE_CONVERSATION],
        description_key=MODE_DESCRIPTION_KEYS[MODE_CONVERSATION],
        params=_conversation_params(),
    ),
}


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #

def normalize_mode(value: Any) -> str:
    """Return a known mode key; anything unknown falls back to all-round."""
    text = "" if value is None else str(value).strip().lower()
    if text in PRESETS:
        return text
    if text:
        logger.debug("unknown work mode %r, using %s", value, MODE_DEFAULT)
    return MODE_DEFAULT


def get_preset(mode: Any) -> ModePreset:
    """Preset for *mode* (never raises)."""
    return PRESETS[normalize_mode(mode)]


def mode_params(mode: Any) -> Dict[str, Any]:
    """Copy of every parameter a mode sets."""
    return get_preset(mode).as_dict()


def mode_keys(mode: Any) -> Tuple[str, ...]:
    """Dotted keys a mode sets."""
    return get_preset(mode).keys()


def preset_keys(*modes: Any) -> Tuple[str, ...]:
    """Union of the keys used by *modes* (defaults to every mode)."""
    chosen: Iterable[Any] = modes or MODE_ORDER
    seen: Dict[str, None] = {}
    for mode in chosen:
        for key in mode_keys(mode):
            seen.setdefault(key, None)
    return tuple(seen.keys())


def apply_mode(config: Any,
               mode: Any,
               *,
               keys: Optional[Sequence[str]] = None,
               save: bool = False) -> Dict[str, Any]:
    """
    Write a mode's parameters into *config*.

    Only values the schema accepts are applied; rejected keys are logged and
    skipped so a stale preset can never corrupt the configuration file.
    Returns the keys that were actually written.
    """
    preset = get_preset(mode)
    values = preset.as_dict()
    if keys is not None:
        wanted = set(keys)
        values = {key: value for key, value in values.items() if key in wanted}

    setter = getattr(config, "set", None)
    applied: Dict[str, Any] = {}
    for key, value in values.items():
        try:
            accepted = bool(setter(key, value)) if callable(setter) else False
        except Exception:
            logger.debug("mode %s could not set %s", preset.key, key, exc_info=True)
            accepted = False
        if accepted:
            applied[key] = value
        else:
            logger.debug("mode %s: %s was rejected by the schema", preset.key, key)

    if save:
        save_fn = getattr(config, "save", None)
        if callable(save_fn):
            try:
                save_fn()
            except Exception:
                logger.debug("could not persist the mode", exc_info=True)

    logger.info("work mode %s applied %d/%d parameter(s)",
                preset.key, len(applied), len(values))
    return applied


# --------------------------------------------------------------------------- #
# Localized text
# --------------------------------------------------------------------------- #

def _translate(config: Any, key: str, fallback: str) -> str:
    """Look a key up through ``config.t`` when it exists."""
    getter = getattr(config, "t", None)
    if callable(getter):
        try:
            text = getter(key)
        except Exception:
            text = None
        if text and text != key:
            return str(text)
    return fallback


def mode_label(config: Any, mode: Any) -> str:
    """Localized display name of a mode."""
    preset = get_preset(mode)
    return _translate(config, preset.label_key, preset.label)


def mode_description(config: Any, mode: Any) -> str:
    """Localized one-line explanation of what a mode changes."""
    preset = get_preset(mode)
    return _translate(config, preset.description_key, preset.label)


def _number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return ("%.1f" % number).rstrip("0").rstrip(".")


def mode_summary(config: Any, mode: Any) -> str:
    """
    Compact, language-neutral list of the numbers a mode applies.

    The audio-source token is localized because it is the part a user reads
    most often when switching between game and conversation modes.
    """
    params = mode_params(mode)
    tokens = []
    gate = params.get("asr.vad_threshold_db", params.get("audio.vad_threshold_db"))
    if gate is not None:
        tokens.append("VAD %s dB" % _number(gate))
    minimum = params.get("asr.vad_min_speech_ms", params.get("audio.vad_min_speech_ms"))
    if minimum is not None:
        tokens.append("min %s ms" % _number(minimum))
    if "audio.vad_chunk_seconds" in params:
        tokens.append("slice %s s" % _number(params["audio.vad_chunk_seconds"]))
    source = str(params.get("audio.source", SOURCE_SPEAKER))
    tokens.append(_translate(config, SOURCE_LABEL_KEYS.get(source, ""),
                             SOURCE_LABEL_KEYS.get(source, source).split(".")[-1]))
    gains = params.get("audio.speaker_gain")
    if source == SOURCE_MIXED and gains is not None:
        tokens.append("loopback x%s" % _number(gains))
    if params.get("audio.burst_filter"):
        tokens.append("gunshot filter")
    if params.get("audio.noise_suppression"):
        tokens.append("denoise")
    queues = params.get("perf.asr_queue_max")
    if queues is not None:
        tokens.append("queue %s" % _number(queues))
    fps = params.get("perf.overlay_max_fps")
    if fps is not None:
        tokens.append("%s FPS" % _number(fps))
    return " · ".join(str(token) for token in tokens if token)
