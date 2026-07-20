"""Test the NormalizedSnapshot serialization contract."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_music.normalize import NormalizedSnapshot


def load_fixture(name: str) -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_semantic_hash_roundtrip():
    """serialize → deserialize preserves semantic hash exactly."""
    topo = load_fixture("active_federation.json")
    snap1 = NormalizedSnapshot.from_topology(topo)
    hash1 = snap1.semantic_hash()

    # Serialize to JSON (as snapshot command does)
    raw = snap1.to_json_bytes()

    # Deserialize (as render command does)
    data = json.loads(raw)
    snap2 = NormalizedSnapshot.from_dict(data)
    hash2 = snap2.semantic_hash()

    assert hash1 == hash2
    assert len(hash1) == 64


def test_to_dict_structure():
    """to_dict() produces the expected schema."""
    topo = load_fixture("active_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    data = snap.to_dict()

    assert data["schema_version"] == 1
    assert "observed_at" in data
    assert isinstance(data["nodes"], list)
    assert len(data["nodes"]) > 0
    for node in data["nodes"]:
        assert "id" in node
        assert "full_name" in node
        assert "role" in node
        assert isinstance(node["active"], bool)
        assert isinstance(node["feed_available"], bool)
        assert isinstance(node["activity"], int)
    assert isinstance(data["flows"], list)
    assert isinstance(data["pulse"], dict)


def test_from_dict_validates_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        NormalizedSnapshot.from_dict({"schema_version": 99})


def test_from_dict_rejects_missing_nodes():
    with pytest.raises(ValueError, match="nodes"):
        NormalizedSnapshot.from_dict({"schema_version": 1})


def test_from_dict_rejects_invalid_node_types():
    with pytest.raises(ValueError):
        NormalizedSnapshot.from_dict({
            "schema_version": 1,
            "nodes": [{"id": 123, "full_name": "x", "role": "x", "active": True, "feed_available": True, "activity": 0}],
            "flows": [],
            "pulse": {"node_count": 1, "communicating_nodes": 0, "in_flight": 0, "available_feeds": 0},
        })


def test_from_dict_rejects_malformed_pulse():
    with pytest.raises(ValueError, match="pulse"):
        NormalizedSnapshot.from_dict({
            "schema_version": 1,
            "nodes": [],
            "flows": [],
            "pulse": "not-a-dict",
        })


def test_ordering_preserved():
    """Node and flow ordering is deterministic through roundtrip."""
    topo = load_fixture("active_federation.json")
    snap1 = NormalizedSnapshot.from_topology(topo)
    data = snap1.to_dict()
    snap2 = NormalizedSnapshot.from_dict(data)

    n1 = [(n.id_, n.full_name) for n in snap1.nodes]
    n2 = [(n.id_, n.full_name) for n in snap2.nodes]
    assert n1 == n2


def test_no_kimeisele_fallback():
    """from_topology must not invent kimeisele/ owner for missing repo_id."""
    topo = {
        "nodes": {
            "test-node": {
                "repo_id": "test-node",  # no slash = no owner
                "capabilities": [],
                "layer": "node",
                "status": "active",
                "depth": 0,
                "has_authority_feed": False,
            }
        },
        "flows": {},
        "summary": {"total_nodes": 1, "communicating": 0, "in_flight": 0, "feeds": 0},
    }
    snap = NormalizedSnapshot.from_topology(topo)
    node = snap.nodes[0]
    # Must use legacy: prefix, not kimeisele/
    assert node.full_name == "legacy:test-node"
    assert "kimeisele" not in node.full_name


# ── CLI-level E2E test ──────────────────────────────────────────────────────


def test_cli_snapshot_to_render_e2e():
    """Exercise the same file format used by the GitHub Actions workflow."""
    import subprocess
    import sys
    import wave

    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "snapshot.json"
        wav_path = Path(tmp) / "federation.wav"
        meta_path = Path(tmp) / "render.json"
        fixture = Path(__file__).parent / "fixtures" / "active_federation.json"

        # Step 1: Snapshot command (uses from_topology → to_dict)
        # We can't run the full snapshot in tests (needs network), so simulate
        topo = json.loads(fixture.read_text())
        snap = NormalizedSnapshot.from_topology(topo)
        snap.observed_at = "2026-01-01T00:00:00Z"
        snap_path.write_bytes(snap.to_json_bytes())

        # Step 2: Render command (reads snapshot, composes, writes WAV)
        result = subprocess.run(
            [sys.executable, "-m", "agent_music.cli", "render",
             "--input", str(snap_path),
             "--output", str(wav_path),
             "--metadata-output", str(meta_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

        # Verify WAV is valid and has audio
        assert wav_path.exists()
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getsampwidth() == 2
            assert wf.getnframes() > 0

        # Verify metadata
        meta = json.loads(meta_path.read_text())
        assert "semantic_snapshot_sha256" in meta
        assert "audio_sha256" in meta
        assert meta.get("state_changed") is True

        # Step 3: Re-render with previous metadata → should skip synthesis
        result2 = subprocess.run(
            [sys.executable, "-m", "agent_music.cli", "render",
             "--input", str(snap_path),
             "--output", str(wav_path),
             "--metadata-output", str(tmp) + "/render2.json",
             "--prev-metadata", str(meta_path)],
            capture_output=True, text=True,
        )
        assert result2.returncode == 0
        meta2 = json.loads(open(Path(tmp) / "render2.json").read())
        assert meta2.get("state_changed") is False
        assert "skipping" in result2.stderr.lower() or "unchanged" in result2.stderr.lower()


# ── Canonical node ordering tests ───────────────────────────────────────────


def test_reordered_nodes_produce_same_order():
    """Deserializing snapshots with different node array order produces
    the same canonical order."""
    topo = load_fixture("active_federation.json")
    snap1 = NormalizedSnapshot.from_topology(topo)
    data1 = snap1.to_dict()

    # Reverse the node array
    data2 = dict(data1)
    data2["nodes"] = list(reversed(data1["nodes"]))

    snap2 = NormalizedSnapshot.from_dict(data2)

    ids1 = [(n.full_name, n.id_) for n in snap1.nodes]
    ids2 = [(n.full_name, n.id_) for n in snap2.nodes]
    assert ids1 == ids2


def test_reordered_nodes_same_semantic_hash():
    """Reordered node arrays produce identical semantic hashes."""
    topo = load_fixture("active_federation.json")
    snap = NormalizedSnapshot.from_topology(topo)
    data = snap.to_dict()

    # Shuffle nodes
    import random
    rng = random.Random(42)
    nodes_shuffled = list(data["nodes"])
    rng.shuffle(nodes_shuffled)
    data["nodes"] = nodes_shuffled

    snap2 = NormalizedSnapshot.from_dict(data)
    assert snap.semantic_hash() == snap2.semantic_hash()


def test_roundtrip_ordering_stable():
    """Serialization/deserialization roundtrip keeps node ordering stable."""
    topo = load_fixture("active_federation.json")
    snap1 = NormalizedSnapshot.from_topology(topo)
    data = snap1.to_dict()
    snap2 = NormalizedSnapshot.from_dict(data)
    data2 = snap2.to_dict()

    # A second roundtrip should produce identical order
    snap3 = NormalizedSnapshot.from_dict(data2)
    ids2 = [(n.full_name, n.id_) for n in snap2.nodes]
    ids3 = [(n.full_name, n.id_) for n in snap3.nodes]
    assert ids2 == ids3
