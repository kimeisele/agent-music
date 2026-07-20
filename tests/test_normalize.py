"""Test normalization invariants."""

from __future__ import annotations

import json
from pathlib import Path

from agent_music.normalize import NormalizedSnapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_snapshot_from_active_fixture():
    topo = _load("active_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    assert snap.schema_version == 1
    assert len(snap.nodes) == 8
    assert snap.pulse.node_count == 8
    assert snap.pulse.in_flight > 0
    # Semantic hash must be stable
    h1 = snap.semantic_hash()
    h2 = snap.semantic_hash()
    assert h1 == h2
    assert len(h1) == 64


def test_snapshot_from_quiet_fixture():
    topo = _load("quiet_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    assert snap.pulse.in_flight == 0
    assert snap.pulse.communicating_nodes == 0
    for node in snap.nodes:
        assert node.activity == 0


def test_snapshot_from_partial_fixture():
    topo = _load("partial_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    assert len(snap.nodes) == 3


def test_nodes_sorted():
    topo = _load("active_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    ids = [n.id_ for n in snap.nodes]
    assert ids == sorted(ids)


def test_flows_sorted():
    topo = _load("active_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    for i in range(len(snap.flows) - 1):
        a, b = snap.flows[i], snap.flows[i + 1]
        assert (a.source, a.target, a.weight) <= (b.source, b.target, b.weight)


def test_negative_activity_clamped():
    topo = _load("active_federation.json")
    # Inject negative depth
    topo["nodes"]["agent-internet"]["depth"] = -5
    snap = NormalizedSnapshot.from_topology(topo)
    ai = next(n for n in snap.nodes if n.id_ == "agent-internet")
    assert ai.activity == 0


def test_semantic_hash_excludes_timestamp():
    topo = _load("active_federation.json")
    snap1 = NormalizedSnapshot.from_topology(topo)
    topo["generated_at"] = "2099-01-01T00:00:00Z"
    snap2 = NormalizedSnapshot.from_topology(topo)
    assert snap1.semantic_hash() == snap2.semantic_hash()


def test_seed_deterministic():
    topo = _load("active_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    s1 = snap.seed()
    s2 = snap.seed()
    assert s1 == s2
    assert isinstance(s1, int)
