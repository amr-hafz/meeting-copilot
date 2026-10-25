"""WASAPI loopback capture (what you *hear*, not your microphone).

Windows only.  ``soundcard`` exposes loopback "microphones" that mirror an
output device, so whatever Teams/Zoom/Meet plays through the speakers or
headphones is captured without touching the default microphone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np
import soundcard as sc

TARGET_RATE = 16_000


class AudioError(RuntimeError):
    """Raised when the loopback device cannot be opened or dies mid-call."""


@dataclass(frozen=True)
class LoopbackDevice:
    name: str
    id: str
    is_default: bool


@dataclass(frozen=True)
class OutputDevice:
    name: str
    is_default: bool


def _norm(name: str) -> str:
    text = " ".join(name.lower().split())
    for suffix in (" [loopback]", " (loopback)"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def output_devices() -> list[OutputDevice]:
    default = sc.default_speaker()
    default_key = _norm(str(default.name)) if default else None
    result: list[OutputDevice] = []
    for speaker in sc.all_speakers():
        name = str(speaker.name)
        result.append(OutputDevice(name=name, is_default=_norm(name) == default_key))
    return result


def loopback_devices() -> list[LoopbackDevice]:
    default = sc.default_speaker()
    default_key = _norm(str(default.name)) if default else None
    seen: set[str] = set()
    result: list[LoopbackDevice] = []
    for mic in sc.all_microphones(include_loopback=True):
        if not getattr(mic, "isloopback", False):
            continue
        name = str(mic.name)
        mic_id = str(mic.id)
        if mic_id in seen:
            continue
        seen.add(mic_id)
        key = _norm(name)
        is_default = bool(default_key) and (
            key == default_key or key.startswith(default_key) or default_key.startswith(key)
        )
        result.append(LoopbackDevice(name=name, id=mic_id, is_default=is_default))
    return result


def select_loopback(query: str | None = None) -> LoopbackDevice:
    devices = loopback_devices()
    if not devices:
        raise AudioError(
            "No WASAPI loopback device found. Make sure an output device "
            "(speakers or headphones) is enabled in Windows sound settings."
        )

    if query:
        needle = _norm(query)
        exact = [d for d in devices if _norm(d.name) == needle]
        if exact:
            return exact[0]
        partial = [d for d in devices if needle in _norm(d.name)]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = ", ".join(d.name for d in partial)
            raise AudioError(f"Audio device '{query}' is ambiguous: {names}")
        names = ", ".join(d.name for d in devices)
        raise AudioError(f"No loopback device matches '{query}'. Available: {names}")

    defaults = [d for d in devices if d.is_default]
    return (defaults or devices)[0]


def _to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    """Downmix/resample a mono float32 signal to 16 kHz."""
    if rate == TARGET_RATE:
        return samples.astype(np.float32, copy=False)

    if rate % TARGET_RATE == 0:
        factor = rate // TARGET_RATE
        usable = len(samples) - (len(samples) % factor)
        if usable <= 0:
            return np.zeros(0, dtype=np.float32)
        return samples[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)

    # Rare non-integer ratio (e.g. 44.1 kHz): linear interpolation is good
    # enough for speech ASR and avoids a scipy dependency.
    duration = len(samples) / float(rate)
    out_len = int(round(duration * TARGET_RATE))
    if out_len <= 1:
        return np.zeros(0, dtype=np.float32)
    old_x = np.arange(len(samples), dtype=np.float64) / float(rate)
    new_x = np.arange(out_len, dtype=np.float64) / float(TARGET_RATE)
    return np.interp(new_x, old_x, samples).astype(np.float32)


class LoopbackSource:
    """Context manager yielding fixed-size mono frames at 16 kHz."""

    def __init__(
        self,
        device_query: str | None = None,
        sample_rate: int = TARGET_RATE,
        frame_ms: int = 20,
    ) -> None:
        self.device_query = device_query
        self.requested_rate = sample_rate
        self.frame_ms = frame_ms
        self.rate = sample_rate
        self.channels = 2
        self.name = ""
        self.id = ""
        self._rec = None
        self._frame_samples = 0

    def __enter__(self) -> "LoopbackSource":
        device = select_loopback(self.device_query)
        mic = sc.get_microphone(id=device.id, include_loopback=True)
        if mic is None:
            raise AudioError(f"Could not open loopback device '{device.name}'.")

        attempts = [
            # Prefer the native shared-mode rate: letting soundcard resample
            # 48k -> 16k caused "data discontinuity" warnings on Realtek.
            (48_000, 2),
            (48_000, 1),
            (44_100, 2),
            (self.requested_rate, 2),
            (self.requested_rate, 1),
        ]
        seen: set[tuple[int, int]] = set()
        last_error: Exception | None = None
        for rate, channels in attempts:
            if (rate, channels) in seen:
                continue
            seen.add((rate, channels))
            try:
                recorder = mic.recorder(samplerate=rate, channels=channels)
                recorder.__enter__()
            except Exception as exc:  # noqa: BLE001 - driver errors are opaque
                last_error = exc
                continue
            self._rec = recorder
            self.rate = rate
            self.channels = channels
            break
        if self._rec is None:
            raise AudioError(
                f"Could not open loopback device '{device.name}': {last_error}"
            )

        self.name = device.name
        self.id = device.id
        self._frame_samples = max(1, int(self.rate * self.frame_ms / 1000))
        # Read in ~100 ms blocks. Tiny 20 ms reads starve when the ASR thread
        # holds the GIL and WASAPI then drops samples ("data discontinuity").
        self._block_samples = max(self._frame_samples, int(self.rate * 0.1))
        self._out_frame_samples = max(1, int(TARGET_RATE * self.frame_ms / 1000))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._rec is not None:
            try:
                self._rec.__exit__(exc_type, exc, tb)
            except Exception:
                pass
            self._rec = None

    def frames(self) -> Iterator[np.ndarray]:
        """Yield 16 kHz mono float32 frames forever (or raise AudioError)."""
        if self._rec is None:
            raise AudioError("LoopbackSource is not open; use it as a context manager.")

        pending = np.empty(0, dtype=np.float32)
        while True:
            try:
                data = self._rec.record(numframes=self._block_samples)
            except Exception as exc:  # noqa: BLE001
                raise AudioError(
                    "Audio capture stopped. The output device probably changed "
                    f"(headphones unplugged / Bluetooth disconnected): {exc}"
                ) from exc

            if data is None or len(data) == 0:
                continue

            samples = np.asarray(data, dtype=np.float32)
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            samples = _to_16k(samples, self.rate)
            if len(samples) == 0:
                continue

            pending = samples if len(pending) == 0 else np.concatenate((pending, samples))
            offset = 0
            while len(pending) - offset >= self._out_frame_samples:
                yield np.ascontiguousarray(
                    pending[offset : offset + self._out_frame_samples], dtype=np.float32
                )
                offset += self._out_frame_samples
            if offset:
                pending = pending[offset:].copy()
