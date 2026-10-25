"""Terminal interface: live loopback transcription with latency numbers."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import IO

from app import __version__
from app.asr import AsrError, WhisperASR
from app.audio import loopback_devices, output_devices
from app.config import Settings
from app.pipeline import Event, StreamPipeline


# --------------------------------------------------------------------- ANSI
DIM = "\x1b[2m"
RESET = "\x1b[0m"
CYAN = "\x1b[36m"
RED = "\x1b[31m"
BOLD = "\x1b[1m"


def enable_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


# ----------------------------------------------------------------- renderer
class ConsoleRenderer:
    def __init__(self, *, plain: bool, color: bool, transcript: IO[str] | None = None) -> None:
        self.plain = plain
        self.color = color and not plain
        self.transcript = transcript
        self._lock = threading.RLock()
        self._partial_visible = False

    def on_event(self, event: Event) -> None:
        with self._lock:
            if event.kind == "partial":
                self._partial(event)
            elif event.kind == "final":
                self._final(event)
            elif event.kind == "status":
                self._status(event.text)
            elif event.kind == "error":
                self._error(event.text)

    # -- internals -----------------------------------------------------------
    def _paint(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def _clear_partial(self) -> None:
        if self._partial_visible:
            sys.stdout.write("\r\x1b[2K")
            self._partial_visible = False

    def _write_line(self, line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def _partial(self, event: Event) -> None:
        if self.plain or not event.text:
            return
        width = max(20, shutil.get_terminal_size((120, 20)).columns - 2)
        text = f"  …  {event.text}"
        if len(text) > width:
            text = text[: width - 1] + "…"
        sys.stdout.write("\r\x1b[2K" + self._paint(DIM, text))
        sys.stdout.flush()
        self._partial_visible = True

    def _final(self, event: Event) -> None:
        self._clear_partial()
        stamp = f"[{event.wall:%H:%M:%S}]" if event.wall else "[--:--:--]"
        meta = f"  ·  {event.latency_s:.1f}s"
        if event.forced:
            meta += "  ·  max-length split"
        line = f"{self._paint(CYAN, stamp)} {event.text}{self._paint(DIM, meta)}"
        self._write_line(line)
        if self.transcript is not None:
            self.transcript.write(f"{stamp} {event.text}\n")
            self.transcript.flush()

    def _status(self, text: str) -> None:
        self._clear_partial()
        self._write_line(self._paint(DIM, f"  • {text}"))

    def _error(self, text: str) -> None:
        self._clear_partial()
        self._write_line(self._paint(RED, f"  ✖ {text}"))


# ---------------------------------------------------------------- CLI parts
def _print_devices() -> None:
    print("Output devices (what you hear):")
    outputs = output_devices()
    if not outputs:
        print("  (none)")
    for device in outputs:
        marker = "  *" if device.is_default else "   "
        suffix = "  [default]" if device.is_default else ""
        print(f"{marker} {device.name}{suffix}")

    print("\nLoopback devices (captured by this app):")
    loopbacks = loopback_devices()
    if not loopbacks:
        print("  (none - enable an output device in Windows sound settings)")
    for device in loopbacks:
        marker = "  *" if device.is_default else "   "
        suffix = "  [default]" if device.is_default else ""
        print(f"{marker} {device.name}{suffix}")

    print("\nPick one with:  python -m app.cli --audio-device \"part of the name\"")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Meeting Copilot - local live transcription of system audio.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--list-devices", action="store_true", help="list output/loopback devices and exit")
    parser.add_argument("--audio-device", default=None, help="part of the loopback device name")
    parser.add_argument(
        "--model",
        default="auto",
        help="Whisper model (default: small.en; prefers a larger local model if present)",
    )
    parser.add_argument(
        "--compute-device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="where to run Whisper (default: auto)",
    )
    parser.add_argument("--compute-type", default=None, help="override compute type, e.g. float16 or int8")
    parser.add_argument("--beam-size", type=int, default=1, help="beam size for finalized text (default: 1)")
    parser.add_argument("--end-silence-ms", type=int, default=400, help="silence that closes a sentence (default: 400)")
    parser.add_argument("--max-utterance", type=float, default=12.0, help="force-flush after N seconds (default: 12)")
    parser.add_argument("--partial-interval", type=float, default=2.0, help="seconds between live partial updates")
    parser.add_argument("--partials", action="store_true", help="show live partial text (adds latency)")
    parser.add_argument("--no-partials", action="store_true", help="only show finalized sentences (default)")
    parser.add_argument("--plain", action="store_true", help="no colors, no in-place partial line")
    parser.add_argument("--save-transcript", action="store_true", help="write finalized text to logs/ (off by default)")
    parser.add_argument("--seconds", type=float, default=None, help="stop automatically after N seconds")
    parser.add_argument("--warmup", action="store_true", help="load the model and exit (download/benchmark check)")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        audio_device=args.audio_device,
        model=args.model,
        compute_device=args.compute_device,
        compute_type=args.compute_type,
        beam_size=args.beam_size,
        end_silence_ms=args.end_silence_ms,
        max_utterance_s=args.max_utterance,
        partials=args.partials and not args.no_partials,
        partial_interval_s=args.partial_interval,
        save_transcript=args.save_transcript,
    )


def _warmup(settings: Settings) -> int:
    asr = WhisperASR(
        model=settings.model,
        compute_device=settings.compute_device,
        compute_type=settings.compute_type,
        beam_size=settings.beam_size,
    )
    try:
        asr.load()
    except AsrError as exc:
        print(f"Could not load the model:\n{exc}", file=sys.stderr)
        return 1
    for note in asr.notes:
        print(f"note: {note}")
    print(f"Model: {asr.label}")
    print(f"Load time: {asr.load_s:.1f}s")
    return 0


def _print_summary(stats: dict[str, float | int | str]) -> None:
    print()
    print("-" * 62)
    print("Session summary")
    print(f"  Model:                     {stats['model']}")
    print(f"  Finalized sentences:       {stats['utterances']}")
    print(
        "  Latency (speech end -> text):"
        f" avg {stats['latency_avg_s']:.2f}s"
        f" | p50 {stats['latency_p50_s']:.2f}s"
        f" | p95 {stats['latency_p95_s']:.2f}s"
    )
    print(
        "  ASR inference:             "
        f"avg {stats['inference_avg_s']:.2f}s"
        f" | real-time factor {stats['rtf_avg']:.2f}x"
    )
    print(f"  Dropped audio frames:      {stats['dropped_frames']}")
    print("  Audio written to disk:     no")
    print("-" * 62)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_devices:
        _print_devices()
        return 0

    settings = _settings_from_args(args)

    if args.warmup:
        return _warmup(settings)

    color_ok = enable_ansi()
    is_tty = sys.stdout.isatty()
    plain = args.plain or not is_tty

    transcript_file = None
    transcript_path = None
    if settings.save_transcript:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / f"transcript-{datetime.now():%Y%m%d-%H%M%S}.txt"
        transcript_file = transcript_path.open("w", encoding="utf-8")
        print(f"! Transcript logging is ON: {transcript_path}", file=sys.stderr)

    renderer = ConsoleRenderer(
        plain=plain,
        color=color_ok and is_tty,
        transcript=transcript_file,
    )

    print()
    print(f"Meeting Copilot {__version__} — Milestone 1 (local ASR)")
    print("Capturing system audio only. Your microphone is not used.")
    print("Default model: small.en. Better accuracy: --model distil-large-v3 (larger download).")
    print()

    pipeline = StreamPipeline(settings, renderer.on_event)
    try:
        pipeline.run(max_seconds=args.seconds)
    except KeyboardInterrupt:
        pipeline.stop()
    finally:
        if transcript_file is not None:
            transcript_file.close()

    _print_summary(pipeline.stats())
    if transcript_path is not None:
        print(f"Transcript saved to: {transcript_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
