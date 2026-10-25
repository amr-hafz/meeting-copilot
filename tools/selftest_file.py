"""Offline pipeline test: WAV -> VAD -> Whisper, with RTF/latency numbers.

Usage:
    python tools/selftest_file.py samples/meeting_test.wav
    python tools/selftest_file.py samples/meeting_test.wav --model small.en
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import cuda_bootstrap  # noqa: E402

cuda_bootstrap.enable()

from app.asr import AsrError, WhisperASR  # noqa: E402
from app.config import Settings  # noqa: E402
from app.vad import EnergyVAD  # noqa: E402


def load_audio_16k(path: Path) -> np.ndarray:
    """Decode any audio file to mono float32 at 16 kHz."""
    try:
        from faster_whisper.audio import decode_audio

        return np.ascontiguousarray(
            decode_audio(str(path), sampling_rate=16_000), dtype=np.float32
        )
    except TypeError:
        # PyAV 19 incompatibility; ffmpeg is installed on this machine.
        process = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        return np.frombuffer(process.stdout, dtype=np.float32).copy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--model", default="auto")
    parser.add_argument("--compute-device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--compute-type", default=None)
    parser.add_argument("--end-silence-ms", type=int, default=500)
    parser.add_argument("--max-utterance", type=float, default=12.0)
    args = parser.parse_args()

    if not args.wav.exists():
        print(f"File not found: {args.wav}", file=sys.stderr)
        return 1

    settings = Settings(
        model=args.model,
        compute_device=args.compute_device,
        compute_type=args.compute_type,
        end_silence_ms=args.end_silence_ms,
        max_utterance_s=args.max_utterance,
    )

    audio = load_audio_16k(args.wav)
    total_audio_s = len(audio) / 16_000
    print(f"Audio: {args.wav.name} ({total_audio_s:.1f}s)")

    asr = WhisperASR(
        model=settings.model,
        compute_device=settings.compute_device,
        compute_type=settings.compute_type,
        beam_size=settings.beam_size,
    )
    started = time.perf_counter()
    try:
        asr.load()
    except AsrError as exc:
        print(f"Model load failed:\n{exc}", file=sys.stderr)
        return 1
    load_s = time.perf_counter() - started
    for note in asr.notes:
        print(f"note: {note}")
    print(f"Model: {asr.label} (loaded in {load_s:.1f}s)\n")

    vad = EnergyVAD(
        sample_rate=settings.sample_rate,
        frame_ms=settings.frame_ms,
        multiplier=settings.vad_multiplier,
        floor=settings.vad_floor,
        start_ms=settings.start_ms,
        end_silence_ms=settings.end_silence_ms,
        pre_roll_ms=settings.pre_roll_ms,
        max_utterance_s=settings.max_utterance_s,
        min_utterance_s=settings.min_utterance_s,
    )

    frame_samples = int(settings.sample_rate * settings.frame_ms / 1000)
    utterances = []
    for offset in range(0, len(audio), frame_samples):
        chunk = audio[offset : offset + frame_samples]
        if len(chunk) < frame_samples:
            chunk = np.pad(chunk, (0, frame_samples - len(chunk)))
        t_end = (offset + len(chunk)) / 16_000.0
        utterances.extend(vad.feed(chunk, t_end))

    # Flush the last sentence with ~1s of silence.
    t_end = len(audio) / 16_000.0
    silence = np.zeros(frame_samples, dtype=np.float32)
    for _ in range(int(1.0 / (settings.frame_ms / 1000))):
        t_end += settings.frame_ms / 1000
        utterances.extend(vad.feed(silence, t_end))

    total_inference = 0.0
    total_speech = 0.0
    for index, utterance in enumerate(utterances, start=1):
        result = asr.transcribe(utterance.audio)
        total_inference += result.inference_s
        total_speech += result.audio_s
        forced = " [forced split]" if utterance.forced else ""
        print(
            f"[{index:02d}] {utterance.start_t:6.2f}s-{utterance.end_t:6.2f}s "
            f"({result.audio_s:4.1f}s audio){forced}"
        )
        print(f"     {result.text or '(empty)'}")
        print(
            f"     asr {result.inference_s:.2f}s · RTF {result.rtf:.2f}x · "
            f"would appear ~{result.inference_s + settings.end_silence_ms / 1000:.1f}s "
            "after the sentence ends"
        )

    print("\n" + "-" * 62)
    print(f"Utterances: {len(utterances)}")
    print(f"Speech captured by VAD: {total_speech:.1f}s of {total_audio_s:.1f}s")
    print(f"Total ASR inference: {total_inference:.2f}s (RTF {total_inference / max(total_speech, 1e-9):.2f}x)")
    print("-" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
