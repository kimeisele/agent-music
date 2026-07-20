"""Collect validated federation state from discovered candidates.

Stages (explicit, separable):
  1. fetch_federation_descriptor  — get .well-known/agent-federation.json
  2. validate_federation_descriptor — kind, repo_id, identity match
  3. collect_node_state           — NADI outbox + authority feed

No static participant list.  All candidates come from topic discovery.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field

from .discovery import (
    RepositoryCandidate,
    FetchResult,
    FetchError,
    fetch_json,
    discover_candidate_repositories,
    FEDERATION_TOPIC,
)

# ── Protocol constants ──────────────────────────────────────────────────────

FEDERATION_DESCRIPTOR_KIND = "agent_federation_descriptor"
AUTHORITY_FEED_KIND = "source_authority_feed_manifest"
DEFAULT_OUTBOX_PATH = "data/federation/nadi_outbox.json"


# ── Structured result types ─────────────────────────────────────────────────


@dataclass
class ValidatedNode:
    """A candidate that passed descriptor validation."""
    full_name: str            # canonical "owner/repo"
    default_branch: str
    descriptor: dict
    peer: dict | None         # peer.json data, if available


@dataclass
class RejectedCandidate:
    full_name: str
    reason: str               # machine-readable: "invalid_json", "wrong_kind", etc.
    detail: str = ""


@dataclass
class CollectedNode:
    """A validated node with live state collected."""
    node_name: str
    repo_id: str              # from descriptor
    full_name: str
    default_branch: str
    status: str               # "ACTIVE", "SLEEPING", "UNREACHABLE"
    layer: str
    depth: int                # outbox envelope count
    outbox_reachable: bool
    has_authority_feed: bool
    flow_targets: dict[str, int]
    flow_sources: list[str]
    capabilities: list[str]


@dataclass
class CollectionResult:
    nodes: list[CollectedNode] = field(default_factory=list)
    flows: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""
    accepted: int = 0
    rejected: int = 0
    rejection_categories: dict[str, int] = field(default_factory=dict)
    outboxes_reachable: int = 0
    outboxes_unavailable: int = 0

    @property
    def has_authoritative_state(self) -> bool:
        return len(self.nodes) > 0

    def to_topology(self) -> dict:
        """Export as federation-map-compatible topology dict."""
        nodes_dict: dict[str, dict] = {}
        for n in self.nodes:
            nodes_dict[n.node_name] = {
                "node_name": n.node_name,
                "repo_id": n.repo_id,
                "status": n.status,
                "layer": n.layer,
                "depth": n.depth,
                "outbox_reachable": n.outbox_reachable,
                "has_authority_feed": n.has_authority_feed,
                "flow_targets": n.flow_targets,
                "flow_sources": n.flow_sources,
                "capabilities": n.capabilities,
            }
        total_in_flight = sum(n.depth for n in self.nodes)
        communicating = sum(
            1 for n in self.nodes if n.outbox_reachable and n.depth > 0
        )
        return {
            "generated_at": self.generated_at,
            "nodes": nodes_dict,
            "flows": dict(self.flows),
            "summary": {
                "total_nodes": len(self.nodes),
                "communicating": communicating,
                "in_flight": total_in_flight,
                "feeds": sum(1 for n in self.nodes if n.has_authority_feed),
            },
        }


# ── Stage 1: Fetch descriptor ───────────────────────────────────────────────


def _raw_url(full_name: str, branch: str, path: str) -> str:
    """Build a raw.githubusercontent.com URL using the actual default branch."""
    return f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"


def fetch_federation_descriptor(
    candidate: RepositoryCandidate,
) -> tuple[dict | None, FetchError | None]:
    """Fetch .well-known/agent-federation.json from a candidate repository.

    Uses the candidate's actual *default_branch*, not a hardcoded ``main``.
    """
    url = _raw_url(
        candidate.full_name,
        candidate.default_branch,
        ".well-known/agent-federation.json",
    )
    result = fetch_json(url)
    if not result.ok:
        return None, result.error
    data = result.data
    if not isinstance(data, dict):
        return None, FetchError(category="wrong_type", message="descriptor is not a JSON object")
    return data, None


# ── Stage 2: Validate descriptor ────────────────────────────────────────────


def validate_federation_descriptor(
    candidate: RepositoryCandidate,
    descriptor: dict,
) -> ValidatedNode | RejectedCandidate:
    """Validate a federation descriptor against the candidate repository.

    Required checks:
    - kind == "agent_federation_descriptor"
    - repo_id exists and matches candidate identity
    - display_name exists (required by protocol)
    """
    # Check kind
    kind = descriptor.get("kind")
    if kind != FEDERATION_DESCRIPTOR_KIND:
        return RejectedCandidate(
            full_name=candidate.full_name,
            reason="wrong_kind",
            detail=f"expected '{FEDERATION_DESCRIPTOR_KIND}', got {kind!r}",
        )

    # Check repo_id
    repo_id = str(descriptor.get("repo_id", "")).strip()
    if not repo_id:
        return RejectedCandidate(
            full_name=candidate.full_name,
            reason="missing_repo_id",
            detail="descriptor has no repo_id",
        )

    # Identity check: descriptor's repo_id must match the discovered repo
    # Normalize: strip trailing .git, lowercase comparison
    normalized_repo_id = repo_id.lower().removesuffix(".git")
    normalized_candidate = candidate.full_name.lower().removesuffix(".git")
    if normalized_repo_id != normalized_candidate:
        return RejectedCandidate(
            full_name=candidate.full_name,
            reason="identity_mismatch",
            detail=f"descriptor claims {repo_id!r}, repository is {candidate.full_name!r}",
        )

    # Check display_name
    display_name = str(descriptor.get("display_name", "")).strip()
    if not display_name:
        return RejectedCandidate(
            full_name=candidate.full_name,
            reason="missing_display_name",
            detail="descriptor has no display_name",
        )

    return ValidatedNode(
        full_name=candidate.full_name,
        default_branch=candidate.default_branch,
        descriptor=descriptor,
        peer=None,
    )


# ── Stage 3: Collect node state ────────────────────────────────────────────


def _count_flows(envelopes: list[dict]) -> tuple[int, dict[str, int], set[str]]:
    """Count outbox depth, targets, and sources."""
    depth = len(envelopes)
    targets: dict[str, int] = {}
    sources: set[str] = set()
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        target = str(
            env.get("target", "")
            or env.get("target_city_id", "")
        ).strip()
        source = str(
            env.get("source", "")
            or env.get("source_city_id", "")
        ).strip()
        if target:
            targets[target] = targets.get(target, 0) + 1
        if source:
            sources.add(source)
    return depth, targets, sources


def collect_node_state(
    node: ValidatedNode,
    outbox_path: str = DEFAULT_OUTBOX_PATH,
) -> CollectedNode:
    """Collect live state from a validated federation node.

    Fetches: NADI outbox, peer.json, authority feed.
    All optional — a node missing them is still valid but may be degraded.
    """
    descriptor = node.descriptor
    repo_id = descriptor.get("repo_id", node.full_name)
    node_name = repo_id.split("/")[-1]

    # Fetch peer.json for NADI config (optional)
    peer_url = _raw_url(node.full_name, node.default_branch, "data/federation/peer.json")
    peer_result = fetch_json(peer_url)
    peer: dict | None = None
    actual_outbox_path = outbox_path
    if peer_result.ok and isinstance(peer_result.data, dict):
        peer = peer_result.data
        actual_outbox_path = (
            peer.get("nadi", {}).get("outbox", outbox_path)
        )

    # Fetch NADI outbox
    outbox_url = _raw_url(node.full_name, node.default_branch, actual_outbox_path)
    outbox_result = fetch_json(outbox_url)
    outbox_reachable = False
    depth = 0
    targets: dict[str, int] = {}
    sources: set[str] = set()
    if outbox_result.ok and isinstance(outbox_result.data, list):
        outbox_reachable = True
        depth, targets, sources = _count_flows(outbox_result.data)

    # Fetch authority feed (optional)
    authority_url = _raw_url(
        node.full_name,
        "authority-feed",  # authority-feed is a BRANCH, not a path on main
        "latest-authority-manifest.json",
    )
    authority_result = fetch_json(authority_url)
    has_authority_feed = (
        authority_result.ok
        and isinstance(authority_result.data, dict)
        and authority_result.data.get("kind") == AUTHORITY_FEED_KIND
    )

    # Determine status
    declared_status = str(descriptor.get("status", "")).lower()
    if declared_status != "active":
        status = "SLEEPING"
    elif not outbox_reachable:
        status = "UNREACHABLE"
    else:
        status = "ACTIVE"

    # Merge capabilities from descriptor and peer
    desc_caps = [c.lower() for c in descriptor.get("capabilities", [])]
    peer_caps = [c.lower() for c in peer.get("capabilities", [])] if peer else []
    merged_caps = sorted(set(desc_caps + peer_caps))

    layer = str(descriptor.get("layer", "node")).lower()

    return CollectedNode(
        node_name=node_name,
        repo_id=repo_id,
        full_name=node.full_name,
        default_branch=node.default_branch,
        status=status,
        layer=layer,
        depth=depth,
        outbox_reachable=outbox_reachable,
        has_authority_feed=has_authority_feed,
        flow_targets=targets,
        flow_sources=list(sources),
        capabilities=merged_caps,
    )


# ── Orchestration ───────────────────────────────────────────────────────────


def collect_federation_state(
    outbox_path: str = DEFAULT_OUTBOX_PATH,
) -> CollectionResult:
    """Full pipeline: discover → validate → collect.

    Returns a CollectionResult with all successful nodes, flow data,
    and rejection statistics.  Individual failures never abort the
    entire collection as long as at least one valid node remains.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── Discover ────────────────────────────────────────────────────
    candidates, discovery_errors = discover_candidate_repositories()
    if discovery_errors and not candidates:
        print(f"discovery: FAILED — {len(discovery_errors)} error(s)", file=sys.stderr)
        for e in discovery_errors[:3]:
            print(f"  {e.category}: {e.message[:100]}", file=sys.stderr)
        return CollectionResult(generated_at=ts)

    print(f"discovery: {len(candidates)} candidates from topic '{FEDERATION_TOPIC}'", file=sys.stderr)
    if discovery_errors:
        print(f"  (with {len(discovery_errors)} page error(s))", file=sys.stderr)

    # ── Validate ─────────────────────────────────────────────────────
    validated: list[ValidatedNode] = []
    rejections: list[RejectedCandidate] = []

    for candidate in candidates:
        descriptor, fetch_err = fetch_federation_descriptor(candidate)
        if fetch_err is not None:
            rejections.append(RejectedCandidate(
                full_name=candidate.full_name,
                reason=f"fetch_{fetch_err.category}",
                detail=fetch_err.message[:120],
            ))
            continue

        assert descriptor is not None
        result = validate_federation_descriptor(candidate, descriptor)
        if isinstance(result, RejectedCandidate):
            rejections.append(result)
        else:
            validated.append(result)

    # Tally rejection categories
    rejection_categories: dict[str, int] = {}
    for r in rejections:
        rejection_categories[r.reason] = rejection_categories.get(r.reason, 0) + 1

    print(f"validation: {len(validated)} accepted, {len(rejections)} rejected", file=sys.stderr)
    for cat, count in sorted(rejection_categories.items()):
        print(f"  {cat}: {count}", file=sys.stderr)

    # ── Collect ──────────────────────────────────────────────────────
    nodes: list[CollectedNode] = []
    outboxes_reachable = 0
    outboxes_unavailable = 0

    for node in validated:
        collected = collect_node_state(node, outbox_path)
        nodes.append(collected)
        if collected.outbox_reachable:
            outboxes_reachable += 1
        else:
            outboxes_unavailable += 1

    print(f"collection: {outboxes_reachable} outboxes reachable, {outboxes_unavailable} unavailable", file=sys.stderr)

    # ── Aggregate flows ──────────────────────────────────────────────
    all_flows: dict[str, int] = {}
    for n in nodes:
        for target, count in n.flow_targets.items():
            key = f"{n.full_name}>{target}"
            all_flows[key] = all_flows.get(key, 0) + count

    return CollectionResult(
        nodes=nodes,
        flows=all_flows,
        generated_at=ts,
        accepted=len(validated),
        rejected=len(rejections),
        rejection_categories=rejection_categories,
        outboxes_reachable=outboxes_reachable,
        outboxes_unavailable=outboxes_unavailable,
    )
