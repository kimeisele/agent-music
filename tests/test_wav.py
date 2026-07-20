"""Test WAV file validity."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_music.normalize import NormalizedSnapshot
from agent_music.compose import compose
from agent_music.synth import synth, samples_to_pcm
from agent_music.wav import write_wav, validate_wav

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _render_wav(name: str, path: Path) -> None:
    topo = json.loads((FIXTURES / name).read_text())
    snap = NormalizedSnapshot.from_topology(topo)
    comp = compose(snap)
    loop = synth(comp)
    repeats = comp.repeat_count(48.0)
    all_samples = loop * repeats
    peak = max(abs(s) for s in all_samples) if all_samples else 0.0
    if peak > 0.0:
        scale = 0.9 / peak
        all_samples = [s * scale for s in all_samples]
    pcm = samples_to_pcm(all_samples)
    write_wav(path, pcm)


def test_wav_header_valid():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    try:
        _render_wav("active_federation.json", path)
        result = validate_wav(path)
        assert result["valid"], f"WAV validation failed: {result['errors']}"
        assert result["sample_rate"] == 16000
        assert result["channels"] == 1
        assert result["bits"] == 16
        assert result["has_audio"] is True
    finally:
        path.unlink(missing_ok=True)


def test_wav_duration_in_range():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    try:
        _render_wav("active_federation.json", path)
        result = validate_wav(path)
        assert 5.0 <= result["duration_sec"] <= 120.0, \
            f"Duration {result['duration_sec']:.1f}s out of range"
    finally:
        path.unlink(missing_ok=True)


def test_wav_no_clipping():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    try:
        _render_wav("active_federation.json", path)
        result = validate_wav(path)
        assert result["peak"] < 1.0, f"Peak at {result['peak']}, possible clipping"
    finally:
        path.unlink(missing_ok=True)


def test_wav_nonzero():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    try:
        _render_wav("active_federation.json", path)
        result = validate_wav(path)
        assert result["has_audio"] is True
    finally:
        path.unlink(missing_ok=True)
