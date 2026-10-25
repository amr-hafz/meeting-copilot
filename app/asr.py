"""faster-whisper wrapper with automatic CUDA -> CPU fallback."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app import cuda_bootstrap

# Nicer first-run output on Windows and no tokenizer fork warnings.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Xet downloads were extremely slow / stalled on this connection. Plain HTTP
# with a longer timeout is slower on paper but actually finishes.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"

_MARKERS = re.compile(
    r"[\[\(]\s*(music|applause|laughter|silence|inaudible|no speech|noise)\s*[\]\)]",
    re.IGNORECASE,
)


class AsrError(RuntimeError):
    pass


@dataclass(frozen=True)
class AsrResult:
    text: str
    audio_s: float
    inference_s: float
    model: str
    device: str
    compute_type: str

    @property
    def rtf(self) -> float:
        return self.inference_s / self.audio_s if self.audio_s else 0.0


def clean_text(text: str) -> str:
    text = _MARKERS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—")
    if not re.search(r"[A-Za-z0-9]", text):
        return ""
    return text


def cuda_available() -> bool:
    cuda_bootstrap.enable()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class WhisperASR:
    """Loads one model and transcribes in-memory float32 audio at 16 kHz."""

    def __init__(
        self,
        model: str = "auto",
        compute_device: str = "auto",
        compute_type: str | None = None,
        beam_size: int = 1,
    ) -> None:
        self.requested_model = model
        self.requested_device = compute_device
        self.requested_compute_type = compute_type
        self.beam_size = beam_size

        self.model_name = ""
        self.device = ""
        self.compute_type = ""
        self.load_s = 0.0
        self.notes: list[str] = []
        self._model = None

    @property
    def label(self) -> str:
        if not self._model:
            return "not loaded"
        name = Path(self.model_name).name if os.path.isdir(self.model_name) else self.model_name
        return f"{name} · {self.device}/{self.compute_type}"

    @staticmethod
    def _find_local(model: str) -> Path | None:
        """Look for a manually downloaded model under ./models/."""
        stem = model.removeprefix("distil-") if model.startswith("distil-") else model
        candidates = [
            MODELS_DIR / model,
            MODELS_DIR / f"faster-whisper-{model}",
            MODELS_DIR / f"faster-distil-whisper-{stem}",
        ]
        for candidate in candidates:
            model_file = candidate / "model.bin"
            config_file = candidate / "config.json"
            if (
                model_file.is_file()
                and model_file.stat().st_size > 0
                and config_file.is_file()
                and config_file.stat().st_size > 0
            ):
                return candidate
        return None

    def _pick_device(self) -> str:
        if self.requested_device == "cpu":
            return "cpu"
        if self.requested_device == "cuda":
            if cuda_available():
                return "cuda"
            self.notes.append("CUDA was requested but no usable CUDA device was found; using CPU.")
            return "cpu"
        return "cuda" if cuda_available() else "cpu"

    def _pick_model(self, device: str) -> str:
        if self.requested_model != "auto":
            local = self._find_local(self.requested_model)
            if local is not None:
                if str(local) != self.requested_model:
                    self.notes.append(f"Using local model: {local}")
                return str(local)
            return self.requested_model

        preferred = (
            ("distil-large-v3", "distil-medium.en", "small.en")
            if device == "cuda"
            else ("small.en", "base.en", "tiny.en")
        )
        for name in preferred:
            local = self._find_local(name)
            if local is not None:
                self.notes.append(f"Using local model: {local}")
                return str(local)

        # Default download stays small on purpose: the better models are
        # optional manual downloads (see tools/download_model.ps1).
        return "small.en"

    def load(self) -> None:
        if self._model is not None:
            return

        started = time.perf_counter()
        cuda_bootstrap.enable()
        from faster_whisper import WhisperModel

        device = self._pick_device()
        model_name = self._pick_model(device)

        attempts: list[tuple[str, str, str]] = []
        if device == "cuda":
            attempts.append((device, self.requested_compute_type or "float16", model_name))
            if self.requested_model == "auto":
                attempts.append(("cuda", "int8_float16", "distil-medium.en"))
                attempts.append(("cpu", "int8", "small.en"))
            else:
                attempts.append(("cpu", "int8", model_name))
        else:
            attempts.append(("cpu", self.requested_compute_type or "int8", model_name))

        errors: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for dev, compute_type, candidate in attempts:
            key = (dev, compute_type, candidate)
            if key in seen:
                continue
            seen.add(key)
            try:
                model = WhisperModel(
                    candidate,
                    device=dev,
                    compute_type=compute_type,
                    num_workers=1,
                )
            except Exception as exc:  # noqa: BLE001 - driver/CUDA errors vary
                message = f"{type(exc).__name__}: {exc}"
                errors.append(f"{dev}/{compute_type} ({candidate}): {message}")
                self.notes.append(f"Could not load {candidate} on {dev}: {message.splitlines()[0]}")
                continue

            self._model = model
            self.model_name = candidate
            self.device = dev
            self.compute_type = compute_type
            if (dev, compute_type, candidate) != attempts[0]:
                self.notes.append(f"Using fallback model {candidate} on {dev}/{compute_type}.")
            break

        if self._model is None:
            detail = "\n".join(errors) if errors else "unknown error"
            raise AsrError(f"Could not load any Whisper model:\n{detail}")

        # First CUDA inference compiles kernels and took ~2.5 s in benchmarks.
        # Do it now so the first real sentence is as fast as the rest.
        self._warmup()
        self.load_s = time.perf_counter() - started

    def _warmup(self) -> None:
        try:
            self.transcribe(np.zeros(16_000, dtype=np.float32), partial=True)
        except Exception:  # noqa: BLE001 - warmup must never break startup
            pass

    def transcribe(self, audio: np.ndarray, *, partial: bool = False) -> AsrResult:
        self.load()
        audio = np.ascontiguousarray(audio, dtype=np.float32)

        started = time.perf_counter()
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=1 if partial else self.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
            no_speech_threshold=0.6,
        )
        text = " ".join(segment.text.strip() for segment in segments)
        elapsed = time.perf_counter() - started

        return AsrResult(
            text=clean_text(text),
            audio_s=len(audio) / 16_000.0,
            inference_s=elapsed,
            model=self.model_name,
            device=self.device,
            compute_type=self.compute_type,
        )
