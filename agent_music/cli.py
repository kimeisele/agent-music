"""Command-line interface for agent-music.

Separated lifecycle:
  snapshot  — discover → validate → collect → normalize → write files
  render    — read snapshot → check hash → compose → synth → write WAV

Machine-readable output goes to files (--metadata-output), not stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .collect import collect_federation_state, CollectionResult
from .normalize import NormalizedSnapshot
from .compose import compose
from .synth import synth, samples_to_pcm, SAMPLE_RATE
from .wav import write_wav, validate_wav

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "federation.json"


def _load_outbox_path(config_path: str | None) -> str:
    """Load the outbox path from config, or use the default."""
    if config_path is None:
        config_path = str(DEFAULT_CONFIG)
    cfg = Path(config_path)
    if cfg.exists():
        data = json.loads(cfg.read_text())
        return data.get("collection", {}).get("default_outbox_path", "data/federation/nadi_outbox.json")
    return "data/federation/nadi_outbox.json"


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Discover, validate, collect, normalize → write snapshot + metadata."""
    outbox_path = _load_outbox_path(args.config)

    print("Discovering federation nodes via topic search...", file=sys.stderr)
    result = collect_federation_state(outbox_path=outbox_path)

    if not result.has_authoritative_state:
        print("ERROR: No authoritative federation state collected.", file=sys.stderr)
        return 2

    topology = result.to_topology()
    snapshot = NormalizedSnapshot.from_topology(topology)
    semantic_hash = snapshot.semantic_hash()
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Write snapshot
    snapshot_data = json.loads(snapshot.semantic_bytes())
    snapshot_data["observed_at"] = observed_at
    snapshot_out = Path(args.output)
    snapshot_out.parent.mkdir(parents=True, exist_ok=True)
    snapshot_out.write_text(json.dumps(snapshot_data, indent=2, sort_keys=True) + "\n")

    # Write metadata
    meta = {
        "schema_version": 1,
        "semantic_snapshot_sha256": semantic_hash,
        "observed_at": observed_at,
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


def cmd_render(args: argparse.Namespace) -> int:
    """Read snapshot → hash check → compose → synth → write WAV."""
    from .normalize import NormalizedSnapshot

    snapshot_path = Path(args.input)
    if not snapshot_path.exists():
        print(f"ERROR: Snapshot not found: {args.input}", file=sys.stderr)
        return 1

    topology = json.loads(snapshot_path.read_text())
    snapshot = NormalizedSnapshot.from_topology(topology)
    semantic_hash = snapshot.semantic_hash()

    # ── Change detection: check previous hash BEFORE synthesis ────────
    prev_hash = _load_previous_hash(args.prev_metadata)
    if prev_hash and prev_hash == semantic_hash:
        print("Federation state unchanged — skipping render", file=sys.stderr)
        meta = {
            "schema_version": 1,
            "semantic_snapshot_sha256": semantic_hash,
            "audio_sha256": "",
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_sec": 0,
            "state_changed": False,
        }
        meta_path = Path(args.metadata_output)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        return 0

    # ── Compose ───────────────────────────────────────────────────────
    composition = compose(snapshot)
    loop_dur = composition.loop_duration_seconds()
    repeats = composition.repeat_count()
    total_dur = loop_dur * repeats
    print(
        f"Tempo: {composition.tempo:.0f} BPM · "
        f"Loop: {loop_dur:.1f}s · "
        f"Repeats: {repeats} · "
        f"Total: {total_dur:.0f}s",
        file=sys.stderr,
    )

    # ── Synthesize ────────────────────────────────────────────────────
    loop_samples = synth(composition)
    all_samples = loop_samples * repeats

    # Crossfade loop boundaries
    if len(loop_samples) > 0:
        fade_len = min(int(0.005 * SAMPLE_RATE), len(loop_samples))
        if fade_len > 0:
            for i in range(fade_len):
                if i < len(all_samples) - fade_len:
                    fade = i / fade_len
                    idx_end = len(all_samples) - fade_len + i
                    if idx_end < len(all_samples):
                        all_samples[idx_end] *= (1.0 - fade)
                for r in range(1, repeats):
                    idx = r * len(loop_samples) + i
                    if idx < len(all_samples):
                        all_samples[idx] *= i / fade_len

    # Normalize
    peak = max(abs(s) for s in all_samples) if all_samples else 0.0
    if peak > 0.0:
        scale = 0.9 / peak
        all_samples = [s * scale for s in all_samples]

    pcm = samples_to_pcm(all_samples)

    # ── Write WAV ─────────────────────────────────────────────────────
    output_path = Path(args.output)
    write_wav(output_path, pcm, sample_rate=SAMPLE_RATE)

    # Validate
    validation = validate_wav(output_path)
    if not validation["valid"]:
        print("WAV validation warnings:", file=sys.stderr)
        for err in validation["errors"]:
            print(f"  - {err}", file=sys.stderr)

    audio_sha256 = hashlib.sha256(pcm).hexdigest()

    # Write metadata
    meta = {
        "schema_version": 1,
        "semantic_snapshot_sha256": semantic_hash,
        "audio_sha256": audio_sha256,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_sec": validation["duration_sec"],
        "state_changed": True,
    }
    meta_path = Path(args.metadata_output)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")

    print(f"WAV written: {output_path}", file=sys.stderr)
    print(f"Duration: {validation['duration_sec']:.1f}s · "
          f"Peak: {validation['peak']:.2f} · "
          f"Has audio: {validation['has_audio']}", file=sys.stderr)
    return 0


def _load_previous_hash(prev_meta_path: str | None) -> str | None:
    """Load previous semantic hash from a render metadata file."""
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Music — Federation state → deterministic WAV audio",
    )
    sub = parser.add_subparsers(dest="command")

    snap = sub.add_parser("snapshot", help="Discover federation and write normalized snapshot")
    snap.add_argument("--config", default=None, help="Path to config JSON")
    snap.add_argument("--output", default="snapshot.json", help="Output snapshot JSON path")
    snap.add_argument("--metadata-output", default="snapshot-meta.json", help="Metadata output path")

    rndr = sub.add_parser("render", help="Render snapshot to WAV")
    rndr.add_argument("--input", required=True, help="Input snapshot JSON")
    rndr.add_argument("--output", default="federation.wav", help="Output WAV path")
    rndr.add_argument("--metadata-output", default="render.json", help="Metadata output path")
    rndr.add_argument("--prev-metadata", default=None, help="Previous render metadata for change detection")

    args = parser.parse_args()

    if args.command == "snapshot":
        return cmd_snapshot(args)
    elif args.command == "render":
        return cmd_render(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
