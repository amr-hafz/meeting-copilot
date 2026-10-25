"""Make pip-installed CUDA libraries discoverable on Windows.

CTranslate2 (the engine behind faster-whisper) loads cuBLAS and cuDNN with
``LoadLibrary`` at model-creation time.  The NVIDIA pip wheels put those DLLs
inside site-packages, which is *not* on the default DLL search path on Windows.

Call :func:`enable` before importing ``ctranslate2`` or ``faster_whisper``.
It is a no-op on non-Windows platforms and when the wheels are not installed
(in that case the ASR layer falls back to CPU).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_handles: list[object] = []
_enabled = False


def _nvidia_dirs() -> list[Path]:
    try:
        import nvidia  # namespace package created by the nvidia-* wheels
    except Exception:
        return []

    roots = [Path(p) for p in list(getattr(nvidia, "__path__", []))]
    dirs: list[Path] = []
    for root in roots:
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            for name in ("bin", "lib"):
                candidate = root / sub / name
                if candidate.is_dir():
                    dirs.append(candidate)
    return dirs


def enable() -> list[str]:
    """Register NVIDIA wheel DLL directories. Returns the directories added."""
    global _enabled
    if sys.platform != "win32" or _enabled:
        return [str(d) for d in _nvidia_dirs()]

    _enabled = True
    added: list[str] = []
    for directory in _nvidia_dirs():
        try:
            _handles.append(os.add_dll_directory(str(directory)))
        except OSError:
            continue
        os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
        added.append(str(directory))
    return added
