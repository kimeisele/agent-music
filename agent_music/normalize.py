"""Normalize raw federation state into a stable internal snapshot.

All music generation operates on ``NormalizedSnapshot``, never on raw
payloads directly.  The snapshot is deterministically reproducible from
the same input data regardless of ordering.

Serialization contract:

    snapshot = NormalizedSnapshot.from_topology(topology)
    data = snapshot.to_dict()
    # … write data to disk …
    loaded = NormalizedSnapshot.from_dict(data)
    assert loaded.semantic_hash() == snapshot.semantic_hash()
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

SNAPSHOT_SCHEMA_VERSION = 1


@dataclass
class NodeInfo:
    id_: str                 # stable node name (last segment of repo_id)
    full_name: str           # canonical "owner/repo" identity
    role: str
    active: bool
    feed_available: bool
    activity: int            # non-negative, outbox depth


@dataclass
class FlowInfo:
    source: str
    target: str
    weight: int  # non-negative, envelope count


@dataclass
class PulseInfo:
    node_count: int
    communicating_nodes: int
    in_flight: int
    available_feeds: int


# ── Validation helpers ──────────────────────────────────────────────────────


def _require_str(obj: dict, key: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val:
        raise ValueError(f"'{key}' must be a non-empty string, got {type(val).__name__}")
    return val


def _require_int(obj: dict, key: str, min_val: int = 0) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or val < min_val:
        raise ValueError(f"'{key}' must be an int >= {min_val}, got {type(val).__name__}")
    return val


def _require_bool(obj: dict, key: str) -> bool:
    val = obj.get(key)
    if not isinstance(val, bool):
        raise ValueError(f"'{key}' must be a bool, got {type(val).__name__}")
    return val


def _require_list_of_dicts(obj: dict, key: str) -> list[dict]:
    val = obj.get(key)
    if not isinstance(val, list) or not all(isinstance(v, dict) for v in val):
        raise ValueError(f"'{key}' must be a list of dicts")
    return val


# ── NormalizedSnapshot ──────────────────────────────────────────────────────


@dataclass
class NormalizedSnapshot:
    """Stable internal representation of federation state.

    Two semantically equivalent inputs MUST produce byte-identical
    snapshots (excluding ``observed_at``).

    Serialization roundtrip preserves semantic hash exactly:

        snapshot.semantic_hash()
        ==
        NormalizedSnapshot.from_dict(
            json.loads(snapshot.to_json_bytes())
        ).semantic_hash()
    """

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    observed_at: str = ""
    nodes: list[NodeInfo] = field(default_factory=list)
    flows: list[FlowInfo] = field(default_factory=list)
    pulse: PulseInfo = field(default_factory=lambda: PulseInfo(0, 0, 0, 0))

    # ── Serialization ──────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize to a dict suitable for JSON file storage.

        Includes ``observed_at`` for metadata.  The semantic hash is NOT
        affected by ``observed_at`` — it is excluded from semantic_bytes().
        """
        return {
            "schema_version": self.schema_version,
            "observed_at": self.observed_at,
            "nodes": [
                {
                    "id": n.id_,
                    "full_name": n.full_name,
                    "role": n.role,
                    "active": n.active,
                    "feed_available": n.feed_available,
                    "activity": n.activity,
                }
                for n in self.nodes
            ],
            "flows": [
                {"source": f.source, "target": f.target, "weight": f.weight}
                for f in self.flows
            ],
            "pulse": {
                "node_count": self.pulse.node_count,
                "communicating_nodes": self.pulse.communicating_nodes,
                "in_flight": self.pulse.in_flight,
                "available_feeds": self.pulse.available_feeds,
            },
        }

    def to_json_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes (with observed_at)."""
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()

    @staticmethod
    def from_dict(data: dict) -> NormalizedSnapshot:
        """Deserialize a snapshot from a dict.  Validates types and rejects
        malformed input with ``ValueError``.

        Raises ``ValueError`` on invalid schema_version, missing fields,
        or wrong field types.
        """
        # Validate schema
        sv = data.get("schema_version")
        if sv != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version: {sv!r} (expected {SNAPSHOT_SCHEMA_VERSION})"
            )

        # Validate nodes
        raw_nodes = _require_list_of_dicts(data, "nodes")
        nodes: list[NodeInfo] = []
        for n in raw_nodes:
            nodes.append(NodeInfo(
                id_=_require_str(n, "id"),
                full_name=_require_str(n, "full_name"),
                role=_require_str(n, "role"),
                active=_require_bool(n, "active"),
                feed_available=_require_bool(n, "feed_available"),
                activity=_require_int(n, "activity", min_val=0),
            ))

        # Canonical ordering — two snapshots that differ only in node
        # array order must produce the same semantic hash.
        nodes.sort(key=lambda n: (n.full_name, n.id_))

        # Validate flows
        raw_flows = _require_list_of_dicts(data, "flows")
        flows: list[FlowInfo] = []
        for f in raw_flows:
            flows.append(FlowInfo(
                source=_require_str(f, "source"),
                target=_require_str(f, "target"),
                weight=_require_int(f, "weight", min_val=0),
            ))
        flows.sort(key=lambda f: (f.source, f.target, f.weight))

        # Validate pulse
        raw_pulse = data.get("pulse")
        if not isinstance(raw_pulse, dict):
            raise ValueError("'pulse' must be a dict")
        pulse = PulseInfo(
            node_count=_require_int(raw_pulse, "node_count", min_val=0),
            communicating_nodes=_require_int(raw_pulse, "communicating_nodes", min_val=0),
            in_flight=_require_int(raw_pulse, "in_flight", min_val=0),
            available_feeds=_require_int(raw_pulse, "available_feeds", min_val=0),
        )

        return NormalizedSnapshot(
            schema_version=sv,
            observed_at=str(data.get("observed_at", "")),
            nodes=nodes,
            flows=flows,
            pulse=pulse,
        )

    # ── Construction from topology ─────────────────────────────────────

    @staticmethod
    def _classify_role(capabilities: list[str], layer: str) -> str:
        caps = [c.lower() for c in capabilities]
        if "nadi-relay" in caps:
            return "relay"
        if layer == "internet":
            return "relay"
        if "governance" in caps:
            return "governance"
        if any(c.startswith("research") for c in caps):
            return "research"
        if any(c in caps for c in ("code_analysis", "task_execution", "ci_automation")):
            return "execution"
        if "federation-visualization" in caps:
            return "observer"
        if "test-target" in caps:
            return "sandbox"
        if "authority-publishing" in caps:
            return "outpost"
        return "generic"

    @staticmethod
    def from_topology(topology: dict) -> NormalizedSnapshot:
        """Build a normalized snapshot from a federation-map-style topology dict.

        This accepts the same topology format produced by federation-map's
        ``render_topology.py``.  Topology nodes MUST carry a canonical
        ``repo_id`` (owner/repo) — no owner is invented.
        """
        raw_nodes = topology.get("nodes", {})
        raw_flows = topology.get("flows", {})
        raw_summary = topology.get("summary", {})

        nodes: list[NodeInfo] = []
        for name in sorted(raw_nodes.keys()):
            n = raw_nodes[name]
            repo_id = str(n.get("repo_id", "")).strip()
            if "/" not in repo_id:
                # Offline/test fixture: use a clearly non-production identity
                repo_id = f"legacy:{name}"
            nodes.append(NodeInfo(
                id_=name,
                full_name=repo_id,
                role=NormalizedSnapshot._classify_role(
                    n.get("capabilities", []),
                    n.get("layer", "node"),
                ),
                active=n.get("status", "").upper() == "ACTIVE",
                feed_available=n.get("has_authority_feed", False),
                activity=max(0, n.get("depth", 0)),
            ))
        # Canonical ordering — must match from_dict sort key
        nodes.sort(key=lambda n: (n.full_name, n.id_))

        # Build a short-name → full_name mapping for flow key normalization
        name_map: dict[str, str] = {}
        for node in nodes:
            name_map[node.id_] = node.full_name

        flows: list[FlowInfo] = []
        for key, weight in raw_flows.items():
            if ">" not in key:
                continue
            source, target = key.split(">", 1)
            source, target = source.strip(), target.strip()
            if not source or not target:
                continue
            # Normalize to canonical owner/repo identity
            source = name_map.get(source, source)
            target = name_map.get(target, target)
            flows.append(FlowInfo(
                source=source,
                target=target,
                weight=max(0, weight),
            ))
        flows.sort(key=lambda f: (f.source, f.target, f.weight))

        pulse = PulseInfo(
            node_count=max(0, raw_summary.get("total_nodes", len(nodes))),
            communicating_nodes=max(0, raw_summary.get("communicating", 0)),
            in_flight=max(0, raw_summary.get("in_flight", 0)),
            available_feeds=max(0, raw_summary.get("feeds", 0)),
        )

        return NormalizedSnapshot(
            nodes=nodes,
            flows=flows,
            pulse=pulse,
        )

    # ── Hashing ─────────────────────────────────────────────────────────

    def semantic_bytes(self) -> bytes:
        """Return canonical JSON bytes of all musical fields.

        Timestamp is excluded so equivalent states produce identical hashes.
        """
        data: dict = {
            "schema_version": self.schema_version,
            "nodes": [
                {
                    "full_name": n.full_name,
                    "id": n.id_,
                    "role": n.role,
                    "active": n.active,
                    "feed_available": n.feed_available,
                    "activity": n.activity,
                }
                for n in self.nodes
            ],
            "flows": [
                {"source": f.source, "target": f.target, "weight": f.weight}
                for f in self.flows
            ],
            "pulse": {
                "node_count": self.pulse.node_count,
                "communicating_nodes": self.pulse.communicating_nodes,
                "in_flight": self.pulse.in_flight,
                "available_feeds": self.pulse.available_feeds,
            },
        }
        return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()

    def semantic_hash(self) -> str:
        """SHA-256 hex digest of the semantic bytes."""
        return hashlib.sha256(self.semantic_bytes()).hexdigest()

    def seed(self) -> int:
        """64-bit deterministic seed derived from the semantic hash."""
        return int.from_bytes(
            hashlib.sha256(self.semantic_bytes()).digest()[:8],
            "big",
        )
