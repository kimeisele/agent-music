"""Normalize raw federation state into a stable internal snapshot.

All music generation operates on ``NormalizedSnapshot``, never on raw
payloads directly.  The snapshot is deterministically reproducible from
the same input data regardless of ordering.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


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


@dataclass
class NormalizedSnapshot:
    """Stable internal representation of federation state.

    All fields are derived deterministically from input data.  Two
    semantically equivalent inputs MUST produce byte-identical
    snapshots (excluding ``observed_at``).
    """

    schema_version: int = 1
    observed_at: str = ""
    nodes: list[NodeInfo] = field(default_factory=list)
    flows: list[FlowInfo] = field(default_factory=list)
    pulse: PulseInfo = field(default_factory=lambda: PulseInfo(0, 0, 0, 0))

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
        ``render_topology.py``, so agent-music reuses federation-map's
        normalization behavior without depending on that repository.
        """
        raw_nodes = topology.get("nodes", {})
        raw_flows = topology.get("flows", {})
        raw_summary = topology.get("summary", {})

        nodes: list[NodeInfo] = []
        # Sort by stable node ID
        for name in sorted(raw_nodes.keys()):
            n = raw_nodes[name]
            # repo_id is canonical "owner/repo"; fall back to node name
            full_name = n.get("repo_id", name)
            if "/" not in full_name:
                full_name = f"kimeisele/{name}"  # legacy topology compat
            nodes.append(NodeInfo(
                id_=name,
                full_name=full_name,
                role=NormalizedSnapshot._classify_role(
                    n.get("capabilities", []),
                    n.get("layer", "node"),
                ),
                active=n.get("status", "").upper() == "ACTIVE",
                feed_available=n.get("has_authority_feed", False),
                activity=max(0, n.get("depth", 0)),
            ))

        flows: list[FlowInfo] = []
        for key, weight in raw_flows.items():
            if ">" not in key:
                continue
            source, target = key.split(">", 1)
            source, target = source.strip(), target.strip()
            if not source or not target:
                continue
            flows.append(FlowInfo(
                source=source,
                target=target,
                weight=max(0, weight),
            ))
        # Sort flows: source, target, weight
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
