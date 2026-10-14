"""Spot a FLAC that is really a low-bitrate or low-passed transcode.

Bitrate catches a file that is too small for lossless. The spectrum check
catches a decoded MP3 that was wrapped in a FLAC: music is present around
1 kHz and almost none remains near 17 kHz.
"""

from __future__ import annotations

import math
from pathlib import Path

# CD FLAC is usually well above this. A 320 kbps source is not.
FAKE_FLAC_KBPS = 450


def _goertzel(samples: list[float], sample_rate: int, freq: float) -> float:
    count = len(samples)
    if count < 8 or sample_rate <= 0 or freq <= 0 or freq >= sample_rate / 2:
        return 0.0
    omega = 2.0 * math.pi * freq / sample_rate
    coeff = 2.0 * math.cos(omega)
    s1 = 0.0
    s2 = 0.0
    for sample in samples:
        s0 = sample + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def spectrum_is_lossy(samples: list[float] | tuple[float, ...], sample_rate: int) -> bool:
    """True when a short PCM window has a voice band and no energy near 17 kHz."""
    window = list(samples[:4096])
    if sample_rate < 32000 or len(window) < 512:
        return False
    high_freq = min(17000.0, sample_rate / 2 - 400)
    low = _goertzel(window, sample_rate, 1000.0)
    high = _goertzel(window, sample_rate, high_freq)
    if low < 1e-6:
        return False
    return high / low < 0.02


def fake_flac_reason(
    *,
    extension: str,
    bitrate_kbps: float | None = None,
    samples: list[float] | tuple[float, ...] | None = None,
    sample_rate: int = 44100,
) -> str:
    """Why this file should not be treated as a real FLAC, or an empty string."""
    if (extension or "").lstrip(".").lower() != "flac":
        return ""
    if bitrate_kbps is not None and bitrate_kbps > 0 and bitrate_kbps < FAKE_FLAC_KBPS:
        return f"fake flac: bitrate {int(bitrate_kbps)} kbps"
    if samples and spectrum_is_lossy(samples, sample_rate):
        return "fake flac: spectrum cuts off like a lossy file"
    return ""


def folder_fake_reason(folder: Path) -> str:
    """First fake FLAC in a staged folder, using size and an optional short decode."""
    if not folder.is_dir():
        return ""
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".flac":
            continue
        bitrate = _bitrate_kbps(path)
        samples, rate = _preview_samples(path)
        reason = fake_flac_reason(extension="flac", bitrate_kbps=bitrate, samples=samples, sample_rate=rate)
        if reason:
            return f"{path.name}: {reason}"
    return ""


def _bitrate_kbps(path: Path) -> float | None:
    try:
        from mutagen.flac import FLAC

        audio = FLAC(path)
        info = getattr(audio, "info", None)
        bitrate = getattr(info, "bitrate", None)
        if bitrate:
            return float(bitrate) / 1000.0
        length = float(getattr(info, "length", 0) or 0)
        if length > 0:
            return (path.stat().st_size * 8) / length / 1000.0
    except Exception:
        return None
    return None


def _preview_samples(path: Path) -> tuple[list[float] | None, int]:
    """One second of mono PCM, when ffmpeg is on PATH. Otherwise no samples."""
    import shutil

    from process_launch import run_hidden

    exe = shutil.which("ffmpeg")
    if not exe:
        return None, 44100
    rate = 44100
    proc = run_hidden(
        [exe, "-v", "error", "-ss", "0", "-t", "1", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None, rate
    raw = proc.stdout
    count = len(raw) // 4
    if count < 512:
        return None, rate
    import struct

    samples = list(struct.unpack(f"<{count}f", raw[: count * 4]))
    return samples, rate
