"""Write WAV files from PCM audio data.

Pure Python, standard library only.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path


def write_wav(
    path: Path,
    pcm_data: bytes,
    sample_rate: int = 16000,
    num_channels: int = 1,
    bits_per_sample: int = 16,
) -> None:
    """Write a mono 16-bit PCM WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(bits_per_sample // 8)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


def validate_wav(
    path: Path,
    expected_sample_rate: int = 16000,
    expected_channels: int = 1,
    expected_bits: int = 16,
    min_duration_sec: float = 5.0,
    max_duration_sec: float = 120.0,
) -> dict:
    """Validate a WAV file and return diagnostic info.

    Returns a dict with keys: valid (bool), errors (list[str]),
    sample_rate, channels, bits, duration_sec, peak, has_audio.
    """
    errors: list[str] = []
    info: dict = {
        "valid": False,
        "errors": errors,
        "sample_rate": 0,
        "channels": 0,
        "bits": 0,
        "duration_sec": 0.0,
        "peak": 0.0,
        "has_audio": False,
    }

    if not path.exists():
        errors.append(f"File not found: {path}")
        return info

    try:
        with wave.open(str(path), "rb") as wf:
            info["sample_rate"] = wf.getframerate()
            info["channels"] = wf.getnchannels()
            info["bits"] = wf.getsampwidth() * 8
            n_frames = wf.getnframes()
            info["duration_sec"] = n_frames / wf.getframerate() if wf.getframerate() > 0 else 0.0

            frames = wf.readframes(n_frames)

            if info["channels"] != expected_channels:
                errors.append(
                    f"Expected {expected_channels} channels, got {info['channels']}"
                )
            if info["sample_rate"] != expected_sample_rate:
                errors.append(
                    f"Expected {expected_sample_rate} Hz, got {info['sample_rate']}"
                )
            if info["bits"] != expected_bits:
                errors.append(
                    f"Expected {expected_bits}-bit, got {info['bits']}-bit"
                )

            if n_frames == 0:
                errors.append("No audio frames")
            else:
                info["has_audio"] = True

            # Check peak and duration
            samples = struct.unpack(f"<{n_frames}h", frames)
            peak = max(abs(s) for s in samples) if samples else 0
            info["peak"] = peak / 32767.0

            if info["duration_sec"] < min_duration_sec:
                errors.append(
                    f"Duration {info['duration_sec']:.1f}s < minimum {min_duration_sec}s"
                )
            if info["duration_sec"] > max_duration_sec:
                errors.append(
                    f"Duration {info['duration_sec']:.1f}s > maximum {max_duration_sec}s"
                )

            if peak >= 32767:
                errors.append("Clipping detected (peak at max)")

    except wave.Error as e:
        errors.append(f"Invalid WAV: {e}")
        return info

    info["valid"] = len(errors) == 0
    return info
