"""Synthetic sanity checks for the energy VAD (no audio hardware needed)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.vad import EnergyVAD  # noqa: E402

SR = 16_000
FRAME = 320  # 20 ms


def tone(seconds: float, amplitude: float = 0.2) -> np.ndarray:
    t = np.arange(int(SR * seconds), dtype=np.float32) / SR
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


def run(signal: np.ndarray, **kwargs) -> list:
    vad = EnergyVAD(sample_rate=SR, frame_ms=20, **kwargs)
    found = []
    for offset in range(0, len(signal), FRAME):
        chunk = signal[offset : offset + FRAME]
        if len(chunk) < FRAME:
            chunk = np.pad(chunk, (0, FRAME - len(chunk)))
        found.extend(vad.feed(chunk, (offset + len(chunk)) / SR))
    for extra in range(60):  # 1.2s of trailing silence to flush
        found.extend(vad.feed(np.zeros(FRAME, np.float32), (len(signal) + (extra + 1) * FRAME) / SR))
    return found


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
    if not condition:
        raise SystemExit(1)


def main() -> int:
    # Two sentences separated by 1 second of silence.
    signal = np.concatenate([silence(0.5), tone(1.6), silence(1.0), tone(0.9), silence(1.0)])
    utterances = run(signal)
    check("two utterances detected", len(utterances) == 2, f"got {len(utterances)}")
    check(
        "first utterance keeps pre-roll and tail",
        1.6 < utterances[0].duration < 2.4,
        f"{utterances[0].duration:.2f}s",
    )
    check("no forced split for short speech", not any(u.forced for u in utterances))

    # Runaway speech must be force-flushed (bounded latency).
    long_signal = np.concatenate([silence(0.3), tone(15.0), silence(1.0)])
    long_utterances = run(long_signal, max_utterance_s=6.0)
    check(
        "long speech is split at max_utterance_s",
        len(long_utterances) >= 2 and long_utterances[0].forced,
        f"{len(long_utterances)} chunks",
    )

    # Quiet speech below the absolute floor must be ignored.
    quiet = np.concatenate([silence(0.3), tone(1.5, amplitude=0.002), silence(0.8)])
    check("quiet noise ignored", len(run(quiet)) == 0)

    print("\nAll VAD checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
