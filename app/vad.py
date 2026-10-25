"""Low-latency utterance segmentation with an energy VAD.

The goal is not perfect speech detection - it is to hand Whisper complete
*sentences* instead of arbitrary 8-second chunks.  A short silence closes the
utterance; a long runaway segment is force-flushed so latency stays bounded.

Silero VAD can replace the energy detector later without changing the
pipeline contract (``feed(frame, t) -> list[Utterance]``).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Utterance:
    id: int
    audio: np.ndarray
    start_t: float  # monotonic timestamp of the first sample
    end_t: float  # monotonic timestamp of the last *voiced* sample
    duration: float  # seconds of audio, including pre-roll and tail
    forced: bool = False


class EnergyVAD:
    def __init__(
        self,
        sample_rate: int = 16_000,
        frame_ms: int = 20,
        *,
        multiplier: float = 3.0,
        floor: float = 0.004,
        start_ms: int = 60,
        end_silence_ms: int = 500,
        pre_roll_ms: int = 300,
        max_utterance_s: float = 12.0,
        min_utterance_s: float = 0.45,
        keep_tail_ms: int = 150,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_s = frame_ms / 1000.0
        self.multiplier = multiplier
        self.floor = floor
        self.start_frames = max(1, int(round(start_ms / frame_ms)))
        self.end_silence_frames = max(1, int(round(end_silence_ms / frame_ms)))
        self.max_frames = max(1, int(round(max_utterance_s / self.frame_s)))
        self.min_duration = min_utterance_s
        self.tail_frames = max(0, int(round(keep_tail_ms / frame_ms)))

        self._pre: deque[np.ndarray] = deque(
            maxlen=max(1, int(round(pre_roll_ms / frame_ms)))
        )
        self._frames: list[np.ndarray] = []
        self._noise = 0.0015
        self._in_speech = False
        self._voice_streak = 0
        self._silence_run = 0
        self._last_voice_idx = 0
        self._last_voice_t = 0.0
        self._start_t = 0.0
        self._id = 0

    # -- read-only state used by the pipeline --------------------------------
    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def current_id(self) -> int:
        return self._id

    @property
    def current_start_t(self) -> float:
        return self._start_t

    @property
    def last_voice_t(self) -> float:
        return self._last_voice_t

    @property
    def duration(self) -> float:
        return len(self._frames) * self.frame_s

    def current_audio(self, max_seconds: float | None = None) -> np.ndarray:
        if not self._frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self._frames)
        if max_seconds is not None:
            limit = int(max_seconds * self.sample_rate)
            if len(audio) > limit:
                audio = audio[-limit:]
        return np.ascontiguousarray(audio, dtype=np.float32)

    # -- main entry point ----------------------------------------------------
    def feed(self, frame: np.ndarray, t: float) -> list[Utterance]:
        """Consume one frame.

        ``t`` is a monotonic clock value for the *end* of the frame.
        Returns zero or more completed utterances.
        """
        frame = np.asarray(frame, dtype=np.float32)
        if frame.size == 0:
            return []

        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        if rms < self._noise * 2.5:
            self._noise = 0.95 * self._noise + 0.05 * rms
        threshold = max(self.floor, self._noise * self.multiplier)
        voiced = rms >= threshold

        if not self._in_speech:
            self._pre.append(frame)
            if not voiced:
                self._voice_streak = 0
                return []
            self._voice_streak += 1
            if self._voice_streak < self.start_frames:
                return []
            self._begin(t)
            return []

        self._frames.append(frame)
        if voiced:
            self._silence_run = 0
            self._last_voice_idx = len(self._frames) - 1
            self._last_voice_t = t
        else:
            self._silence_run += 1

        silence_done = self._silence_run >= self.end_silence_frames
        too_long = len(self._frames) >= self.max_frames
        if not silence_done and not too_long:
            return []

        utterance = self._finish(forced=too_long and not silence_done)
        return [utterance] if utterance else []

    # -- internals -----------------------------------------------------------
    def _begin(self, t: float) -> None:
        self._frames = list(self._pre)
        self._pre.clear()
        self._in_speech = True
        self._silence_run = 0
        self._last_voice_idx = len(self._frames) - 1
        self._last_voice_t = t
        self._start_t = t - len(self._frames) * self.frame_s
        self._id += 1

    def _finish(self, *, forced: bool) -> Utterance | None:
        end_idx = min(len(self._frames), self._last_voice_idx + 1 + self.tail_frames)
        frames = self._frames[:end_idx]
        audio = (
            np.concatenate(frames).astype(np.float32, copy=False)
            if frames
            else np.zeros(0, dtype=np.float32)
        )
        duration = len(audio) / float(self.sample_rate)
        utterance = Utterance(
            id=self._id,
            audio=np.ascontiguousarray(audio),
            start_t=self._start_t,
            end_t=self._last_voice_t,
            duration=duration,
            forced=forced,
        )

        self._in_speech = False
        self._frames = []
        self._pre.clear()
        self._voice_streak = 0
        self._silence_run = 0
        self._last_voice_idx = 0

        if duration < self.min_duration:
            return None
        return utterance
