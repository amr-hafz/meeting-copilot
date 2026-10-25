"""Runtime settings for the streaming pipeline.

Kept as a frozen dataclass so a value can never be mutated by a worker thread.
CLI arguments are merged with :func:`dataclasses.replace`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # --- audio input -------------------------------------------------------
    audio_device: str | None = None  # substring of the loopback device name
    sample_rate: int = 16_000
    frame_ms: int = 20

    # --- ASR ---------------------------------------------------------------
    model: str = "auto"  # "auto" -> small.en; prefers a larger local model if present
    compute_device: str = "auto"  # auto | cuda | cpu
    compute_type: str | None = None  # None -> float16 (cuda) / int8 (cpu)
    beam_size: int = 1

    # --- VAD / utterance segmentation -------------------------------------
    vad_multiplier: float = 3.0
    vad_floor: float = 0.004
    start_ms: int = 60
    end_silence_ms: int = 400
    pre_roll_ms: int = 300
    max_utterance_s: float = 12.0
    min_utterance_s: float = 0.45

    # --- partial (live) transcripts ----------------------------------------
    # Off by default: partials compete with finals for the single ASR worker
    # and added ~2 s to the final text in live tests.
    partials: bool = False
    partial_interval_s: float = 2.0
    partial_min_s: float = 1.2
    partial_window_s: float = 8.0

    # --- output / privacy --------------------------------------------------
    save_transcript: bool = False
    log_dir: str = "logs"
