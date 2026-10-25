"""End-to-end loopback check without a meeting.

Plays a test WAV through the default output, records it back through WASAPI
loopback, prints per-second levels, segments it with the VAD and transcribes it.

    python tools/loopback_check.py --play samples/meeting_test.wav

This deliberately makes sound. Nothing is written to disk.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import cuda_bootstrap  # noqa: E402

cuda_bootstrap.enable()

from app.asr import AsrError, WhisperASR  # noqa: E402
from app.audio import AudioError, LoopbackSource  # noqa: E402
from app.vad import EnergyVAD  # noqa: E402

SR = 16_000
FRAME = 320


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--play", type=Path, default=PROJECT_ROOT / "samples" / "meeting_test.wav")
    parser.add_argument("--audio-device", default=None, help="part of the loopback device name")
    parser.add_argument("--no-transcribe", action="store_true")
    parser.add_argument("--model", default="auto")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("This check is Windows-only.", file=sys.stderr)
        return 1
    if not args.play.exists():
        print(f"Test WAV not found: {args.play}", file=sys.stderr)
        print("Create it with:  powershell -ExecutionPolicy Bypass -File tools/make_test_speech.ps1")
        return 1

    import winsound

    duration = wav_duration(args.play)
    seconds = duration + 3.0
    print(f"Playing {args.play.name} ({duration:.1f}s) through the default output...")

    frames: list[np.ndarray] = []
    try:
        with LoopbackSource(args.audio_device, SR, 20) as source:
            print(f"Recording loopback: {source.name} ({source.rate} Hz)\n")
            time.sleep(0.3)
            winsound.PlaySound(str(args.play), winsound.SND_FILENAME | winsound.SND_ASYNC)
            deadline = time.monotonic() + seconds
            for frame in source.frames():
                frames.append(frame)
                if time.monotonic() >= deadline:
                    break
    except AudioError as exc:
        print(f"Loopback error: {exc}", file=sys.stderr)
        return 1
    finally:
        winsound.PlaySound(None, winsound.SND_PURGE)

    if not frames:
        print("No audio captured.", file=sys.stderr)
        return 1

    audio = np.concatenate(frames)
    print("Captured levels (should go up while speech is playing):")
    for offset in range(0, len(audio), SR):
        chunk = audio[offset : offset + SR]
        if not len(chunk):
            continue
        rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
        peak = float(np.max(np.abs(chunk)))
        marker = "#" * min(40, int(rms * 400))
        print(f"  {offset / SR:5.1f}s  rms {rms:.4f}  peak {peak:.3f}  {marker}")

    if args.no_transcribe:
        return 0

    print("\nRunning VAD + ASR on the captured audio...")
    vad = EnergyVAD(sample_rate=SR, frame_ms=20)
    utterances = []
    for offset in range(0, len(audio), FRAME):
        chunk = audio[offset : offset + FRAME]
        if len(chunk) < FRAME:
            chunk = np.pad(chunk, (0, FRAME - len(chunk)))
        utterances.extend(vad.feed(chunk, (offset + len(chunk)) / SR))
    for extra in range(60):
        utterances.extend(vad.feed(np.zeros(FRAME, np.float32), (len(audio) + (extra + 1) * FRAME) / SR))

    print(f"VAD found {len(utterances)} utterances.")
    if not utterances:
        print("Nothing to transcribe. Is the output device muted or too quiet?", file=sys.stderr)
        return 1

    asr = WhisperASR(model=args.model)
    try:
        asr.load()
    except AsrError as exc:
        print(f"Model load failed:\n{exc}", file=sys.stderr)
        return 1
    print(f"Model: {asr.label}\n")

    for index, utterance in enumerate(utterances, start=1):
        result = asr.transcribe(utterance.audio)
        print(f"[{index:02d}] {utterance.start_t:6.2f}s-{utterance.end_t:6.2f}s  {result.text or '(empty)'}")
        print(f"     asr {result.inference_s:.2f}s · RTF {result.rtf:.2f}x")

    print("\nIf the text above matches the script, loopback + VAD + ASR all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
