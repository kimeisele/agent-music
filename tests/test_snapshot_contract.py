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
    """Exercise the full three-stage lifecycle used by the GitHub Actions workflow."""
    import subprocess
    import sys
    import wave

    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "snapshot.json"
        comp_path = Path(tmp) / "composition.json"
        wav_path = Path(tmp) / "federation.wav"
        svg_path = Path(tmp) / "federation.svg"
        meta_path = Path(tmp) / "render.json"
        fixture = Path(__file__).parent / "fixtures" / "active_federation.json"

        # Step 1: Simulate snapshot (offline, no network)
        topo = json.loads(fixture.read_text())
        snap = NormalizedSnapshot.from_topology(topo)
        snap.observed_at = "2026-01-01T00:00:00Z"
        snap_path.write_bytes(snap.to_json_bytes())

        # Step 2: Compose command (snapshot → composition.json)
        result = subprocess.run(
            [sys.executable, "-m", "agent_music.cli", "compose",
             "--input", str(snap_path),
             "--output", str(comp_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"compose failed: {result.stderr}"

        # Verify composition.json exists and is valid
        assert comp_path.exists()
        comp_data = json.loads(comp_path.read_text())
        assert comp_data["schema_version"] == 1
        assert "composition_sha256" in comp_data
        assert len(comp_data["voices"]) > 0
        assert len(comp_data["events"]) > 0

        # Step 3: Render command (composition.json → WAV + SVG)
        result = subprocess.run(
            [sys.executable, "-m", "agent_music.cli", "render",
             "--input", str(comp_path),
             "--wav-output", str(wav_path),
             "--svg-output", str(svg_path),
             "--metadata-output", str(meta_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"render failed: {result.stderr}"

        # Verify WAV is valid and has audio
        assert wav_path.exists()
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getsampwidth() == 2
            assert wf.getnframes() > 0

        # Verify SVG exists, is valid XML, and has expected elements
        assert svg_path.exists()
        svg_raw = svg_path.read_bytes()
        assert b"<svg" in svg_raw
        assert b"<rect" in svg_raw or b"<polygon" in svg_raw

        # Validate SVG via the validator
        from agent_music.score_svg import validate_svg
        from agent_music.compose import Composition
        comp_obj = Composition.from_dict(comp_data)
        svg_val = validate_svg(svg_path, comp_obj)
        assert svg_val["valid"], f"SVG invalid: {svg_val['errors']}"

        # Count event elements and connectors in SVG
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(svg_path))
        ns = "{http://www.w3.org/2000/svg}"
        event_els = tree.findall(f".//*[@data-event-id]")
        assert len(event_els) == len(comp_data["events"]), \
            f"SVG events {len(event_els)} != composition events {len(comp_data['events'])}"
        line_conns = tree.findall(f".//{ns}line[@data-pair-id]")
        path_conns = tree.findall(f".//{ns}path[@data-pair-id]")
        pair_count = len({e["provenance"]["pair_id"] for e in comp_data["events"] if e["provenance"].get("pair_id")})
        assert len(line_conns) + len(path_conns) == pair_count, \
            f"SVG connectors {len(line_conns)+len(path_conns)} != pairs {pair_count}"

        # Verify late-tick events are in composition
        late_events = [e for e in comp_data["events"] if e["start_tick"] >= 120]
        assert len(late_events) > 0, "No late-tick events in composition"

        # Verify metadata
        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == 2
        assert meta["composition_sha256"] == comp_data["composition_sha256"]
        assert len(meta["audio_sha256"]) == 64  # PCM hash present
        assert len(meta["wav_sha256"]) == 64
        assert len(meta["svg_sha256"]) == 64
        assert meta.get("state_changed") is True
        # Loop and render durations are consistent
        assert abs(meta["loop_duration_sec"] * meta["repeat_count"] - meta["duration_sec"]) < 1.0

        # Step 4: Deterministic rerun — byte-identical
        result_b = subprocess.run(
            [sys.executable, "-m", "agent_music.cli", "render",
             "--input", str(comp_path),
             "--wav-output", str(Path(tmp) / "federation_b.wav"),
             "--svg-output", str(Path(tmp) / "federation_b.svg"),
             "--metadata-output", str(Path(tmp) / "render_b.json")],
            capture_output=True, text=True,
        )
        assert result_b.returncode == 0
        assert wav_path.read_bytes() == Path(tmp).joinpath("federation_b.wav").read_bytes(), "WAV not deterministic"
        assert svg_path.read_bytes() == Path(tmp).joinpath("federation_b.svg").read_bytes(), "SVG not deterministic"

        # Step 5: Re-render with previous metadata → should skip synthesis
        result2 = subprocess.run(
            [sys.executable, "-m", "agent_music.cli", "render",
             "--input", str(comp_path),
             "--wav-output", str(wav_path),
             "--svg-output", str(svg_path),
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
