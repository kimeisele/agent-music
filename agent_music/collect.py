"""Collect federation state from live protocol surfaces.

Reads descriptor seeds, fetches .well-known/agent-federation.json and
NADI outboxes, and produces a topology dict compatible with the
federation-map format (so normalize.py can consume it).

Uses only stdlib urllib — no external HTTP dependencies.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FederationConfig:
    seed_urls: list[str] = field(default_factory=list)
    outbox_path: str = "data/federation/nadi_outbox.json"
    user_agent: str = "agent-music/0.1 (observer node)"
    http_timeout: int = 15


def _fetch_json(url: str, timeout: int = 15, ua: str = "agent-music/0.1") -> dict | list | None:
    """Fetch and parse JSON. Returns None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def collect_federation_state(config: FederationConfig) -> dict | None:
    """Collect live federation state and return a topology dict.

    Returns None if no authoritative state could be collected.
    """
    peers: list[dict] = []
    seen: set[str] = set()

    for url in config.seed_urls:
        descriptor = _fetch_json(url, config.http_timeout, config.user_agent)
        if not descriptor or not isinstance(descriptor, dict):
            continue
        if descriptor.get("kind") != "agent_federation_descriptor":
            continue
        repo_id = str(descriptor.get("repo_id", ""))
        if not repo_id or repo_id in seen:
            continue
        seen.add(repo_id)

        node_name = repo_id.split("/")[-1]
        base = f"https://raw.githubusercontent.com/{repo_id}/main"

        # Fetch peer.json for NADI config
        peer_url = f"{base}/data/federation/peer.json"
        peer_data = _fetch_json(peer_url, config.http_timeout, config.user_agent)
        peer = peer_data if isinstance(peer_data, dict) else None

        peers.append({
            "node_name": node_name,
            "repo_id": repo_id,
            "descriptor": descriptor,
            "peer": peer,
            "base_url": base,
        })

    if not peers:
        print("No peers discovered from seed URLs.", file=sys.stderr)
        return None

    # ── Collect outbox data ────────────────────────────────────────────
    outbox_data: dict[str, tuple[int, dict[str, int], set[str]]] = {}
    for p in peers:
        outbox_path = (
            p["peer"].get("nadi", {}).get("outbox", config.outbox_path)
            if p["peer"] else config.outbox_path
        )
        url = f"{p['base_url']}/{outbox_path}"
        envelopes = _fetch_json(url, config.http_timeout, config.user_agent)
        if isinstance(envelopes, list):
            depth, targets, sources = _count_flows(envelopes)
            outbox_data[p["node_name"]] = (depth, targets, sources)

    # ── Collect authority feeds ────────────────────────────────────────
    authority_data: dict[str, bool] = {}
    for p in peers:
        url = f"https://raw.githubusercontent.com/{p['repo_id']}/authority-feed/latest-authority-manifest.json"
        data = _fetch_json(url, config.http_timeout, config.user_agent)
        authority_data[p["node_name"]] = (
            isinstance(data, dict) and data.get("kind") == "source_authority_feed_manifest"
        )

    # ── Build topology dict (federation-map compatible) ────────────────
    nodes: dict[str, dict] = {}
    all_flows: dict[str, int] = {}
    total_in_flight = 0

    for p in peers:
        node_name = p["node_name"]
        descriptor = p["peer"] if p["peer"] else p["descriptor"]
        outbox = outbox_data.get(node_name)
        outbox_reachable = outbox is not None
        depth, targets, sources = outbox if outbox_reachable else (0, {}, set())
        total_in_flight += depth

        desc_caps = [c.lower() for c in p["descriptor"].get("capabilities", [])]
        peer_caps = [c.lower() for c in p.get("peer", {}).get("capabilities", [])] if p["peer"] else []
        merged_caps = sorted(set(desc_caps + peer_caps))

        declared_status = str(p["descriptor"].get("status", "")).lower()
        status = "ACTIVE" if declared_status == "active" and outbox_reachable else (
            "UNREACHABLE" if not outbox_reachable else "SLEEPING"
        )

        layer = str(p["descriptor"].get("layer", "node")).lower()

        for target, count in targets.items():
            key = f"{node_name}>{target}"
            all_flows[key] = all_flows.get(key, 0) + count

        nodes[node_name] = {
            "node_name": node_name,
            "repo_id": p["repo_id"],
            "status": status,
            "layer": layer,
            "depth": depth,
            "outbox_reachable": outbox_reachable,
            "has_authority_feed": authority_data.get(node_name, False),
            "flow_targets": targets,
            "flow_sources": list(sources),
            "capabilities": merged_caps,
        }

    communicating = sum(
        1 for n in nodes.values()
        if n["outbox_reachable"] and n["depth"] > 0
    )

    topology = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodes": nodes,
        "flows": all_flows,
        "summary": {
            "total_nodes": len(nodes),
            "communicating": communicating,
            "in_flight": total_in_flight,
            "feeds": sum(1 for n in nodes.values() if n["has_authority_feed"]),
        },
    }

    return topology


def _count_flows(envelopes: list[dict]) -> tuple[int, dict[str, int], set[str]]:
    """Count envelope depth, targets, and sources from NADI outbox."""
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
