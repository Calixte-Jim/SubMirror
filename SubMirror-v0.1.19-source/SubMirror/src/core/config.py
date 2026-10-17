# -*- coding: utf-8 -*-
"""
SubMirror v0.1 - Configuration manager.

Responsibilities
    * Persist user settings as JSON (installed build:
      %APPDATA%\\SubMirror\\config.json).
    * Reserve a portable-build extension point (disabled in v0.1).
    * Validate every value by type / range; illegal values fall back to defaults.
    * Recover from a missing or corrupted config file without crashing.
    * Provide the hard-coded bilingual UI dictionary (zh-CN / en) used by v0.1.
      The dictionary only affects the application UI. ASR / OCR subtitle text and
      the translation language pair are completely independent of ui_language.

Safety: user-mode only. This module performs plain file I/O and never touches
game processes, memory, hooks, drivers or simulated input.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ConfigManager",
    "lang_data",
    "get_config",
    "init_config",
    "reset_singleton",
    "normalize_hotkey",
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "LOAD_OK",
    "LOAD_DEFAULT",
    "LOAD_PARTIAL",
    "LOAD_CORRUPT",
    "SINGLE_QUEUE_MAX",
    "MAX_SUBTITLE_CHARS",
    "MIN_VALID_TEXT_CHARS",
]

# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #

APP_NAME = "SubMirror"
APP_VERSION = "0.1.19"

SUPPORTED_LANGUAGES: Tuple[str, ...] = ("zh-CN", "en")
DEFAULT_LANGUAGE = "zh-CN"

# Load status codes reported to the UI layer.
LOAD_OK = "ok"            # file existed and every value was valid
LOAD_DEFAULT = "default"  # file did not exist, defaults were used
LOAD_PARTIAL = "partial"  # some values were illegal, they fell back to defaults
LOAD_CORRUPT = "corrupt"  # file unreadable / not a JSON object, full reset

# Hard limits shared with the other modules.
SINGLE_QUEUE_MAX = 2
MAX_SUBTITLE_CHARS = 200
MIN_VALID_TEXT_CHARS = 3

# Portable mode is intentionally NOT shipped in v0.1. The resolver below already
# understands the portable layout, flipping this flag is the only change needed.
PORTABLE_MODE_ENABLED = False
PORTABLE_MARKER_NAME = "SubMirror.portable"

_APPDATA_FOLDER = "%APPDATA%\\SubMirror\\config.json"
_PORTABLE_FOLDER = "config\\config.json"
_CONFIG_FILE_NAME = "config.json"

# --------------------------------------------------------------------------- #
# Default configuration + validation schema
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": APP_VERSION,
    "ui_language": DEFAULT_LANGUAGE,
    "disclaimer_accepted": False,
    "fps_safe_mode": False,
    "hotkeys": {
        "area_ocr": "Ctrl+Alt+A",
        "fullscreen_ocr": "Ctrl+Alt+S",
        # Optional: pause/resume the speech pipeline. Empty means "not bound",
        # which is the shipped default, so an upgrade never grabs a new key.
        "toggle_asr": "",
        # Start/stop the fixed-region OCR loop (v0.1.13). With no region saved
        # yet the same key arms the pick window instead, so one key covers
        # "draw the box" and "run the loop" (v0.1.15).  Alt+C is the shipped
        # suggestion; an existing config.json keeps whatever the user recorded.
        "toggle_region_ocr": "Alt+C",
    },
    "ocr": {
        # Sampling rate used while the area-OCR hotkey is held down.
        "area_fps_safe": 0.5,       # locked rate when fps_safe_mode is on
        "area_fps_normal": 5.0,     # rate when fps_safe_mode is off
        "fullscreen_min_interval_ms": 800,  # hard de-bounce for the single shot
        "frame_queue_max": 1,       # keep only the newest pending frame
        # WinRT OCR (Windows.Media.Ocr) is the only v0.1 engine, so the only
        # engine-level settings are the preferred language and that choice.
        "winrt_language": "",       # preferred BCP-47 tag, empty = auto
        "winrt_language_auto": True,  # pick any installed OCR language
        "picker_mask_opacity": 0.35,  # dim strength of the region-picker bands
        "min_region_size": 8,       # drags smaller than this count as no selection
        # Fixed-region OCR (v0.1.13): one saved "left,top,width,height"
        # rectangle. Nothing is captured and nothing is recognised until the
        # user turns the loop on, and the region is only updated by an explicit
        # re-pick.
        "fixed_region": "",
        "fixed_region_enabled": False,
        "region_change_tolerance": 8,  # luminance delta that counts as a change
        "timeout_seconds": 20.0,    # ceiling for one WinRT recognise() call
        # Line filtering (BL-3, option B). Windows OCR cannot score its lines,
        # so the height/character pair does the real work; the confidence
        # threshold is honoured for engines that do report a score. 0 = off,
        # and the best line is always kept whatever the thresholds say.
        "confidence_min": 0.0,      # 0.0-1.0, only scored lines are tested
        "min_line_height": 0.0,     # px; shorter lines are dropped
        "min_line_chars": 0,        # characters; shorter lines are dropped
    },
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        "device": "",                   # loopback selector, empty = system default
        "ring_buffer_seconds": 30,      # hard cap of the capture ring buffer
        "block_ms": 30,                 # requested read block from the backend
        "reconnect_attempts": 5,        # backend re-open attempts after a failure
        "reconnect_interval_ms": 2000,
        "silence_log_seconds": 30,      # log "long silence" hint once per period
        "silence_dbfs": -50.0,          # level below this counts as silence
        "spike_filter": True,           # enable the short-transient filter
        "spike_filter_ms": 10,          # drop transients shorter than this
        "spike_margin_db": 12.0,        # how far above the baseline counts as a spike
        "feed_when_silent": True,       # keep feeding the VAD during silence
        # Voice-activity tuning. These describe the detector itself, so the
        # values are read straight from this group (see audio/vad.py).
        "vad_margin_db": 9.0,           # required rise above the noise floor
        "vad_onset_ms": 120,            # speech must persist this long to start
        # 400ms of trailing silence was one of the reasons a line reached the
        # overlay late: the segment only closed once the speaker paused.
        "vad_hangover_ms": 320,         # silence needed before a segment ends
        # A long monologue is sliced instead of being held for one big
        # recognition call, which keeps the wait for the first line short.
        "vad_max_segment_seconds": 6.0,
        # The energy detector has no speech model, so it slices on a timer
        # alone: a longer slice means fewer recognition tasks per minute and
        # less queue pressure while the engine is falling back to energy.
        "vad_energy_max_segment_seconds": 9.0,
        "vad_preroll_ms": 200,          # audio kept from before the onset
        "vad_adaptive": True,           # track the noise floor over time
        "vad_overlap_ms": 400,          # tail replayed into the next slice
        # Short-chunk slicing (v0.1.18). A monologue is cut into ~2s chunks
        # instead of one long call, and the 0.4s tail above is replayed into
        # the next chunk so a word never falls into the seam. When the queue
        # stays overloaded the chunk length is stretched towards the ceiling
        # below, which lowers the task rate instead of piling work up.
        "vad_chunk_seconds": 2.0,
        "vad_chunk_max_seconds": 4.0,
        # -- voice detection backend --
        # "auto" prefers Silero (ONNX) and silently falls back to the built-in
        # energy detector when onnxruntime or the model file is missing.
        "vad_engine": "auto",           # auto | silero | energy
        "vad_silero_path": "",          # explicit silero_vad.onnx, empty = search
        "vad_silero_threshold": 0.5,    # speech probability that opens the gate
        "vad_silero_negative_margin": 0.15,  # hysteresis below the open threshold
        "vad_silero_min_speech_ms": 250,     # shorter blips are discarded
        # -- capture source and mixing (v0.1.19) --
        # "speaker" follows the system output (the loopback capture used since
        # 0.1.0), "microphone" records the default input endpoint, and "mixed"
        # runs both and sums them: the conversation mode uses "mixed" with the
        # speaker side attenuated so the microphone dominates the mix.
        "source": "speaker",            # speaker | microphone | mixed
        "mic_device": "",               # index or name substring, "" = default input
        "mic_gain": 1.0,                # microphone weight in the mix
        "speaker_gain": 1.0,            # loopback weight in the mix
        "mix_grace_ms": 400,            # a silent source is bypassed after this long
        # -- background-noise suppression (v0.1.19, conversation mode) --
        # A spectral gate driven by a running noise-floor estimate. It applies
        # to the microphone/mixed path, where a room tone would otherwise hold
        # the VAD open between sentences.
        "noise_suppression": False,
        "noise_reduction_db": 12.0,     # oversubtraction above the noise floor
        "noise_floor_adapt_s": 1.0,     # seconds of history for the floor estimate
        # -- transient (gunshot) rejection (v0.1.19, game mode) --
        # Distinct from spike_filter: gunshots last tens of milliseconds, so the
        # burst gate mutes blocks whose energy jumps this far above the running
        # median for at most burst_max_ms.
        "burst_filter": False,
        "burst_max_ms": 60.0,
        "burst_margin_db": 10.0,
    },
    "asr": {
        "model_path": "",
        "model_size": "",               # "" | tiny | base | small | medium
        "language": "auto",             # auto | en | zh
        # Game dialogue is English; pinning the language stops Whisper from
        # spending a detection pass (and from answering in another language).
        "force_english": True,
        "device": "cpu",                # cpu | cuda, handed to CTranslate2
        "compute_type": "int8",         # CTranslate2 compute type
        "vad_mode": "single",           # single | fps
        # Kept at 300 to match the detector's long-standing effective default:
        # the panel used to show 200 while audio/vad.py actually ran with 300.
        "vad_min_speech_ms": 300,       # shorter bursts are never transcribed
        "vad_threshold_db": -45.0,      # absolute speech gate in dBFS
        "beam_size": 1,                 # 1 = greedy, the fastest decode
        # -- real-time guards (v0.1.18) --
        # A fast speaker produces more words per second than the decoder can
        # keep up with; dropping to greedy keeps the backlog from growing.
        "adaptive_beam": True,
        "fast_speech_wps": 3.0,         # words/second that counts as "fast"
        "fast_beam_size": 1,            # beam used while the speech is fast
        # A recognised paragraph is split on . ! ? so every short sentence is
        # translated on its own; long text is never submitted in one piece.
        "sentence_split": True,
        "max_sentences": 4,             # sentences translated per utterance
        # -- incremental output (lower end-to-end latency) --
        "streaming": True,              # preview the line while it is spoken
        "partial_interval_ms": 1100,    # spacing between two preview passes
        "partial_min_seconds": 0.6,     # audio needed before a preview is useful
        "partial_max_seconds": 8.0,     # preview audio is capped at this length
        # -- decode tuning (faster-whisper / CTranslate2) --
        "best_of": 1,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "no_speech_threshold": 0.5,
        "compression_ratio_threshold": 2.2,
        "log_prob_threshold": -1.0,
        "initial_prompt": "Video game dialogue.",
        # Translation runs on its own lane so a slow endpoint no longer
        # serialises recognition of the following segment.
        "translate_in_lane": True,
        # An incremental preview line is translated BEFORE it is drawn, so the
        # overlay never shows raw English while the translation is in flight.
        "preview_translate": True,
        "min_text_chars": MIN_VALID_TEXT_CHARS,
        "max_subtitle_chars": MAX_SUBTITLE_CHARS,
    },
    "translate": {
        "source_lang": "auto",
        "target_lang": "zh-CN",
        "timeout_seconds": 3,
        "failure_threshold": 3,     # consecutive failures before the breaker opens
        "circuit_break_seconds": 10,
        "show_original": False,     # show original + translation on the ASR line
        # -- retry budget (per translate() call) --
        "max_retries": 2,               # total attempts, so 1 retry by default
        "retry_delay_seconds": 0.25,    # first backoff step
        "retry_max_delay_seconds": 1.0,  # backoff ceiling
        "total_budget_seconds": 6.5,    # hard ceiling for one call, retries included
        "min_interval_ms": 0.0,         # minimum spacing between outbound calls
        "min_request_chars": 1,         # text shorter than this is never sent
        # -- in-memory cache, written to memory only --
        "cache_ttl_seconds": 300.0,
        "cache_max_entries": 256,
        # -- endpoint selection --
        "endpoints": [],                # empty = built-in Google/MyMemory/Libre order
        "prefer_requests": True,        # use requests/keep-alive when installed
        "http_user_agent": "",          # empty = built-in browser User-Agent
        "libre_base_url": "",           # empty = shared public LibreTranslate host
        "libre_dir": "",                # folder holding start_libretranslate.bat
        "libre_autostart": False,       # logon entry: silent Libre, then SubMirror
        "mymemory_email": "",           # optional, raises the free daily quota
        # -- style pre-prompt (item 1) --
        # The prompt is handed to every backend that advertises prompt support;
        # the keyless public endpoints cannot honour it and ignore it, which is
        # why the shorthand dictionary does the visible work for them.
        "prompt_enabled": True,
        "prompt": ("输入是 FPS 游戏玩家的实时语音：短句多、缩写多、口语化。"
                   "请翻译成自然通顺的中文，不要生硬直译；保留人名与游戏术语。"),
        # -- per-task deadline and light context (v0.1.18, tightened in 0.1.19) --
        # One translate task may occupy the lane for this long at most; the
        # call is abandoned afterwards so a slow endpoint cannot stall the
        # subtitle flow. 0.1.19 lowered it from 700 ms to 600 ms because the
        # lane is single-threaded: the deadline is also its worst-case slot.
        "task_timeout_ms": 600,
        # Only the last few short sentences travel with a request, and their
        # total size is capped, so a long paragraph can never be submitted at
        # once and the translation cannot drift.
        "context_sentences": 2,
        "context_max_chars": 240,
    },
    "text": {
        # Shorthand/abbreviation dictionary applied to ASR English before it is
        # translated ("gg" -> "打得好"), which stops the endpoint from guessing.
        "slang_enabled": True,
        "slang_path": "assets/slang_dict.json",
        # Drop ASR garbage (whistle/♪/repeated symbols/known hallucinations)
        # before it reaches the translator and the overlay.
        "asr_noise_filter": True,
        # -- sentence shaping (v0.1.18) --
        # Filler sounds ("um", "ah", ...) carry no meaning and are removed; the
        # text is then split on . ! ? so each short sentence is one subtitle.
        "filler_filter": True,
        "filler_words": "um,uh,ah,er,erm,hmm,mm,mmm,uhm,ahh,eh,ha",
        "max_sentence_chars": 200,
    },
    "subtitle": {
        "font_family": "Microsoft YaHei",
        "font_size": 22,
        "text_opacity": 1.0,
        "mask_opacity": 0.35,
        "queue_max": SINGLE_QUEUE_MAX,
        "timeout_seconds": 8,
        "show_tts_line": False,     # TTS line is a v0.1 UI placeholder only
        "show_original": False,     # keep the original text and the translation
        "max_lines_per_channel": 2,
        # When on, the base font size follows the panel size (the width/height
        # sliders resize the text as well as the box).
        "font_scale_with_panel": False,
        # -- fade in/out (0 disables the animation) --
        "fade_in_ms": 180,          # a new line fades in over this long
        "fade_out_ms": 220,         # a cleared/expired line fades out
        "fade_slide_px": 6,         # the line also drifts up by this much
    },
    "overlay": {
        "always_on_top": True,
        "click_through": True,
        "lock_position": False,     # ignore drag gestures, keep x/y fixed
        "x": -1,                    # -1 means "let Qt center the window"
        "y": -1,
        "width": 900,
        "height": 220,
        "text_color": "#FFFFFF",
        "mask_color": "#000000",
        "outline_color": "#101010",
        "outline": True,            # draw an outline behind the glyphs
        "outline_width": 2,         # outline thickness in pixels (0 = none)
        "size_preset": "default",   # default | compact | square (panel size)
        "corner_radius": 14,
        "padding": 18,
        "line_spacing": 4,
        "notice_seconds": 6.0,      # how long a notice banner stays on screen
        "show_status": True,        # status dot in the corner
        "show_status_text": False,  # status dot plus its text label
        "status_follows_translate": False,  # translate errors drive the dot
    },
    # -- performance guards --
    # Every lane that carries audio has a bounded queue: when it is full the
    # OLDEST pending item is discarded (task_scheduler drops instead of
    # blocking), so a slow recognition pass can never pile up into a timestamp
    # jump. 0 means "unbounded" and is not recommended for the audio lanes.
    "perf": {
        "audio_queue_max": 1,
        "vad_queue_max": 4,
        # The ASR lane accepts a small backlog and then discards the oldest
        # pending chunk, which bounds the delay of the newest line (v0.1.18).
        # 0.1.19 lowered the ceiling from 4 to 3 and adds the capture pause
        # below: three pending chunks is the point where the newest subtitle
        # would otherwise start lagging the audio by several seconds.
        "asr_queue_max": 3,
        "ocr_queue_max": 1,
        "translate_queue_max": 3,
        # Translation is single-threaded on purpose: one request at a time keeps
        # the endpoint from being hammered and the responses in order.
        "translate_workers": 1,
        # 0.1.19 spec item 3: when the ASR lane is full the capture pipeline
        # stops publishing frames, so the backlog drains instead of growing.
        # The stream itself keeps running (the ring buffer stays warm) and
        # resumes within a couple of chunks.
        "pause_capture_on_full": True,
        # Upper bound for overlay repaints: the fade animation ticks at
        # 1000 / max_fps at most, and a frame with nothing new to show is
        # skipped instead of being repainted.
        "overlay_max_fps": 30,
        # Hard floor for two consecutive subtitle repaints (0.1.19 spec item 5
        # raised it from 400 ms to 500 ms): the overlay is never asked to redraw
        # more often than this, and the paint itself only runs on the GUI thread.
        "overlay_min_interval_ms": 500,
        # While a fullscreen foreground window is on screen (i.e. a game), the
        # fade animation is slowed down by this factor.
        "overlay_fullscreen_slowdown": True,
        "overlay_fullscreen_scale": 0.5,
    },
    "app": {
        # Logon auto-start of the application itself. Off by default: the
        # installer neither writes nor assumes a Startup entry, and the
        # advanced settings page is the single owner of this switch.
        "autostart": False,
        # -- work mode (v0.1.19) --
        # One of modes.MODE_ORDER. The panel loads the matching preset into the
        # form; "mode_auto_params" additionally re-applies it at every start,
        # which is what keeps a mode's intent after the config was hand-edited.
        "mode": "allround",             # allround | game | conversation
        "mode_auto_params": True,
    },
    "logging": {
        # The console is quiet by default: only warnings and errors are printed,
        # which keeps a busy session from flooding the terminal (and the
        # redirected log file) with debug traffic.
        "level": "warning",         # debug | info | warning | error
    },
}

# Schema entries:
#   dict           -> nested group (recursed)
#   (kind, rule)   -> leaf value
# kinds: any | str | int | float | bool | enum | hotkey | color
_SCHEMA: Dict[str, Any] = {
    "version": ("str", None),
    "ui_language": ("enum", SUPPORTED_LANGUAGES),
    "disclaimer_accepted": ("bool", None),
    "fps_safe_mode": ("bool", None),
    "hotkeys": {
        "area_ocr": ("hotkey", None),
        "fullscreen_ocr": ("hotkey", None),
        "toggle_asr": ("hotkey", None),
        "toggle_region_ocr": ("hotkey", None),
    },
    "ocr": {
        "area_fps_safe": ("float", (0.05, 5.0)),
        "area_fps_normal": ("float", (0.1, 30.0)),
        "fullscreen_min_interval_ms": ("int", (800, 60000)),
        "frame_queue_max": ("int", (1, 1)),
        "winrt_language": ("str", (0, 32)),
        "winrt_language_auto": ("bool", None),
        "picker_mask_opacity": ("float", (0.0, 0.95)),
        "min_region_size": ("int", (2, 64)),
        "fixed_region": ("str", (0, 64)),
        "fixed_region_enabled": ("bool", None),
        "region_change_tolerance": ("int", (1, 64)),
        "timeout_seconds": ("float", (1.0, 120.0)),
        "confidence_min": ("float", (0.0, 1.0)),
        "min_line_height": ("float", (0.0, 500.0)),
        "min_line_chars": ("int", (0, 500)),
    },
    "audio": {
        "sample_rate": ("int", (8000, 48000)),
        "channels": ("int", (1, 2)),
        "ring_buffer_seconds": ("int", (1, 600)),
        "device": ("str", (0, 128)),
        "block_ms": ("int", (10, 200)),
        "reconnect_attempts": ("int", (0, 50)),
        "reconnect_interval_ms": ("int", (200, 60000)),
        "silence_log_seconds": ("int", (0, 3600)),
        "silence_dbfs": ("float", (-80.0, -20.0)),
        "spike_filter": ("bool", None),
        "spike_filter_ms": ("int", (0, 100)),
        "spike_margin_db": ("float", (6.0, 40.0)),
        "feed_when_silent": ("bool", None),
        "vad_margin_db": ("float", (0.0, 40.0)),
        "vad_onset_ms": ("int", (0, 2000)),
        "vad_hangover_ms": ("int", (0, 5000)),
        "vad_max_segment_seconds": ("float", (1.0, 60.0)),
        "vad_energy_max_segment_seconds": ("float", (1.0, 60.0)),
        "vad_preroll_ms": ("int", (0, 2000)),
        "vad_adaptive": ("bool", None),
        "vad_overlap_ms": ("int", (0, 1000)),
        "vad_chunk_seconds": ("float", (1.0, 10.0)),
        "vad_chunk_max_seconds": ("float", (1.0, 30.0)),
        "vad_engine": ("enum", ("auto", "silero", "energy")),
        "vad_silero_path": ("path", None),
        "vad_silero_threshold": ("float", (0.05, 0.95)),
        "vad_silero_negative_margin": ("float", (0.0, 0.5)),
        "vad_silero_min_speech_ms": ("int", (0, 5000)),
        # -- capture source / mixing / DSP stages (v0.1.19) --
        "source": ("enum", ("speaker", "microphone", "mixed")),
        "mic_device": ("str", (0, 120)),
        "mic_gain": ("float", (0.0, 4.0)),
        "speaker_gain": ("float", (0.0, 4.0)),
        "mix_grace_ms": ("int", (50, 5000)),
        "noise_suppression": ("bool", None),
        "noise_reduction_db": ("float", (0.0, 30.0)),
        "noise_floor_adapt_s": ("float", (0.2, 5.0)),
        "burst_filter": ("bool", None),
        "burst_max_ms": ("float", (10.0, 200.0)),
        "burst_margin_db": ("float", (3.0, 30.0)),
    },
    "asr": {
        "model_path": ("path", None),
        "model_size": ("enum", ("", "tiny", "base", "small", "medium")),
        "language": ("enum", ("auto", "en", "zh")),
        "force_english": ("bool", None),
        # Left as free strings on purpose: CTranslate2 accepts values such as
        # "cuda:0" or "int8_float16" that a closed enum would wrongly reject.
        "device": ("str", (0, 32)),
        "compute_type": ("str", (0, 32)),
        "vad_mode": ("enum", ("single", "fps")),
        "vad_min_speech_ms": ("int", (50, 5000)),
        "vad_threshold_db": ("float", (-90.0, 0.0)),
        "beam_size": ("int", (1, 10)),
        "adaptive_beam": ("bool", None),
        "fast_speech_wps": ("float", (0.5, 12.0)),
        "fast_beam_size": ("int", (1, 10)),
        "sentence_split": ("bool", None),
        "max_sentences": ("int", (1, 12)),
        "streaming": ("bool", None),
        "partial_interval_ms": ("int", (200, 5000)),
        "partial_min_seconds": ("float", (0.0, 5.0)),
        "partial_max_seconds": ("float", (1.0, 30.0)),
        "best_of": ("int", (1, 10)),
        "temperature": ("float", (0.0, 1.0)),
        "condition_on_previous_text": ("bool", None),
        "without_timestamps": ("bool", None),
        "no_speech_threshold": ("float", (0.0, 1.0)),
        "compression_ratio_threshold": ("float", (1.0, 10.0)),
        "log_prob_threshold": ("float", (-5.0, 0.0)),
        "initial_prompt": ("str", (0, 200)),
        "translate_in_lane": ("bool", None),
        "preview_translate": ("bool", None),
        "min_text_chars": ("int", (1, 20)),
        "max_subtitle_chars": ("int", (20, 1000)),
    },
    "translate": {
        "source_lang": ("enum", ("auto", "en", "zh")),
        "target_lang": ("enum", ("zh-CN", "en")),
        "timeout_seconds": ("float", (0.5, 30.0)),
        "failure_threshold": ("int", (1, 20)),
        "circuit_break_seconds": ("float", (1.0, 300.0)),
        "show_original": ("bool", None),
        "max_retries": ("int", (1, 5)),
        "retry_delay_seconds": ("float", (0.0, 10.0)),
        "retry_max_delay_seconds": ("float", (0.0, 30.0)),
        "total_budget_seconds": ("float", (1.0, 60.0)),
        "min_interval_ms": ("float", (0.0, 60000.0)),
        "min_request_chars": ("int", (1, 64)),
        "cache_ttl_seconds": ("float", (0.0, 3600.0)),
        "cache_max_entries": ("int", (0, 4096)),
        # "any" because the value is either a list of endpoint names or empty.
        "endpoints": ("any", None),
        "prefer_requests": ("bool", None),
        "http_user_agent": ("str", (0, 256)),
        "libre_base_url": ("str", (0, 256)),
        "libre_dir": ("path", None),
        "libre_autostart": ("bool", None),
        "mymemory_email": ("str", (0, 128)),
        "prompt_enabled": ("bool", None),
        "prompt": ("str", (0, 512)),
        "task_timeout_ms": ("int", (50, 5000)),
        "context_sentences": ("int", (0, 8)),
        "context_max_chars": ("int", (0, 2000)),
    },
    "text": {
        "slang_enabled": ("bool", None),
        "slang_path": ("path", None),
        "asr_noise_filter": ("bool", None),
        "filler_filter": ("bool", None),
        "filler_words": ("str", (0, 256)),
        "max_sentence_chars": ("int", (20, 500)),
    },
    "subtitle": {
        "font_family": ("str", (1, 128)),
        "font_size": ("int", (8, 96)),
        "text_opacity": ("float", (0.0, 1.0)),
        "mask_opacity": ("float", (0.0, 1.0)),
        "queue_max": ("int", (1, SINGLE_QUEUE_MAX)),
        "timeout_seconds": ("float", (1.0, 60.0)),
        "show_tts_line": ("bool", None),
        "show_original": ("bool", None),
        "max_lines_per_channel": ("int", (1, 4)),
        "font_scale_with_panel": ("bool", None),
        "fade_in_ms": ("int", (0, 2000)),
        "fade_out_ms": ("int", (0, 2000)),
        "fade_slide_px": ("int", (0, 64)),
    },
    "overlay": {
        "always_on_top": ("bool", None),
        "click_through": ("bool", None),
        "lock_position": ("bool", None),
        "x": ("int", (-100000, 100000)),
        "y": ("int", (-100000, 100000)),
        "width": ("int", (200, 8000)),
        "height": ("int", (60, 4000)),
        "text_color": ("color", None),
        "mask_color": ("color", None),
        "outline_color": ("color", None),
        "outline": ("bool", None),
        "outline_width": ("int", (0, 8)),
        "size_preset": ("enum", ("default", "compact", "square")),
        "corner_radius": ("int", (0, 48)),
        "padding": ("int", (4, 64)),
        "line_spacing": ("int", (0, 32)),
        "notice_seconds": ("float", (1.0, 60.0)),
        "show_status": ("bool", None),
        "show_status_text": ("bool", None),
        "status_follows_translate": ("bool", None),
    },
    "perf": {
        "audio_queue_max": ("int", (0, 64)),
        "vad_queue_max": ("int", (0, 64)),
        "asr_queue_max": ("int", (0, 64)),
        "ocr_queue_max": ("int", (0, 64)),
        "translate_queue_max": ("int", (0, 64)),
        "translate_workers": ("int", (1, 8)),
        "pause_capture_on_full": ("bool", None),
        "overlay_max_fps": ("int", (1, 240)),
        "overlay_min_interval_ms": ("int", (0, 5000)),
        "overlay_fullscreen_slowdown": ("bool", None),
        "overlay_fullscreen_scale": ("float", (0.1, 1.0)),
    },
    "app": {
        "autostart": ("bool", None),
        "mode": ("enum", ("allround", "game", "conversation")),
        "mode_auto_params": ("bool", None),
    },
    "logging": {
        "level": ("enum", ("debug", "info", "warning", "error")),
    },
}

_MISSING = object()

# --------------------------------------------------------------------------- #
# Hotkey helpers
# --------------------------------------------------------------------------- #

_MODIFIER_ALIASES = {
    "CTRL": "Ctrl",
    "CONTROL": "Ctrl",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "WIN": "Win",
    "META": "Win",
    "SUPER": "Win",
    "CMD": "Win",
    "WINDOWS": "Win",
}

_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")

_NAMED_KEYS = {
    "SPACE", "TAB", "ESC", "ESCAPE", "ENTER", "RETURN", "BACKSPACE", "DELETE",
    "INSERT", "HOME", "END", "PAGEUP", "PAGEDOWN", "UP", "DOWN", "LEFT",
    "RIGHT", "CAPSLOCK", "PRINTSCREEN", "PAUSE", "NUMLOCK", "SCROLLLOCK",
    "MINUS", "PLUS", "EQUAL", "COMMA", "PERIOD", "SLASH", "SEMICOLON",
    "QUOTE", "BACKQUOTE", "LBRACKET", "RBRACKET", "BACKSLASH",
}


def _canonical_key(token: str) -> Optional[str]:
    """Return the canonical name of a single non-modifier key, else None."""
    upper = token.upper()
    if len(upper) == 1 and (upper.isalpha() or upper.isdigit()):
        return upper
    if upper.startswith("F") and upper[1:].isdigit():
        if 1 <= int(upper[1:]) <= 24:
            return "F" + str(int(upper[1:]))
    if upper in _NAMED_KEYS:
        return upper
    return None


def normalize_hotkey(text: Any) -> Optional[str]:
    """
    Normalize a hotkey string to canonical form, e.g. "ctrl+alt+a" -> "Ctrl+Alt+A".

    Returns None when the string is not a usable global hotkey. At least one
    modifier plus exactly one normal key is required, which keeps accidental
    single-key global grabs from blocking the desktop.
    """
    if not isinstance(text, str):
        return None
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    modifiers, keys = [], []
    for part in parts:
        alias = _MODIFIER_ALIASES.get(part.upper())
        if alias is not None:
            if alias not in modifiers:
                modifiers.append(alias)
            continue
        key = _canonical_key(part)
        if key is None:
            return None
        if key not in keys:
            keys.append(key)
    if not modifiers or len(keys) != 1:
        return None
    modifiers.sort(key=_MODIFIER_ORDER.index)
    return "+".join(modifiers + keys)


# --------------------------------------------------------------------------- #
# Value coercion
# --------------------------------------------------------------------------- #

def _as_bool(value: Any) -> Tuple[bool, bool]:
    if isinstance(value, bool):
        return value, True
    if isinstance(value, int) and value in (0, 1):
        return bool(value), True
    return False, False


def _as_number(value: Any, integral: bool) -> Tuple[Optional[float], bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None, False
    else:
        return None, False
    if number != number or number in (float("inf"), float("-inf")):
        return None, False
    if integral and float(number).is_integer():
        return int(number), True
    if integral:
        return None, False
    return number, True


def _expand_path(value: str) -> str:
    expanded = os.path.expandvars(value.strip())
    expanded = os.path.expanduser(expanded)
    return expanded


def _coerce(default: Any, value: Any, kind: str, rule: Any) -> Tuple[Any, bool, str]:
    """Return (coerced_value, ok, reason). Default is used whenever ok is False."""
    if kind == "any":
        return value, True, ""

    if kind == "bool":
        result, ok = _as_bool(value)
        return (result, True, "") if ok else (default, False, "expected boolean")

    if kind in ("int", "float"):
        result, ok = _as_number(value, integral=(kind == "int"))
        if not ok:
            return default, False, "expected %s" % kind
        if rule:
            low, high = rule
            clamped = min(max(result, low), high)
            if clamped != result:
                return clamped, False, "out of range [%s, %s]" % (low, high)
            return result, True, ""
        return result, True, ""

    if kind == "str":
        if not isinstance(value, str):
            return default, False, "expected string"
        text = value
        if rule:
            max_len = rule[1]
            if len(text) > max_len:
                return text[:max_len], False, "string too long (max %d)" % max_len
        return text, True, ""

    if kind == "path":
        if not isinstance(value, str):
            return default, False, "expected path string"
        return _expand_path(value), True, ""

    if kind == "enum":
        allowed = rule or ()
        if isinstance(value, str):
            for candidate in allowed:
                if value == candidate:
                    return value, True, ""
            lowered = value.strip().lower()
            for candidate in allowed:
                if lowered == str(candidate).lower():
                    return candidate, True, ""
        return default, False, "not one of %s" % (list(allowed),)

    if kind == "hotkey":
        # An empty value is a valid "not bound" state, not a malformed hotkey:
        # the ASR on/off hotkey ships unbound, and a user who clears a field in
        # the settings panel must not be greeted by a "config was reset" dialog.
        if value is None or (isinstance(value, str) and not value.strip()):
            return "", True, ""
        normalized = normalize_hotkey(value)
        if normalized is None:
            return default, False, "invalid hotkey format"
        return normalized, True, ""

    if kind == "color":
        if isinstance(value, str) and value.startswith("#") and len(value) in (7, 9):
            return value, True, ""
        return default, False, "expected #RRGGBB color"

    return value, True, ""


def _merge_validate(
    defaults: Dict[str, Any],
    data: Dict[str, Any],
    schema: Dict[str, Any],
    path: str,
    issues: List[Tuple[str, str]],
) -> Dict[str, Any]:
    """Recursively validate *data* against *schema*, filling defaults on error."""
    result: Dict[str, Any] = {}

    for key, spec in schema.items():
        node_path = ("%s.%s" % (path, key)) if path else key
        default = defaults.get(key, _MISSING)

        if isinstance(spec, dict):
            sub_default = default if isinstance(default, dict) else {}
            raw = data.get(key, _MISSING)
            if raw is _MISSING:
                result[key] = copy.deepcopy(sub_default)
                continue
            if not isinstance(raw, dict):
                issues.append((node_path, "expected object, defaults applied"))
                result[key] = copy.deepcopy(sub_default)
                continue
            result[key] = _merge_validate(sub_default, raw, spec, node_path, issues)
            continue

        kind, rule = spec
        raw = data.get(key, _MISSING)
        if raw is _MISSING:
            result[key] = copy.deepcopy(default) if default is not _MISSING else None
            continue
        value, ok, reason = _coerce(
            None if default is _MISSING else default, raw, kind, rule
        )
        if not ok:
            issues.append((node_path, reason))
        result[key] = value

    # Keep unknown keys untouched so a newer build's settings survive a downgrade
    # round trip instead of being silently destroyed.
    for key, value in data.items():
        if key not in schema:
            result[key] = copy.deepcopy(value)

    return result


# --------------------------------------------------------------------------- #
# Path resolution (portable extension point)
# --------------------------------------------------------------------------- #

def _marker_candidates() -> List[str]:
    candidates: List[str] = []
    for base in (sys.argv[0] if sys.argv else "", sys.executable or ""):
        if not base:
            continue
        folder = os.path.dirname(os.path.abspath(base))
        marker = os.path.join(folder, PORTABLE_MARKER_NAME)
        if marker not in candidates:
            candidates.append(marker)
    return candidates


def _portable_root() -> Optional[str]:
    """Return the app folder when a portable marker is present, else None."""
    for marker in _marker_candidates():
        if os.path.isfile(marker):
            return os.path.dirname(marker)
    env_dir = os.environ.get("SUBMIRROR_PORTABLE_DIR", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    return None


def is_portable() -> bool:
    """
    Portable-build probe. v0.1 keeps portable mode disabled; the resolver is
    already in place so a later build only needs PORTABLE_MODE_ENABLED = True.
    """
    if not PORTABLE_MODE_ENABLED:
        return False
    return _portable_root() is not None


def resolve_config_path() -> str:
    """Absolute path of config.json for the current build layout."""
    if is_portable():
        root = _portable_root()
        if root:
            return os.path.join(root, _PORTABLE_FOLDER.replace("/", os.sep))
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return os.path.join(appdata, APP_NAME, _CONFIG_FILE_NAME)
    # Fallback for a stripped environment: keep everything inside the user folder.
    return os.path.join(os.path.expanduser("~"), ".%s" % APP_NAME.lower(),
                        _CONFIG_FILE_NAME)


# --------------------------------------------------------------------------- #
# Hard-coded bilingual UI dictionary (zh-CN / en)
# --------------------------------------------------------------------------- #

lang_data: Dict[str, Dict[str, str]] = {
    "zh-CN": {
        # Application shell
        "app.title": "SubMirror 实时翻译",
        "app.version": "版本 v{version}",
        # Startup disclaimer
        "disclaimer.title": "使用前请阅读",
        "disclaimer.body": (
            "SubMirror 通过 Windows 原生桌面截图与系统混音环回（WASAPI Loopback）"
            "获取画面和声音，纯用户态运行，不注入、不挂钩、不读取任何游戏进程。"
        ),
        "disclaimer.risk_title": "风险提示",
        "disclaimer.risk": (
            "所有第三方辅助工具都存在极低概率被反作弊系统误判的风险。"
            "请自行评估并承担使用后果，作者不对账号处罚负责。"
        ),
        "disclaimer.accept": "我已阅读并同意",
        "disclaimer.decline": "退出",
        "disclaimer.hint": "勾选同意后方可加载悬浮窗与设置面板。",
        "disclaimer.required": "请先勾选“我已阅读并同意”。",
        # Settings panel
        "settings.title": "SubMirror 设置",
        "settings.language": "界面语言",
        "settings.group.general": "通用",
        "settings.group.hotkeys": "热键",
        "settings.group.mode": "工作模式",
        "settings.group.audio": "音频",
        "settings.group.ocr": "OCR 屏幕识别",
        "settings.group.asr": "语音识别",
        "settings.group.translate": "翻译",
        "settings.group.subtitle": "字幕显示",
        "settings.group.overlay": "悬浮窗",
        "settings.group.maintenance": "维护",
        # BL-5: dedicated tab titles so the page order matches the acceptance
        # checklist without re-purposing the in-page group box text.
        "settings.tab.general": "通用",
        "settings.tab.hotkeys": "全局热键",
        "settings.tab.asr": "音频 / ASR",
        "settings.tab.translate": "翻译",
        "settings.tab.ocr": "OCR",
        "settings.tab.overlay": "悬浮窗样式",
        "settings.tab.queue": "队列与限流",
        "settings.tab.advanced": "高级选项",
        "settings.tab.about": "关于与隐私",
        "settings.group.queue": "字幕队列",
        "settings.group.cache": "翻译缓存",
        "settings.group.resilience": "翻译容错",
        "settings.group.about": "关于",
        "settings.group.privacy": "隐私与数据",
        "settings.privacy_note": (
            "所有设置与字幕内容只保存在本机 config.json 中。仅当你启用翻译时，"
            "待翻译文本才会发往 config.json 中配置的公共翻译端点"
            "（Google 公共接口 / MyMemory / LibreTranslate）；"
            "本工具不采集遥测、不上传截图、不访问麦克风。"
        ),
        "settings.fps_safe_mode": "FPS 安全模式",
        "settings.fps_safe_hint": "开启后区域 OCR 锁定 {fps} FPS，降低对游戏帧率的干扰。",
        # OCR group: WinRT OCR is the only v0.1 engine (Windows.Media.Ocr)
        "settings.group.v02": "v0.2 规划",
        "settings.ocr_winrt_hint": (
            "使用 Windows 系统内置 OCR（Windows.Media.Ocr），仅在本地运行，"
            "需先在“设置 → 时间和语言 → 语言”中安装对应语言包。"
        ),
        "settings.ocr_winrt_auto": "自动选择已安装的 OCR 语言",
        "settings.ocr_winrt_language": "首选 OCR 语言",
        "settings.ocr_winrt_language.auto": "自动（推荐）",
        "settings.ocr_winrt_language.en-US": "英语（en-US）",
        "settings.ocr_winrt_language.zh-Hans-CN": "简体中文（zh-Hans-CN）",
        "settings.ocr_winrt_language.zh-Hant-TW": "繁体中文（zh-Hant-TW）",
        "settings.ocr_winrt_language.ja-JP": "日语（ja-JP）",
        "settings.ocr_winrt_language.ko-KR": "韩语（ko-KR）",
        "settings.ocr_winrt_language.ru-RU": "俄语（ru-RU）",
        "settings.ocr_area_fps_normal": "区域 OCR 采样上限（FPS）",
        "settings.ocr_area_fps_safe": "FPS 安全模式采样上限（FPS）",
        "settings.ocr_fullscreen_interval": "全屏 OCR 最小触发间隔（毫秒）",
        "settings.ocr_confidence_min": "置信度阈值（0 = 关闭）",
        "settings.ocr_confidence_hint": "Windows 内置 OCR 不返回置信度，该项只对会打分的引擎生效；实际过滤以下面两项为准。",
        "settings.ocr_min_line_height": "最小行高（像素，0 = 关闭）",
        "settings.ocr_min_line_chars": "最小字符数（0 = 关闭）",
        # Fixed-region OCR group (v0.1.13)
        "settings.ocr_fixed_region": "固定区域（left,top,width,height）",
        "settings.ocr_fixed_region_hint": "先画框保存坐标（松开左键即保存，Esc 结束）；开启后循环截取该区域，只有画面或文字变化时才重新识别并翻译。",
        "settings.ocr_fixed_region_pick": "画框…",
        "settings.ocr_fixed_region_none": "尚未选择",
        "settings.ocr_fixed_region_yes": "已保存：{region}",
        "settings.ocr_region_enabled": "开启固定区域循环识别",
        "settings.ocr_region_tolerance": "画面变化阈值（越小越灵敏）",
        # Audio / speech group
        "settings.audio_spike_filter": "音频尖峰过滤（毫秒）",
        "settings.audio_reconnect": "音频断流重连次数",
        "settings.audio_silence_log": "长时间静音提示间隔（秒）",
        "settings.asr_language": "识别语言",
        "settings.asr_language.auto": "自动检测",
        "settings.asr_language.en": "英语",
        "settings.asr_language.zh": "中文",
        "settings.asr_beam_size": "Beam size（越大越准越慢）",
        "settings.asr_vad_min_speech": "FPS 模式最小语音时长（毫秒）",
        "settings.asr_vad_threshold": "语音门限（dBFS）",
        "settings.asr_model_size": "模型规格",
        "settings.asr_model_size.hint": "在 base / small 等 Whisper 规格间切换，重启识别后生效",
        "settings.asr_vad_engine": "人声检测",
        # Hover tips (0.1.16): one line per option, shown on the ASR page. They
        # only explain the control; no config key and no behaviour depends here.
        "settings.asr_model_size.tip.none": "跟随模型路径：手动指定模型文件夹，适合进阶用户自定义模型。",
        "settings.asr_model_size.tip.tiny": "tiny：体积最小、速度最快；识别精度低。适合低配电脑、安静环境短句，不适合带枪声/背景音乐。",
        "settings.asr_model_size.tip.base": "base【默认推荐】：体积小速度快，精度均衡。适配绝大多数 FPS 游戏、视频通话，内置在便携包，开箱即用。",
        "settings.asr_model_size.tip.small": "small：抗噪与识别精度更好；CPU/GPU 占用更高。适合嘈杂环境、带背景音乐人声、英文歌词识别，需自行下载模型放入 models 文件夹。",
        "settings.asr_model_size.tip.medium": "medium：识别能力强，强抗干扰；资源消耗大，低配设备容易产生延迟，不推荐游戏实时翻译。",
        "settings.asr_vad_engine.tip.header": "VAD 引擎（audio.vad_engine = {engine}）",
        "settings.asr_vad_engine.tip.auto": "auto：自动优先尝试 Silero VAD，失败自动回退 Energy。",
        "settings.asr_vad_engine.tip.silero": "silero VAD：AI 人声检测。只识别干净人类说话声，过滤枪声、背景音乐；适合纯游戏语音对话，会过滤歌曲演唱声音。",
        "settings.asr_vad_engine.tip.energy": "energy VAD：音量阈值检测。只要声音响度达标就送入识别；适合带背景音乐、歌曲歌词识别，缺点是纯噪音也会尝试识别。",
        "settings.subtitle_fade_in": "字幕淡入时长（毫秒，0 = 关闭）",
        "settings.subtitle_fade_out": "字幕淡出时长（毫秒，0 = 关闭）",
        "settings.subtitle_fade_slide": "淡入位移（像素）",
        # Translation group
        "settings.translate_source": "源语言",
        "settings.translate_target": "目标语言",
        "settings.translate_timeout": "单次翻译超时（秒）",
        "settings.translate_threshold": "连续失败熔断阈值",
        "settings.translate_circuit": "熔断持续时长（秒）",
        "settings.translate_libre_base": "LibreTranslate 服务地址",
        "settings.translate_libre_hint": "留空使用公共实例；自建服务可填自己的地址。",
        "settings.translate_mymemory_email": "MyMemory 邮箱（可选）",
        "settings.translate_mymemory_hint": "留空则匿名使用；填写后免费额度更高。",
        "settings.translate_cache_ttl": "翻译缓存时长（秒）",
        "settings.translate_cache_max": "翻译缓存条数上限",
        # Logon auto-start of the local LibreTranslate service
        "settings.translate_libre_dir": "LibreTranslate 目录",
        "settings.translate_libre_dir_hint": "包含 start_libretranslate.bat 的目录；端口取自「服务地址」。",
        "settings.translate_libre_autostart": "登录时静默启动 LibreTranslate（无窗口）",
        "settings.translate_libre_autostart_hint": "在「启动」文件夹登记一个脚本：登录后无黑窗启动 LibreTranslate，等它就绪再启动 SubMirror；无需管理员权限。取消勾选会删除该启动项。",
        "settings.libre_browse": "浏览…",
        "settings.libre_dir_ok": "目录可用，已找到 start_libretranslate.bat。",
        "settings.libre_dir_invalid": "目录不可用：没有找到 start_libretranslate.bat。",
        "settings.libre_dir_optional": "未配置 LibreTranslate 目录；开机自启只启动 SubMirror 本体（可选）。",
        "settings.libre_register_ok": "已登记登录启动项。",
        "settings.libre_register_failed": "登录启动项登记失败。",
        "settings.libre_unregister_ok": "已删除登录启动项。",
        "settings.libre_legacy_hint": "检测到旧的 SubMirror-LibreTranslate.vbs，可手动删除（本功能已覆盖它）。",
        "silent.libre_timeout": "LibreTranslate 在 %d 秒内没有就绪，SubMirror 未启动。\n详情见 %s",
        # Subtitle / overlay group
        "settings.font_family": "字体",
        "settings.overlay_width": "悬浮窗宽度",
        "settings.overlay_height": "悬浮窗高度",
        "settings.overlay_reset_position": "重置悬浮窗位置",
        "settings.shortcut_hint": "点击输入框后直接按下组合键；按 Esc 清除。",
        "settings.range_hint": "取值范围：{low} ~ {high}",
        "settings.unavailable_hint": "当前运行环境缺少 PySide6，无法显示设置面板。",
        "settings.show_original": "显示原始英文",
        "settings.show_original_hint": "上行字幕同时显示原文与译文；翻译失败时保留原文。",
        "settings.overlay_top": "置顶模式（鼠标穿透）",
        "settings.overlay_click_through": "鼠标穿透",
        "settings.overlay_font_color": "字幕文字颜色",
        "settings.overlay_pick_color": "选择…",
        "settings.overlay_lock_position": "锁定悬浮窗位置",
        "settings.hotkey.area_ocr": "区域 OCR 热键（按住拖拽）",
        "settings.hotkey.fullscreen_ocr": "全屏 OCR 热键（单次快照）",
        "settings.hotkey.toggle_asr": "ASR 启停热键（可留空）",
        "settings.hotkey.hint": "热键不可重复；ASR 启停热键可留空表示不绑定。",
        "settings.hotkey.toggle_hint": "按下后可暂停/恢复语音识别，留空则只使用托盘与设置面板。",
        "settings.hotkey.toggle_region": "固定区域 OCR 启停热键（可留空）",
        "settings.hotkey.region_hint": "按下即可唤起画框（尚无坐标时）或开启/停止固定区域识别；画框时长按左键拖拽，松开左键即保存，Esc 结束，留空则不绑定。",
        "settings.model_path": "Whisper 模型目录",
        "settings.model_browse": "浏览…",
        "settings.model_ok": "模型目录校验通过。",
        "settings.model_invalid_title": "模型目录无效",
        "settings.model_invalid_body": "该目录不是可用的 Faster-Whisper 模型：缺少模型文件。",
        "settings.model_non_ascii_title": "模型路径包含非 ASCII 字符",
        "settings.model_non_ascii_body": "该目录名含中文或其他非 ASCII 字符。Faster-Whisper 的底层库在部分环境下会因此读取失败，建议改用纯英文路径。",
        "settings.vad_mode": "VAD 模式",
        "settings.vad_mode.single": "单机模式（默认灵敏度）",
        "settings.vad_mode.fps": "FPS 模式（过滤枪声短噪声）",
        "settings.vad_hint": "FPS 模式最小语音时长 {ms} ms。",
        "settings.font_size": "字号",
        "settings.font_scale_with_panel": "文字随面板缩放",
        "settings.font_scale_with_panel.tip": "开启后字号按面板宽度等比例缩放：面板拉大时文字同步变大，比例保持不变。",
        "settings.text_opacity": "字幕不透明度",
        "settings.mask_opacity": "遮罩不透明度",
        "settings.mask_opacity.hint": "0 = 完全透明（无底色），100 = 完全不透明。",
        "settings.fade_duration": "字幕动画时长",
        "settings.fade_duration.hint": "淡入淡出用时；0 = 关闭动画（立即显示 / 立即消失）。",
        "settings.size_preset": "面板预设尺寸",
        "settings.size_preset.default": "默认圆角长方形",
        "settings.size_preset.compact": "紧凑窄长矩形",
        "settings.size_preset.square": "正方形",
        "settings.size_preset.tip": "一键套用面板宽高与圆角；套用后仍可用下方滑块微调。",
        "settings.corner_radius": "圆角半径",
        "settings.corner_radius.tip": "0 = 直角，数值越大圆角越大（0~48 px）。",
        "settings.overlay_outline_width": "文字描边粗细",
        "settings.overlay_outline_width.tip": "0 = 不描边；适当加粗可提升复杂游戏画面下的可读性（0~8 px）。",
        "settings.group.logging": "日志",
        "settings.log_level": "日志等级",
        "settings.log_level.tip": "默认「仅警告」只输出关键错误，减少控制台与日志写入带来的卡顿；调试时再开启信息/调试等级。",
        "settings.log_level.debug": "调试（最详细）",
        "settings.log_level.info": "信息",
        "settings.log_level.warning": "仅警告（默认）",
        "settings.log_level.error": "仅错误",
        "settings.group.application": "启动",
        "settings.autostart": "登录时自动启动 SubMirror",
        "settings.autostart.tip": "在「启动」文件夹登记一个无窗口脚本，登录后自动打开 SubMirror；默认关闭，取消勾选会删除该启动项。",
        # -- work modes (v0.1.19) --
        "settings.mode": "工作模式",
        "settings.mode.allround": "全能模式",
        "settings.mode.game": "游戏模式",
        "settings.mode.conversation": "对话模式",
        "settings.mode.tip": "切换模式会载入该模式的推荐参数（下方各项仍可单独修改）；保存后立即生效。",
        "settings.mode.allround.tip": "默认标准参数，游戏字幕与日常视频通用。",
        "settings.mode.game.tip": "抬高语音门限并过滤枪声等短促爆破音，减少 NPC 短句误触发，同时降低 CPU 与刷新率以保护游戏帧率。",
        "settings.mode.conversation.tip": "优先拾取麦克风，降低扬声器权重并抑制背景底噪，适合 Talking 通话与视频会议翻译。",
        "settings.mode.auto_params.tip": "开启后每次启动都会套用所选模式的推荐参数，避免手改配置后模式行为失效。",
        "settings.mode.auto_params": "启动时自动应用模式参数",
        "settings.mode.summary": "推荐参数：{summary}",
        "settings.group.capture": "音频来源与混音",
        "settings.audio.source": "音频来源",
        "settings.audio.source.speaker": "扬声器（系统声音）",
        "settings.audio.source.microphone": "麦克风",
        "settings.audio.source.mixed": "麦克风 + 扬声器混合",
        "settings.audio.source.tip": "「扬声器」只采系统输出；「麦克风」只采默认输入设备；「混合」同时采集两路并按下方权重相加。",
        "settings.audio.mic_device": "麦克风设备",
        "settings.audio.mic_device.tip": "输入设备序号或名称片段，留空表示跟随系统默认麦克风。",
        "settings.audio.mic_gain": "麦克风权重",
        "settings.audio.speaker_gain": "扬声器权重",
        "settings.audio.gain.tip": "混音时的相加权重：游戏/视频字幕用扬声器 1.0；通话时把扬声器降到 0.35 左右以突出麦克风。",
        "settings.audio.noise_suppression": "背景降噪",
        "settings.audio.noise_reduction_db": "降噪强度（dB）",
        "settings.audio.noise_suppression.tip": "按运行中的底噪估计做频谱门限，只作用于麦克风与混合来源，用于压掉房间底噪。",
        "settings.audio.burst_filter": "爆破音过滤",
        "settings.audio.burst_max_ms": "爆破音最长时长（毫秒）",
        "settings.audio.burst_margin_db": "爆破音判定余量（dB）",
        "settings.audio.burst_filter.tip": "枪声等短促爆破音比人声短得多：能量高出运行中位数且不超过此时长的片段会被静音。",
        "settings.pause_on_full": "队列满时暂停采集",
        "settings.pause_on_full.tip": "识别任务积压到队列上限时暂时停止采集，等队列腾空后自动恢复：宁可丢一小段音频，也不让延迟无限增长。",
        "overlay.status.capture_paused": "队列已满，已暂停采集",
        "overlay.status.translate_timeout": "翻译超时",
        "overlay.status.translate_failed": "翻译服务繁忙",
        "overlay.status.busy": "音频积压，已跳过一段",
        "settings.subtitle_queue_max": "每条字幕最大排队数",
        "settings.subtitle_timeout": "字幕显示超时（秒）",
        "settings.btn.save": "保存",
        "settings.btn.cancel": "取消",
        "settings.btn.close": "关闭",
        "settings.btn.browse": "浏览…",
        "settings.btn.reset_defaults": "恢复默认",
        "settings.btn.restart_audio": "重启音频任务",
        "settings.btn.restart_ocr": "重启 OCR 任务",
        "settings.btn.restart_tasks": "重置后台任务",
        "settings.reset_done": "已恢复默认配置。",
        "settings.tasks_reset": "后台任务已重置，无需重启程序。",
        # Dialogs
        "dialog.info.title": "提示",
        "dialog.error.title": "错误",
        "dialog.hotkey_conflict.title": "热键冲突",
        "dialog.hotkey_conflict.body": "热键不能重复：{keys}",
        "dialog.hotkey_register_fail.title": "热键注册失败",
        "dialog.hotkey_register_fail.body": "热键 {key} 已被其他程序占用，请更换后重试。",
        "dialog.config_reset.title": "配置已重置",
        "dialog.config_reset.body": "config.json 缺失或损坏，已自动恢复默认配置。",
        "dialog.permission.title": "权限不足",
        "dialog.permission.body": "无法读写配置文件：{detail}",
        # Status indicator
        "status.audio_ok": "音频正常",
        "status.audio_busy": "处理中",
        "status.audio_error": "异常",
        "status.audio_reconnecting": "音频重连中",
        "status.ocr_running": "OCR 采集中",
        # Overlay
        "overlay.asr_placeholder": "语音字幕待识别…",
        "overlay.ocr_placeholder": "按住区域 OCR 热键可识别画面文字",
        "overlay.tts_placeholder": "（TTS 预留）",
        # Translation
        "translate.unavailable": "翻译服务暂不可用，请稍后重试。",
        "translate.failed": "翻译失败",
        "translate.original_prefix": "原文：",
        # Console log templates (console only, no log files in v0.1)
        "log.capture_started": "[audio] loopback capture started on {device}",
        "log.capture_failed": "[audio] capture error: {detail}",
        "log.capture_reconnect": "[audio] reconnecting ({attempt}/{total})…",
        "log.capture_reconnect_failed": "[audio] reconnect failed, audio module disabled.",
        "log.silence_warning": "[audio] long silence detected, no ASR task submitted.",
        "log.ocr_dropped_frame": "[ocr] backlog exceeded, dropped a stale frame.",
        "log.ocr_failed": "[ocr] frame discarded: {detail}",
        "log.task_error": "[task] {name} failed: {detail}",
        "log.hotkey_registered": "[hotkey] registered {name} = {key}",
        "log.hotkey_release": "[hotkey] released all registered hotkeys.",
        "log.shutdown": "[app] shutting down, stopping background modules…",
        # v0.2 placeholders (UI only, no business logic in v0.1)
        "v02.todo": "该功能将在 v0.2 提供。",
        "v02.slang": "俚语词典替换",
        "v02.log_export": "日志导出",
        "v02.pause_on_blur": "失焦暂停",
        "v02.model_download": "一键下载 Whisper 模型",
        "v02.multi_source": "多翻译源",
        "v02.vad_slider": "VAD 灵敏度滑块",
        "v02.area_preset": "OCR 选区预设",
        "v02.subtitle_history": "字幕历史",
        "v02.subtitle_anim": "字幕动画",
        "v02.streaming_asr": "增量实时 ASR",
        "v02.window_tracking": "窗口追踪 OCR",
        "v02.multimonitor": "多显示器 OCR",
    },
    "en": {
        "app.title": "SubMirror Live Translate",
        "app.version": "Version v{version}",
        "disclaimer.title": "Please read before use",
        "disclaimer.body": (
            "SubMirror captures the screen with native Windows desktop APIs and "
            "captures audio via system loopback (WASAPI). It runs entirely in "
            "user mode: no injection, no hooking, no reading of any game process."
        ),
        "disclaimer.risk_title": "Risk notice",
        "disclaimer.risk": (
            "Any third-party helper carries a very small chance of being flagged "
            "by anti-cheat systems. Use it at your own discretion; the author is "
            "not responsible for account penalties."
        ),
        "disclaimer.accept": "I have read and agree",
        "disclaimer.decline": "Exit",
        "disclaimer.hint": "Agreement is required before the overlay and settings load.",
        "disclaimer.required": "Please tick “I have read and agree” first.",
        "settings.title": "SubMirror Settings",
        "settings.language": "Interface language",
        "settings.group.general": "General",
        "settings.group.hotkeys": "Hotkeys",
        "settings.group.mode": "Work mode",
        "settings.group.audio": "Audio",
        "settings.group.ocr": "OCR",
        "settings.group.asr": "Speech recognition",
        "settings.group.translate": "Translation",
        "settings.group.subtitle": "Subtitle",
        "settings.group.overlay": "Overlay",
        "settings.group.maintenance": "Maintenance",
        # BL-5: dedicated tab titles so the page order matches the acceptance
        # checklist without re-purposing the in-page group box text.
        "settings.tab.general": "General",
        "settings.tab.hotkeys": "Hotkeys",
        "settings.tab.asr": "Audio / ASR",
        "settings.tab.translate": "Translation",
        "settings.tab.ocr": "OCR",
        "settings.tab.overlay": "Overlay style",
        "settings.tab.queue": "Queue and limits",
        "settings.tab.advanced": "Advanced",
        "settings.tab.about": "About and privacy",
        "settings.group.queue": "Subtitle queue",
        "settings.group.cache": "Translation cache",
        "settings.group.resilience": "Translation resilience",
        "settings.group.about": "About",
        "settings.group.privacy": "Privacy and data",
        "settings.privacy_note": (
            "All settings and subtitle text stay in the local config.json. Only when "
            "translation is enabled is the text to translate sent to the public "
            "endpoints configured in config.json (Google public endpoint / MyMemory / "
            "LibreTranslate). No telemetry, no screenshot uploads, no microphone."
        ),
        "settings.fps_safe_mode": "FPS safe mode",
        "settings.fps_safe_hint": "Locks area OCR to {fps} FPS to reduce frame-rate impact.",
        # OCR group: WinRT OCR is the only v0.1 engine (Windows.Media.Ocr)
        "settings.group.v02": "v0.2 roadmap",
        "settings.ocr_winrt_hint": (
            "Uses the OCR engine built into Windows (Windows.Media.Ocr). It runs "
            "entirely offline; install the language pack under Settings > Time & "
            "language > Language first."
        ),
        "settings.ocr_winrt_auto": "Pick an installed OCR language automatically",
        "settings.ocr_winrt_language": "Preferred OCR language",
        "settings.ocr_winrt_language.auto": "Automatic (recommended)",
        "settings.ocr_winrt_language.en-US": "English (en-US)",
        "settings.ocr_winrt_language.zh-Hans-CN": "Chinese, Simplified (zh-Hans-CN)",
        "settings.ocr_winrt_language.zh-Hant-TW": "Chinese, Traditional (zh-Hant-TW)",
        "settings.ocr_winrt_language.ja-JP": "Japanese (ja-JP)",
        "settings.ocr_winrt_language.ko-KR": "Korean (ko-KR)",
        "settings.ocr_winrt_language.ru-RU": "Russian (ru-RU)",
        "settings.ocr_area_fps_normal": "Area OCR sampling cap (FPS)",
        "settings.ocr_area_fps_safe": "FPS safe mode sampling cap (FPS)",
        "settings.ocr_fullscreen_interval": "Full-screen OCR minimum interval (ms)",
        "settings.ocr_confidence_min": "Confidence threshold (0 = off)",
        "settings.ocr_confidence_hint": ("Windows' built-in OCR reports no confidence, so this only "
                                         "affects engines that score their lines; the two settings "
                                         "below do the actual filtering."),
        "settings.ocr_min_line_height": "Minimum line height (px, 0 = off)",
        "settings.ocr_min_line_chars": "Minimum character count (0 = off)",
        # Fixed-region OCR group (v0.1.13)
        "settings.ocr_fixed_region": "Fixed region (left,top,width,height)",
        "settings.ocr_fixed_region_hint": ("Drag a frame on screen to save the coordinates (releasing "
                                           "the mouse saves, Esc ends the pick); while the loop is on "
                                           "this region is captured repeatedly and only a changed "
                                           "picture or line is recognised and translated."),
        "settings.ocr_fixed_region_pick": "Draw frame...",
        "settings.ocr_fixed_region_none": "Not selected yet",
        "settings.ocr_fixed_region_yes": "Saved: {region}",
        "settings.ocr_region_enabled": "Run the fixed-region OCR loop",
        "settings.ocr_region_tolerance": "Picture change threshold (lower = more sensitive)",
        # Audio / speech group
        "settings.audio_spike_filter": "Audio spike filter (ms)",
        "settings.audio_reconnect": "Audio reconnect attempts",
        "settings.audio_silence_log": "Long silence notice interval (s)",
        "settings.asr_language": "Recognition language",
        "settings.asr_language.auto": "Auto detect",
        "settings.asr_language.en": "English",
        "settings.asr_language.zh": "Chinese",
        "settings.asr_beam_size": "Beam size (higher = slower, more accurate)",
        "settings.asr_vad_min_speech": "FPS mode minimum speech duration (ms)",
        "settings.asr_vad_threshold": "Speech gate (dBFS)",
        "settings.asr_model_size": "Model size",
        "settings.asr_model_size.hint": "Switch between Whisper sizes (base / small); applies after recognition restarts",
        "settings.asr_vad_engine": "Voice detection",
        # Hover tips (0.1.16) - same content as the zh table above.
        "settings.asr_model_size.tip.none": "Follow model path: point SubMirror at your own model folder; for advanced users with a custom model.",
        "settings.asr_model_size.tip.tiny": "tiny: smallest and fastest, lowest accuracy. Fits low-end machines and short lines in a quiet room, not gunfire or background music.",
        "settings.asr_model_size.tip.base": "base [recommended default]: small, fast, balanced accuracy. Fits most FPS games and voice chat, bundled in the portable package, ready out of the box.",
        "settings.asr_model_size.tip.small": "small: better noise handling and accuracy, higher CPU/GPU load. Fits noisy rooms, speech over background music and English lyrics; download the model yourself into the models folder.",
        "settings.asr_model_size.tip.medium": "medium: strongest recognition and noise rejection, heavy resource use. Low-end machines tend to lag; not recommended for live game translation.",
        "settings.asr_vad_engine.tip.header": "VAD engine (audio.vad_engine = {engine})",
        "settings.asr_vad_engine.tip.auto": "auto: try Silero VAD first, fall back to Energy automatically.",
        "settings.asr_vad_engine.tip.silero": "silero VAD: AI voice detection. Accepts clean human speech only and filters gunfire and background music; best for pure game voice chat, and it does filter singing.",
        "settings.asr_vad_engine.tip.energy": "energy VAD: loudness threshold. Anything loud enough goes to recognition; fits background music and lyrics, but pure noise is attempted too.",
        "settings.subtitle_fade_in": "Subtitle fade-in (ms, 0 = off)",
        "settings.subtitle_fade_out": "Subtitle fade-out (ms, 0 = off)",
        "settings.subtitle_fade_slide": "Fade-in slide (px)",
        # Translation group
        "settings.translate_source": "Source language",
        "settings.translate_target": "Target language",
        "settings.translate_timeout": "Timeout per translation (s)",
        "settings.translate_threshold": "Consecutive failures before breaking",
        "settings.translate_circuit": "Circuit cool-down (s)",
        "settings.translate_libre_base": "LibreTranslate base URL",
        "settings.translate_libre_hint": (
            "Empty uses the public instance; set your own address for a self-hosted server."
        ),
        "settings.translate_mymemory_email": "MyMemory e-mail (optional)",
        "settings.translate_mymemory_hint": (
            "Empty uses the anonymous quota; providing one raises the free daily limit."
        ),
        "settings.translate_cache_ttl": "Translation cache TTL (s)",
        "settings.translate_cache_max": "Translation cache entries",
        # Logon auto-start of the local LibreTranslate service
        "settings.translate_libre_dir": "LibreTranslate folder",
        "settings.translate_libre_dir_hint": (
            "Folder that holds start_libretranslate.bat; the port comes from the base URL."
        ),
        "settings.translate_libre_autostart": (
            "Start LibreTranslate silently at sign-in (no window)"
        ),
        "settings.translate_libre_autostart_hint": (
            "Registers a script in the Startup folder: LibreTranslate starts hidden at "
            "sign-in, SubMirror starts once the service answers. No administrator rights "
            "are needed. Clearing the box deletes that entry."
        ),
        "settings.libre_browse": "Browse...",
        "settings.libre_dir_ok": "Folder is usable; start_libretranslate.bat found.",
        "settings.libre_dir_invalid": "Folder is not usable: start_libretranslate.bat is missing.",
        "settings.libre_dir_optional": "No LibreTranslate folder set; starting at logon only opens SubMirror itself (optional).",
        "settings.libre_register_ok": "Logon entry registered.",
        "settings.libre_register_failed": "The logon entry could not be registered.",
        "settings.libre_unregister_ok": "Logon entry removed.",
        "settings.libre_legacy_hint": (
            "An old SubMirror-LibreTranslate.vbs was found; it can be deleted by hand "
            "(this setting already covers it)."
        ),
        "silent.libre_timeout": (
            "LibreTranslate did not become ready within %d seconds.\n"
            "SubMirror was not started. Details: %s"
        ),
        # Subtitle / overlay group
        "settings.font_family": "Font",
        "settings.overlay_width": "Overlay width",
        "settings.overlay_height": "Overlay height",
        "settings.overlay_reset_position": "Reset overlay position",
        "settings.shortcut_hint": "Click the field and press the combination; Esc clears it.",
        "settings.range_hint": "Allowed range: {low} ~ {high}",
        "settings.unavailable_hint": "PySide6 is missing, so the settings panel is unavailable.",
        "settings.show_original": "Show original English",
        "settings.show_original_hint": (
            "Show both the original text and the translation; the original is kept "
            "when translation fails."
        ),
        "settings.overlay_top": "Always on top (mouse pass-through)",
        "settings.overlay_click_through": "Click-through",
        "settings.overlay_font_color": "Subtitle text colour",
        "settings.overlay_pick_color": "Pick…",
        "settings.overlay_lock_position": "Lock overlay position",
        "settings.hotkey.area_ocr": "Area OCR hotkey (hold and drag)",
        "settings.hotkey.fullscreen_ocr": "Full-screen OCR hotkey (single shot)",
        "settings.hotkey.toggle_asr": "ASR on/off hotkey (optional)",
        "settings.hotkey.hint": "Hotkeys must differ; leave the ASR hotkey empty to skip it.",
        "settings.hotkey.toggle_hint": "Press to pause or resume speech recognition; empty means tray and settings only.",
        "settings.hotkey.toggle_region": "Fixed-region OCR on/off hotkey (optional)",
        "settings.hotkey.region_hint": ("Press to open the pick window (no coordinates yet) or to start "
                                        "and stop the fixed-region loop. While picking, drag with the "
                                        "left button held, release to save, Esc to finish; empty means "
                                        "no binding."),
        "settings.model_path": "Whisper model folder",
        "settings.model_browse": "Browse…",
        "settings.model_ok": "Model folder validated.",
        "settings.model_invalid_title": "Invalid model folder",
        "settings.model_invalid_body": (
            "This folder is not a usable Faster-Whisper model: model files missing."
        ),
        "settings.model_non_ascii_title": "Model path contains non-ASCII characters",
        "settings.model_non_ascii_body": (
            "The folder name contains Chinese or other non-ASCII characters. "
            "Faster-Whisper's native libraries may fail to load such a path; "
            "an ASCII-only path is safer."
        ),
        "settings.vad_mode": "VAD mode",
        "settings.vad_mode.single": "Single mode (default sensitivity)",
        "settings.vad_mode.fps": "FPS mode (filters gunfire noise)",
        "settings.vad_hint": "FPS mode uses a minimum speech duration of {ms} ms.",
        "settings.font_size": "Font size",
        "settings.font_scale_with_panel": "Text follows panel size",
        "settings.font_scale_with_panel.tip": "When on, the font size scales with the panel width, so text and box keep their ratio.",
        "settings.text_opacity": "Text opacity",
        "settings.mask_opacity": "Mask opacity",
        "settings.mask_opacity.hint": "0 = fully transparent (no backdrop), 100 = fully opaque.",
        "settings.fade_duration": "Subtitle animation",
        "settings.fade_duration.hint": "Fade-in / fade-out duration; 0 disables the animation.",
        "settings.size_preset": "Panel size preset",
        "settings.size_preset.default": "Default rounded rectangle",
        "settings.size_preset.compact": "Compact narrow bar",
        "settings.size_preset.square": "Square",
        "settings.size_preset.tip": "Applies a panel width, height and corner radius in one click; the sliders below still fine-tune it.",
        "settings.corner_radius": "Corner radius",
        "settings.corner_radius.tip": "0 = square corners, higher values round them more (0-48 px).",
        "settings.overlay_outline_width": "Text outline width",
        "settings.overlay_outline_width.tip": "0 = no outline; a thicker outline improves readability over busy game scenes (0-8 px).",
        "settings.group.logging": "Logging",
        "settings.log_level": "Log level",
        "settings.log_level.tip": "The default \"warnings only\" keeps the console and the log file quiet; raise it to info/debug only while diagnosing.",
        "settings.log_level.debug": "Debug (most verbose)",
        "settings.log_level.info": "Info",
        "settings.log_level.warning": "Warnings only (default)",
        "settings.log_level.error": "Errors only",
        "settings.group.application": "Startup",
        "settings.autostart": "Start SubMirror at logon",
        "settings.autostart.tip": "Registers a windowless script in your Startup folder so SubMirror opens at sign-in. Off by default; clearing the box removes the entry.",
        # -- work modes (v0.1.19) --
        "settings.mode": "Work mode",
        "settings.mode.allround": "All-round",
        "settings.mode.game": "Game",
        "settings.mode.conversation": "Conversation",
        "settings.mode.tip": "Switching a mode loads its recommended parameters (every row below stays editable); it takes effect as soon as you save.",
        "settings.mode.allround.tip": "The standard defaults, suited to game subtitles and everyday video.",
        "settings.mode.game.tip": "Raises the speech gate and rejects short bursts such as gunfire, so NPC one-liners stop triggering it, while a lower CPU profile protects the frame rate.",
        "settings.mode.conversation.tip": "Prefers the microphone, attenuates the speaker and suppresses room noise - built for Talking calls and video meetings.",
        "settings.mode.auto_params.tip": "When on, every start re-applies the selected mode's recommended parameters, so a hand-edited config cannot silently defeat the mode.",
        "settings.mode.auto_params": "Apply mode parameters at start-up",
        "settings.mode.summary": "Recommended: {summary}",
        "settings.group.capture": "Capture source and mixing",
        "settings.audio.source": "Audio source",
        "settings.audio.source.speaker": "Speaker (system sound)",
        "settings.audio.source.microphone": "Microphone",
        "settings.audio.source.mixed": "Microphone + speaker mix",
        "settings.audio.source.tip": "Speaker records the system output only, microphone records the default input only, and mixed sums both with the weights below.",
        "settings.audio.mic_device": "Microphone device",
        "settings.audio.mic_device.tip": "Input device index or a name substring; empty follows the system default microphone.",
        "settings.audio.mic_gain": "Microphone gain",
        "settings.audio.speaker_gain": "Speaker gain",
        "settings.audio.gain.tip": "Summing weights during mixing: use speaker 1.0 for game/video subtitles; drop the speaker to about 0.35 in a call so the microphone dominates.",
        "settings.audio.noise_suppression": "Background noise suppression",
        "settings.audio.noise_reduction_db": "Noise reduction (dB)",
        "settings.audio.noise_suppression.tip": "A spectral gate driven by the running noise floor; it applies to the microphone and mixed sources so room tone stops holding the VAD open.",
        "settings.audio.burst_filter": "Burst (gunshot) filter",
        "settings.audio.burst_max_ms": "Maximum burst length (ms)",
        "settings.audio.burst_margin_db": "Burst threshold margin (dB)",
        "settings.audio.burst_filter.tip": "Gunshots are far shorter than speech: blocks whose energy jumps this far above the running median for no longer than this duration are muted.",
        "settings.pause_on_full": "Pause capture when the queue is full",
        "settings.pause_on_full.tip": "Stop feeding audio while the recogniser queue is saturated and resume automatically once it drains: dropping a short stretch beats letting the delay grow without bound.",
        "overlay.status.capture_paused": "Queue full - capture paused",
        "overlay.status.translate_timeout": "Translation timed out",
        "overlay.status.translate_failed": "Translation service busy",
        "overlay.status.busy": "Audio backlog - a segment was skipped",
        "settings.subtitle_queue_max": "Max queued subtitles per channel",
        "settings.subtitle_timeout": "Subtitle display timeout (s)",
        "settings.btn.save": "Save",
        "settings.btn.cancel": "Cancel",
        "settings.btn.close": "Close",
        "settings.btn.browse": "Browse…",
        "settings.btn.reset_defaults": "Restore defaults",
        "settings.btn.restart_audio": "Restart audio",
        "settings.btn.restart_ocr": "Restart OCR",
        "settings.btn.restart_tasks": "Reset background tasks",
        "settings.reset_done": "Default configuration restored.",
        "settings.tasks_reset": "Background tasks reset, no restart required.",
        "dialog.info.title": "Notice",
        "dialog.error.title": "Error",
        "dialog.hotkey_conflict.title": "Hotkey conflict",
        "dialog.hotkey_conflict.body": "Hotkeys must differ: {keys}",
        "dialog.hotkey_register_fail.title": "Hotkey registration failed",
        "dialog.hotkey_register_fail.body": (
            "Hotkey {key} is already taken by another program. Please choose another."
        ),
        "dialog.config_reset.title": "Configuration reset",
        "dialog.config_reset.body": (
            "config.json was missing or corrupted; defaults have been restored."
        ),
        "dialog.permission.title": "Insufficient permission",
        "dialog.permission.body": "Cannot read or write the config file: {detail}",
        "status.audio_ok": "Audio OK",
        "status.audio_busy": "Processing",
        "status.audio_error": "Error",
        "status.audio_reconnecting": "Reconnecting audio",
        "status.ocr_running": "OCR capturing",
        "overlay.asr_placeholder": "Waiting for speech…",
        "overlay.ocr_placeholder": "Hold the area OCR hotkey to read on-screen text",
        "overlay.tts_placeholder": "(TTS reserved)",
        "translate.unavailable": "Translation service unavailable, please retry later.",
        "translate.failed": "Translation failed",
        "translate.original_prefix": "Source: ",
        "log.capture_started": "[audio] loopback capture started on {device}",
        "log.capture_failed": "[audio] capture error: {detail}",
        "log.capture_reconnect": "[audio] reconnecting ({attempt}/{total})…",
        "log.capture_reconnect_failed": "[audio] reconnect failed, audio module disabled.",
        "log.silence_warning": "[audio] long silence detected, no ASR task submitted.",
        "log.ocr_dropped_frame": "[ocr] backlog exceeded, dropped a stale frame.",
        "log.ocr_failed": "[ocr] frame discarded: {detail}",
        "log.task_error": "[task] {name} failed: {detail}",
        "log.hotkey_registered": "[hotkey] registered {name} = {key}",
        "log.hotkey_release": "[hotkey] released all registered hotkeys.",
        "log.shutdown": "[app] shutting down, stopping background modules…",
        "v02.todo": "This feature will be available in v0.2.",
        "v02.slang": "Slang dictionary replacement",
        "v02.log_export": "Log export",
        "v02.pause_on_blur": "Pause when unfocused",
        "v02.model_download": "One-click Whisper model download",
        "v02.multi_source": "Multiple translation sources",
        "v02.vad_slider": "VAD sensitivity slider",
        "v02.area_preset": "OCR selection presets",
        "v02.subtitle_history": "Subtitle history",
        "v02.subtitle_anim": "Subtitle animations",
        "v02.streaming_asr": "Incremental streaming ASR",
        "v02.window_tracking": "Window-tracking OCR",
        "v02.multimonitor": "Multi-monitor OCR",
    },
}


# --------------------------------------------------------------------------- #
# Config manager
# --------------------------------------------------------------------------- #

class ConfigManager:
    """
    Thread-safe configuration accessor backed by a single JSON file.

    Usage:
        cfg = ConfigManager()
        status, issues = cfg.load()
        size = cfg.get("subtitle.font_size")
        cfg.set("subtitle.font_size", 28)
        cfg.save()
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = os.path.abspath(path) if path else resolve_config_path()
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._status: str = LOAD_DEFAULT
        self._issues: List[Tuple[str, str]] = []
        self._lock = threading.RLock()

    # -- properties -------------------------------------------------------- #

    @property
    def path(self) -> str:
        """Absolute path of the backing config.json."""
        return self._path

    @property
    def status(self) -> str:
        """Result of the most recent load()."""
        return self._status

    @property
    def issues(self) -> List[Tuple[str, str]]:
        """List of (dotted_key, reason) collected during the most recent load."""
        return list(self._issues)

    @property
    def ui_language(self) -> str:
        with self._lock:
            language = self._data.get("ui_language", DEFAULT_LANGUAGE)
        return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    # -- loading / saving -------------------------------------------------- #

    def load(self, repair: bool = True) -> Tuple[str, List[Tuple[str, str]]]:
        """
        Read and validate the config file.

        Returns (status, issues) where status is one of LOAD_OK / LOAD_DEFAULT /
        LOAD_PARTIAL / LOAD_CORRUPT. When *repair* is True the default file is
        written back after a missing or corrupted file, so the next launch is
        clean. This method never raises for bad input.
        """
        issues: List[Tuple[str, str]] = []
        raw: Optional[Dict[str, Any]] = None
        status = LOAD_OK
        existed = os.path.isfile(self._path)

        if existed:
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    parsed = json.load(handle)
                if not isinstance(parsed, dict):
                    raise ValueError("root element is not a JSON object")
                raw = parsed
            except (OSError, ValueError, TypeError) as exc:
                raw = None
                status = LOAD_CORRUPT
                issues.append(("<root>", str(exc)))
        else:
            status = LOAD_DEFAULT

        if raw is None:
            merged = copy.deepcopy(DEFAULT_CONFIG)
        else:
            merged = _merge_validate(DEFAULT_CONFIG, raw, _SCHEMA, "", issues)

        # Keep the on-disk schema version current without clobbering future keys.
        merged["version"] = APP_VERSION

        with self._lock:
            self._data = merged
            if issues and status == LOAD_OK:
                status = LOAD_PARTIAL
            self._status = status
            self._issues = issues

        if repair and status in (LOAD_DEFAULT, LOAD_CORRUPT):
            self.save()

        return status, list(issues)

    def save(self) -> bool:
        """
        Write the config atomically (temp file + os.replace).

        Returns True on success. Permission or disk errors are swallowed and
        reported to the caller so the UI can show a dialog instead of crashing.
        """
        with self._lock:
            payload = copy.deepcopy(self._data)
        folder = os.path.dirname(self._path)
        try:
            if folder:
                os.makedirs(folder, exist_ok=True)
            handle_fd, tmp_path = tempfile.mkstemp(
                prefix="config_", suffix=".tmp", dir=folder or None
            )
            try:
                with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self._path)
            except BaseException:
                # Best-effort cleanup; never leave a half-written temp file.
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                raise
            return True
        except (OSError, ValueError, TypeError):
            return False

    def reset(self, save: bool = True) -> bool:
        """Restore every setting to its default value."""
        with self._lock:
            self._data = copy.deepcopy(DEFAULT_CONFIG)
            self._status = LOAD_DEFAULT
            self._issues = []
        return self.save() if save else True

    # -- generic access ---------------------------------------------------- #

    def as_dict(self) -> Dict[str, Any]:
        """Deep copy of the whole configuration tree."""
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Read a value by dotted key, e.g. get("subtitle.font_size")."""
        with self._lock:
            node: Any = self._data
            for part in dotted_key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, dotted_key: str, value: Any, save: bool = False) -> bool:
        """
        Write a value by dotted key. The value is validated against the same
        schema used on load, so callers cannot persist an illegal value.
        Returns True when the stored value equals the requested one.
        """
        parts = [p for p in dotted_key.split(".") if p]
        if not parts:
            return False

        with self._lock:
            # Walk the schema alongside the data tree to find the leaf rule.
            schema: Any = _SCHEMA
            for part in parts[:-1]:
                schema = schema.get(part, {}) if isinstance(schema, dict) else {}
            leaf = parts[-1]
            spec = schema.get(leaf) if isinstance(schema, dict) else None

            node = self._data
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child

            if isinstance(spec, tuple):
                kind, rule = spec
                default = None
                current = node.get(leaf, _MISSING)
                if current is not _MISSING:
                    default = current
                coerced, ok, _reason = _coerce(default, value, kind, rule)
                if not ok:
                    return False
                node[leaf] = coerced
            else:
                node[leaf] = value

        if save:
            self.save()
        return True

    def update(self, values: Dict[str, Any], save: bool = True) -> bool:
        """Apply several dotted-key updates; returns False if any value was rejected."""
        accepted = True
        for key, value in values.items():
            if not self.set(key, value):
                accepted = False
        if save:
            self.save()
        return accepted

    # -- localization ------------------------------------------------------ #

    def t(self, key: str, **fmt: Any) -> str:
        """
        Look up a UI string for the configured language.

        Falls back to English and finally to the raw key, so a missing entry can
        never raise inside a paint or dialog path.
        """
        language = self.ui_language
        text = lang_data.get(language, {}).get(key)
        if text is None:
            text = lang_data.get("en", {}).get(key, key)
        if fmt:
            try:
                return text.format(**fmt)
            except (KeyError, IndexError, ValueError):
                return text
        return text


# --------------------------------------------------------------------------- #
# Module-level singleton helpers
# --------------------------------------------------------------------------- #

_INSTANCE: Optional[ConfigManager] = None
# Re-entrant on purpose: get_config() holds this lock and then calls
# init_config(), which takes it again. A plain Lock() self-deadlocks there, so
# the very first get_config() call would hang the process forever.
_INSTANCE_LOCK = threading.RLock()


def init_config(path: Optional[str] = None, repair: bool = True) -> ConfigManager:
    """
    Create (or return) the process-wide ConfigManager and load it.

    The returned manager exposes .status / .issues so main.py can decide whether
    to show the "configuration reset" dialog.
    """
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = ConfigManager(path)
            _INSTANCE.load(repair=repair)
        return _INSTANCE


def get_config() -> ConfigManager:
    """Return the shared ConfigManager, initializing it on first use."""
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            return init_config()
        return _INSTANCE


def reset_singleton() -> None:
    """Drop the shared instance (used by tests and by a full app restart)."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
