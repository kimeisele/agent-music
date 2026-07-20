"""Command-line interface for agent-music.

Separated lifecycle:
  snapshot  — discover → validate → collect → normalize → write files
  compose   — snapshot → canonical Composition → composition.json
  render    — load Composition → synth WAV + render SVG → publish

Machine output goes to files, not stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .collect import collect_federation_state
from .discovery import DiscoveryConfig
from .normalize import NormalizedSnapshot
from .compose import compose, validate_composition
from .synth import synth, samples_to_pcm, SAMPLE_RATE
from .wav import write_wav, validate_wav

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "federation.json"


def _load_discovery_config(config_path: str | None) -> DiscoveryConfig:
    if config_path is None:
        config_path = str(DEFAULT_CONFIG)
    cfg = Path(config_path)
    if cfg.exists():
        data = json.loads(cfg.read_text())
        disc = data.get("discovery", {})
        return DiscoveryConfig(
            per_page=int(disc.get("per_page", 100)),
            max_pages=int(disc.get("max_pages", 10)),
            http_timeout_seconds=float(disc.get("http_timeout_seconds", 15)),
            max_response_bytes=int(disc.get("max_response_bytes", 10 * 1024 * 1024)),
        )
    return DiscoveryConfig()


def _load_outbox_path(config_path: str | None) -> str:
    if config_path is None:
        config_path = str(DEFAULT_CONFIG)
    cfg = Path(config_path)
    if cfg.exists():
        data = json.loads(cfg.read_text())
        return data.get("collection", {}).get("default_outbox_path", "data/federation/nadi_outbox.json")
    return "data/federation/nadi_outbox.json"


# ── snapshot ────────────────────────────────────────────────────────────────


def cmd_snapshot(args: argparse.Namespace) -> int:
    discovery_config = _load_discovery_config(args.config)
    outbox_path = _load_outbox_path(args.config)

    print("Discovering federation nodes via topic search...", file=sys.stderr)
    result = collect_federation_state(
        outbox_path=outbox_path,
        discovery_config=discovery_config,
    )

    if not result.has_authoritative_state:
        print("ERROR: No authoritative federation state collected.", file=sys.stderr)
        return 2

    topology = result.to_topology()
    snapshot = NormalizedSnapshot.from_topology(topology)
    snapshot.observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    semantic_hash = snapshot.semantic_hash()

    snapshot_out = Path(args.output)
    snapshot_out.parent.mkdir(parents=True, exist_ok=True)
    snapshot_out.write_bytes(snapshot.to_json_bytes())

    meta = {
        "schema_version": 1,
        "semantic_snapshot_sha256": semantic_hash,
        "observed_at": snapshot.observed_at,
        "candidates_discovered": result.accepted + result.rejected,
        "nodes_accepted": result.accepted,
        "nodes_rejected": result.rejected,
        "rejection_categories": result.rejection_categories,
        "outboxes_reachable": result.outboxes_reachable,
        "outboxes_unavailable": result.outboxes_unavailable,
    }
    meta_out = Path(args.metadata_output)
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    meta_out.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")

    print(f"Snapshot: {snapshot_out}", file=sys.stderr)
    print(f"Metadata: {meta_out}", file=sys.stderr)
    print(f"Semantic hash: {semantic_hash[:16]}...", file=sys.stderr)
    print(f"Nodes: {result.accepted} accepted, {result.rejected} rejected", file=sys.stderr)
    return 0


# ── compose ─────────────────────────────────────────────────────────────────


def cmd_compose(args: argparse.Namespace) -> int:
    snapshot_path = Path(args.input)
    if not snapshot_path.exists():
        print(f"ERROR: Snapshot not found: {args.input}", file=sys.stderr)
        return 1

    try:
        data = json.loads(snapshot_path.read_bytes())
        snapshot = NormalizedSnapshot.from_dict(data)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Invalid snapshot: {e}", file=sys.stderr)
        return 1

    composition = compose(snapshot)
    errs = validate_composition(composition)
    if errs:
        for e in errs:
            print(f"Composition validation: {e}", file=sys.stderr)
        return 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(composition.to_artifact_json_bytes())

    comp_hash = composition.semantic_hash()
    print(f"Composition: {out}", file=sys.stderr)
    print(f"Composition hash: {comp_hash[:16]}...", file=sys.stderr)
    print(f"Voices: {len(composition.voices)}, Events: {len(composition.events)}", file=sys.stderr)
    if any(e.provenance.get("pair_id") for e in composition.events):
        pair_count = len({e.provenance.get("pair_id") for e in composition.events if e.provenance.get("pair_id")})
        print(f"Flow pairs: {pair_count}", file=sys.stderr)
    return 0


# ── render ──────────────────────────────────────────────────────────────────


def cmd_render(args: argparse.Namespace) -> int:
    from .compose import Composition

    comp_path = Path(args.input)
    if not comp_path.exists():
        print(f"ERROR: Composition not found: {args.input}", file=sys.stderr)
        return 1

    try:
        data = json.loads(comp_path.read_bytes())
        composition = Composition.from_dict(data)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Invalid composition: {e}", file=sys.stderr)
        return 1

    # ── Change detection ──────────────────────────────────────────────
    prev_hash = _load_previous_hash(args.prev_metadata)
    if prev_hash and prev_hash == composition.semantic_snapshot_sha256:
        print("Federation state unchanged — skipping render", file=sys.stderr)
        meta = {
            "schema_version": 2,
            "semantic_snapshot_sha256": composition.semantic_snapshot_sha256,
            "composition_sha256": composition.composition_sha256,
            "audio_sha256": "",
            "wav_sha256": "",
            "svg_sha256": "",
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_sec": 0,
            "loop_duration_sec": 0,
            "repeat_count": 0,
            "event_count": 0,
            "voice_count": 0,
            "state_changed": False,
        }
        Path(args.metadata_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.metadata_output).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        return 0

    # ── Synthesize WAV ────────────────────────────────────────────────
    loop_samples = synth(composition)
    all_samples = loop_samples * composition.repeat_count

    if len(loop_samples) > 0:
        fade_len = min(int(0.005 * SAMPLE_RATE), len(loop_samples))
        if fade_len > 0:
            for i in range(fade_len):
                if i < len(all_samples) - fade_len:
                    idx_end = len(all_samples) - fade_len + i
                    if idx_end < len(all_samples):
                        all_samples[idx_end] *= (1.0 - i / fade_len)
                for r in range(1, composition.repeat_count):
                    idx = r * len(loop_samples) + i
                    if idx < len(all_samples):
                        all_samples[idx] *= i / fade_len

    peak = max(abs(s) for s in all_samples) if all_samples else 0.0
    if peak > 0.0:
        scale = 0.9 / peak
        all_samples = [s * scale for s in all_samples]

    pcm = samples_to_pcm(all_samples)
    wav_path = Path(args.wav_output)
    write_wav(wav_path, pcm, sample_rate=SAMPLE_RATE)

    wav_validation = validate_wav(wav_path)
    audio_sha256 = hashlib.sha256(pcm).hexdigest()      # PCM bytes (existing)
    wav_sha256 = hashlib.sha256(wav_path.read_bytes()).hexdigest()  # complete file

    # ── Render SVG ────────────────────────────────────────────────────
    from .score_svg import render_svg, validate_svg
    svg_path = Path(args.svg_output)
    svg_result = render_svg(composition, svg_path)
    svg_validation = validate_svg(svg_path, composition)

    # ── Write metadata ────────────────────────────────────────────────
    meta = {
        "schema_version": 2,
        "semantic_snapshot_sha256": composition.semantic_snapshot_sha256,
        "composition_sha256": composition.composition_sha256,
        "audio_sha256": audio_sha256,
        "wav_sha256": wav_sha256,
        "svg_sha256": svg_result["svg_sha256"],
        "duration_sec": wav_validation["duration_sec"],
        "loop_duration_sec": composition.loop_duration_seconds,
        "repeat_count": composition.repeat_count,
        "event_count": len(composition.events),
        "voice_count": len(composition.voices),
        "state_changed": True,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    Path(args.metadata_output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.metadata_output).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")

    print(f"WAV: {wav_path}", file=sys.stderr)
    print(f"SVG: {svg_path}", file=sys.stderr)
    print(f"Duration: {wav_validation['duration_sec']:.1f}s", file=sys.stderr)
    print(f"PCM SHA-256: {audio_sha256[:16]}...", file=sys.stderr)
    print(f"WAV SHA-256: {wav_sha256[:16]}...", file=sys.stderr)
    print(f"SVG SHA-256: {svg_result['svg_sha256'][:16]}...", file=sys.stderr)
    return 0


def _load_previous_hash(prev_meta_path: str | None) -> str | None:
    if prev_meta_path is None:
        return None
    p = Path(prev_meta_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data.get("semantic_snapshot_sha256")
    except (json.JSONDecodeError, OSError):
        return None


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Music — Federation state → deterministic WAV + SVG",
    )
    sub = parser.add_subparsers(dest="command")

    snap = sub.add_parser("snapshot", help="Discover federation and write normalized snapshot")
    snap.add_argument("--config", default=None, help="Path to config JSON")
    snap.add_argument("--output", default="snapshot.json", help="Output snapshot JSON path")
    snap.add_argument("--metadata-output", default="snapshot-meta.json", help="Metadata output path")

    comp = sub.add_parser("compose", help="Compose snapshot into canonical Composition")
    comp.add_argument("--input", required=True, help="Input snapshot JSON")
    comp.add_argument("--output", default="composition.json", help="Output composition JSON path")

    rndr = sub.add_parser("render", help="Render composition to WAV + SVG")
    rndr.add_argument("--input", required=True, help="Input composition JSON")
    rndr.add_argument("--wav-output", default="federation.wav", help="Output WAV path")
    rndr.add_argument("--svg-output", default="federation.svg", help="Output SVG path")
    rndr.add_argument("--metadata-output", default="render.json", help="Metadata output path")
    rndr.add_argument("--prev-metadata", default=None, help="Previous render metadata for change detection")

    args = parser.parse_args()

    if args.command == "snapshot":
        return cmd_snapshot(args)
    elif args.command == "compose":
        return cmd_compose(args)
    elif args.command == "render":
        return cmd_render(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
