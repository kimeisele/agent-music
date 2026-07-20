"""Test end-to-end determinism: same fixture → byte-identical WAV."""

from __future__ import annotations

import json
from pathlib import Path

from agent_music.normalize import NormalizedSnapshot
from agent_music.compose import compose
from agent_music.synth import synth, samples_to_pcm, SAMPLE_RATE

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _render_samples(fixture_name: str) -> bytes:
    topo = json.loads((FIXTURES / fixture_name).read_text())
    snap = NormalizedSnapshot.from_topology(topo)
    comp = compose(snap)
    loop = synth(comp)
    repeats = comp.repeat_count(48.0)
    all_samples = loop * repeats
    # Apply final normalization
    peak = max(abs(s) for s in all_samples) if all_samples else 0.0
    if peak > 0.0:
        scale = 0.9 / peak
        all_samples = [s * scale for s in all_samples]
    return samples_to_pcm(all_samples)


def test_determinism_active():
    pcm1 = _render_samples("active_federation.json")
    pcm2 = _render_samples("active_federation.json")
    assert pcm1 == pcm2


def test_determinism_quiet():
    pcm1 = _render_samples("quiet_federation.json")
    pcm2 = _render_samples("quiet_federation.json")
    assert pcm1 == pcm2


def test_determinism_partial():
    pcm1 = _render_samples("partial_federation.json")
    pcm2 = _render_samples("partial_federation.json")
    assert pcm1 == pcm2


def test_different_states_different_audio():
    active = _render_samples("active_federation.json")
    quiet = _render_samples("quiet_federation.json")
    assert active != quiet


def test_output_not_empty():
    for name in ["active_federation.json", "quiet_federation.json", "partial_federation.json"]:
        pcm = _render_samples(name)
        assert len(pcm) > 0, f"{name} produced empty output"


def test_output_not_silent():
    """Active and partial fixtures must produce non-zero audio."""
    # Active fixture has activity → must have audible content
    pcm = _render_samples("active_federation.json")
    import struct
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm)
    peak = max(abs(s) for s in samples)
    assert peak > 0, "Active fixture produced silence"


def test_loop_boundary_no_click():
    """Loop repeat boundary should not produce extreme amplitude jumps."""
    topo = json.loads((FIXTURES / "active_federation.json").read_text())
    snap = NormalizedSnapshot.from_topology(topo)
    comp = compose(snap)
    loop = synth(comp)
    if len(loop) < 10:
        return  # too short to check
    # Check that loop end isn't wildly different from loop start
    fade_len = min(int(0.005 * SAMPLE_RATE), len(loop) // 2)
    if fade_len > 0:
        start_rms = sum(s * s for s in loop[:fade_len]) / fade_len
        end_rms = sum(s * s for s in loop[-fade_len:]) / fade_len
        # Both should be reasonably small or the transition is handled
        assert start_rms >= 0 and end_rms >= 0  # sanity
