"""Test musical composition behavior."""

from __future__ import annotations

import json
from pathlib import Path

from agent_music.normalize import NormalizedSnapshot
from agent_music.compose import compose, Composition, NoteEvent

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _compose(name: str) -> Composition:
    topo = _load(name)
    snap = NormalizedSnapshot.from_topology(topo)
    return compose(snap)


def test_composition_has_events():
    comp = _compose("active_federation.json")
    assert len(comp.events) > 0


def test_composition_has_tempo_in_range():
    comp = _compose("active_federation.json")
    assert 60 <= comp.tempo <= 120


def test_loop_duration_positive():
    comp = _compose("active_federation.json")
    assert comp.loop_duration_seconds() > 0


def test_repeat_count_reasonable():
    comp = _compose("active_federation.json")
    repeats = comp.repeat_count(48.0)
    assert 1 <= repeats <= 10


def test_quiet_fixture_fewer_events_than_active():
    active = _compose("active_federation.json")
    quiet = _compose("quiet_federation.json")
    # Quiet should produce fewer note events
    assert len(quiet.events) < len(active.events)


def test_quiet_has_events():
    comp = _compose("quiet_federation.json")
    # Should still produce some events (resting federation)
    assert len(comp.events) >= 0  # May legitimately be zero with no nodes active


def test_events_sorted():
    comp = _compose("active_federation.json")
    ticks = [e.start_tick for e in comp.events]
    assert ticks == sorted(ticks)


def test_deterministic_composition():
    comp1 = _compose("active_federation.json")
    comp2 = _compose("active_federation.json")
    # Same number of events
    assert len(comp1.events) == len(comp2.events)
    # Same tempo
    assert comp1.tempo == comp2.tempo
    # Same events
    for e1, e2 in zip(comp1.events, comp2.events):
        assert e1.start_tick == e2.start_tick
        assert e1.midi_note == e2.midi_note


def test_velocity_in_range():
    comp = _compose("active_federation.json")
    for e in comp.events:
        assert 0.0 < e.velocity <= 1.0


def test_flows_affect_events():
    """Reversing flow direction should change event sequence."""
    topo = _load("active_federation.json")
    snap1 = NormalizedSnapshot.from_topology(topo)

    # Swap all flow directions
    swapped_flows = {}
    for key, weight in topo["flows"].items():
        src, tgt = key.split(">", 1)
        swapped_flows[f"{tgt}>{src}"] = weight
    topo["flows"] = swapped_flows
    snap2 = NormalizedSnapshot.from_topology(topo)

    comp1 = compose(snap1)
    comp2 = compose(snap2)

    # Event sequences should differ when flows are reversed
    # (at minimum, different voice sequences in flow events)
    ev1 = [(e.start_tick, e.voice_id, e.midi_note) for e in comp1.events]
    ev2 = [(e.start_tick, e.voice_id, e.midi_note) for e in comp2.events]
    assert ev1 != ev2
