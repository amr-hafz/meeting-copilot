"""Capture -> VAD -> partial/final transcription pipeline.

Threading model
---------------
* capture thread: WASAPI loopback -> audio queue (drops oldest frames if full)
* main thread: queue -> VAD -> schedules jobs
* ASR thread: one job at a time; partials are coalesced (latest wins),
  finalized utterances are queued so no sentence is ever skipped

Audio is only kept in memory.  Nothing is written to disk here.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal

import numpy as np

from app.asr import AsrError, WhisperASR
from app.audio import LoopbackSource
from app.config import Settings
from app.vad import EnergyVAD, Utterance

EventKind = Literal["status", "partial", "final", "error"]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    text: str = ""
    utt_id: int = 0
    start_t: float = 0.0
    end_t: float = 0.0
    latency_s: float = 0.0
    inference_s: float = 0.0
    audio_s: float = 0.0
    wall: datetime | None = None
    forced: bool = False


@dataclass(frozen=True)
class _Job:
    kind: Literal["partial", "final"]
    utt_id: int
    audio: np.ndarray
    seq: int
    start_t: float
    end_t: float
    forced: bool = False


class StreamPipeline:
    def __init__(self, settings: Settings, on_event: Callable[[Event], None]) -> None:
        self.settings = settings
        self.on_event = on_event
        self.vad = EnergyVAD(
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
        self.asr = WhisperASR(
            model=settings.model,
            compute_device=settings.compute_device,
            compute_type=settings.compute_type,
            beam_size=settings.beam_size,
        )

        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=250)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finals: deque[_Job] = deque()
        self._partial: _Job | None = None
        self._busy = False
        self._seq = 0
        self._final_seq: dict[int, int] = {}
        self._last_partial_text: dict[int, str] = {}
        self._last_partial_enq = 0.0

        self._t0: float | None = None
        self._wall0: datetime | None = None
        self._last_frame_t = 0.0
        self._dropped = 0
        self._capture: threading.Thread | None = None
        self._asr_thread: threading.Thread | None = None

        self._latencies: list[float] = []
        self._inferences: list[float] = []
        self._rtfs: list[float] = []

    # ------------------------------------------------------------------ API
    def run(self, max_seconds: float | None = None) -> None:
        self._emit(Event("status", text="Loading Whisper model (first run downloads it)..."))
        try:
            self.asr.load()
        except AsrError as exc:
            self._emit(Event("error", text=str(exc)))
            return

        for note in self.asr.notes:
            self._emit(Event("status", text=note))
        self._emit(
            Event(
                "status",
                text=f"Model ready: {self.asr.label} (loaded in {self.asr.load_s:.1f}s)",
            )
        )

        self._capture = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        self._asr_thread = threading.Thread(target=self._asr_loop, name="asr", daemon=True)
        self._capture.start()
        self._asr_thread.start()

        started = time.monotonic()
        interrupted = False
        try:
            while not self._stop.is_set():
                if max_seconds is not None and time.monotonic() - started >= max_seconds:
                    break
                try:
                    frame = self._audio_q.get(timeout=0.2)
                except queue.Empty:
                    if not self._capture.is_alive() and self._audio_q.empty():
                        break
                    continue

                now = time.monotonic()
                self._last_frame_t = now
                if self._t0 is None:
                    self._t0 = now
                    self._wall0 = datetime.now()
                for utterance in self.vad.feed(frame, now):
                    self._handle_utterance(utterance)
                self._maybe_schedule_partial(now)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            if not interrupted:
                self._flush_vad()
                self._wait_for_asr(timeout=6.0)
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._capture, self._asr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)

    def request_stop(self) -> None:
        """Ask the worker to stop without blocking the GUI thread."""
        self._stop.set()
        with self._lock:
            self._partial = None

    def stats(self) -> dict[str, float | int | str]:
        latencies = sorted(self._latencies)

        def percentile(p: float) -> float:
            if not latencies:
                return 0.0
            index = min(len(latencies) - 1, max(0, int(round(p * (len(latencies) - 1)))))
            return latencies[index]

        def mean(values: list[float]) -> float:
            return sum(values) / len(values) if values else 0.0

        return {
            "utterances": len(latencies),
            "latency_avg_s": mean(latencies),
            "latency_p50_s": percentile(0.50),
            "latency_p95_s": percentile(0.95),
            "inference_avg_s": mean(self._inferences),
            "rtf_avg": mean(self._rtfs),
            "dropped_frames": self._dropped,
            "model": self.asr.label,
        }

    # -------------------------------------------------------------- capture
    def _capture_loop(self) -> None:
        try:
            with LoopbackSource(
                self.settings.audio_device,
                sample_rate=self.settings.sample_rate,
                frame_ms=self.settings.frame_ms,
            ) as source:
                self._emit(
                    Event(
                        "status",
                        text=f"Capturing loopback: {source.name} "
                        f"({source.rate} Hz, {source.channels} ch)",
                    )
                )
                self._emit(
                    Event(
                        "status",
                        text="Listening for speech...",
                    )
                )
                for frame in source.frames():
                    if self._stop.is_set():
                        break
                    try:
                        self._audio_q.put_nowait(frame)
                    except queue.Full:
                        self._dropped += 1
                        try:
                            self._audio_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._audio_q.put_nowait(frame)
                        except queue.Full:
                            pass
        except Exception as exc:  # noqa: BLE001 - driver errors are opaque
            self._emit(Event("error", text=str(exc)))
            self._stop.set()

    # --------------------------------------------------------------- speech
    def _handle_utterance(self, utterance: Utterance) -> None:
        if utterance.duration < self.settings.min_utterance_s:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._final_seq[utterance.id] = seq
            # A final is now waiting; drop any stale preview so the ASR
            # worker goes straight to the sentence the user actually needs.
            self._partial = None
            self._finals.append(
                _Job(
                    kind="final",
                    utt_id=utterance.id,
                    audio=utterance.audio,
                    seq=seq,
                    start_t=utterance.start_t,
                    end_t=utterance.end_t,
                    forced=utterance.forced,
                )
            )

    def _maybe_schedule_partial(self, now: float) -> None:
        if not self.settings.partials or not self.vad.in_speech:
            return
        if now - self._last_partial_enq < self.settings.partial_interval_s:
            return
        if self.vad.duration < self.settings.partial_min_s:
            return
        with self._lock:
            if self._finals:
                return

        audio = self.vad.current_audio(self.settings.partial_window_s)
        if len(audio) < self.settings.partial_min_s * self.settings.sample_rate:
            return

        self._last_partial_enq = now
        uid = self.vad.current_id
        with self._lock:
            seq = self._final_seq.get(uid, 0)
            self._partial = _Job(
                kind="partial",
                utt_id=uid,
                audio=audio,
                seq=seq,
                start_t=self.vad.current_start_t,
                end_t=self.vad.last_voice_t,
            )

    def _flush_vad(self) -> None:
        """Push silence through the VAD so the last sentence is finalized."""
        frame_samples = int(self.settings.sample_rate * self.settings.frame_ms / 1000)
        silence = np.zeros(frame_samples, dtype=np.float32)
        frames = int((self.settings.end_silence_ms + 300) / self.settings.frame_ms)
        now = self._last_frame_t or time.monotonic()
        for _ in range(frames):
            now += self.settings.frame_ms / 1000.0
            for utterance in self.vad.feed(silence, now):
                self._handle_utterance(utterance)

    def _wait_for_asr(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                idle = not self._finals and self._partial is None and not self._busy
            if idle:
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------ ASR
    def _asr_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                job = self._finals.popleft() if self._finals else None
                if job is None and self._partial is not None:
                    job = self._partial
                    self._partial = None
                self._busy = job is not None

            if job is None:
                time.sleep(0.02)
                continue

            try:
                result = self.asr.transcribe(job.audio, partial=(job.kind == "partial"))
            except Exception as exc:  # noqa: BLE001
                self._emit(Event("error", text=f"Transcription failed: {exc}"))
                with self._lock:
                    self._busy = False
                continue

            now = time.monotonic()
            with self._lock:
                stale = job.seq < self._final_seq.get(job.utt_id, 0)
                if job.kind == "partial":
                    if self._last_partial_text.get(job.utt_id) == result.text:
                        stale = True
                    else:
                        self._last_partial_text[job.utt_id] = result.text
                self._busy = False

            if stale or not result.text:
                if job.kind == "final":
                    self._last_partial_text.pop(job.utt_id, None)
                continue

            latency = now - job.end_t
            wall = self._wall_at(job.start_t)

            if job.kind == "partial":
                self._emit(
                    Event(
                        "partial",
                        text=result.text,
                        utt_id=job.utt_id,
                        start_t=job.start_t,
                        end_t=job.end_t,
                        latency_s=latency,
                        inference_s=result.inference_s,
                        audio_s=result.audio_s,
                        wall=wall,
                    )
                )
            else:
                self._latencies.append(latency)
                self._inferences.append(result.inference_s)
                self._rtfs.append(result.rtf)
                self._last_partial_text.pop(job.utt_id, None)
                self._emit(
                    Event(
                        "final",
                        text=result.text,
                        utt_id=job.utt_id,
                        start_t=job.start_t,
                        end_t=job.end_t,
                        latency_s=latency,
                        inference_s=result.inference_s,
                        audio_s=result.audio_s,
                        wall=wall,
                        forced=job.forced,
                    )
                )

    # -------------------------------------------------------------- helpers
    def _wall_at(self, t: float) -> datetime | None:
        if self._t0 is None or self._wall0 is None:
            return None
        return self._wall0 + timedelta(seconds=t - self._t0)

    def _emit(self, event: Event) -> None:
        try:
            self.on_event(event)
        except Exception:
            pass
