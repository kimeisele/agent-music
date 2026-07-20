"""Command-line interface for agent-music.

Usage:
    python -m agent_music.cli render --output federation.wav
    python -m agent_music.cli render --input tests/fixtures/active_federation.json
    python -m agent_music.cli snapshot
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .collect import collect_federation_state, FederationConfig
from .normalize import NormalizedSnapshot
from .compose import compose, Composition
from .synth import synth, samples_to_pcm, SAMPLE_RATE
from .wav import write_wav, validate_wav

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "federation.json"


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Print the normalized snapshot as JSON (dry-run)."""
    config = _load_config(args.config)
    print("Collecting federation state...", file=sys.stderr)
    topology = collect_federation_state(config)
    if topology is None:
        print("ERROR: Could not collect federation state.", file=sys.stderr)
        return 2
    snapshot = NormalizedSnapshot.from_topology(topology)
    print(json.dumps(json.loads(snapshot.semantic_bytes()), indent=2))
    print(f"\nSemantic hash: {snapshot.semantic_hash()}", file=sys.stderr)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Render federation state to a WAV file."""
    config = _load_config(args.config)

    # ── Collect ────────────────────────────────────────────────────────
    if args.input:
        # Offline mode: read topology from fixture file
        topology_path = Path(args.input)
        if not topology_path.exists():
            print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
            return 1
        topology = json.loads(topology_path.read_text())
    else:
        print("Collecting federation state...", file=sys.stderr)
        topology = collect_federation_state(config)
        if topology is None:
            print("ERROR: Could not collect federation state — no authoritative data.", file=sys.stderr)
            return 2

    # ── Normalize ──────────────────────────────────────────────────────
    snapshot = NormalizedSnapshot.from_topology(topology)
    semantic_hash = snapshot.semantic_hash()
    print(f"Semantic hash: {semantic_hash[:16]}...", file=sys.stderr)

    # ── Compose ────────────────────────────────────────────────────────
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

    # ── Synthesize ─────────────────────────────────────────────────────
    loop_samples = synth(composition)
    # Repeat the loop
    all_samples = loop_samples * repeats

    # Crossfade loop boundaries (short linear crossfade to prevent clicks)
    if len(loop_samples) > 0:
        fade_len = min(int(0.005 * SAMPLE_RATE), len(loop_samples))  # 5ms
        if fade_len > 0:
            for i in range(fade_len):
                # Fade out end of loop
                if i < len(all_samples) - fade_len:
                    fade = i / fade_len
                    idx_end = len(all_samples) - fade_len + i
                    if idx_end < len(all_samples):
                        all_samples[idx_end] *= (1.0 - fade)
                # Fade in start of next loop
                for r in range(1, repeats):
                    idx = r * len(loop_samples) + i
                    if idx < len(all_samples):
                        fade = i / fade_len
                        all_samples[idx] *= fade

    # Normalize the full render
    peak = max(abs(s) for s in all_samples) if all_samples else 0.0
    if peak > 0.0:
        scale = 0.9 / peak
        all_samples = [s * scale for s in all_samples]

    pcm = samples_to_pcm(all_samples)

    # ── Write WAV ──────────────────────────────────────────────────────
    output_path = Path(args.output)
    write_wav(output_path, pcm, sample_rate=SAMPLE_RATE)

    # ── Validate ───────────────────────────────────────────────────────
    validation = validate_wav(output_path)
    if not validation["valid"]:
        print("WAV validation warnings:", file=sys.stderr)
        for err in validation["errors"]:
            print(f"  - {err}", file=sys.stderr)

    audio_sha256 = hashlib.sha256(pcm).hexdigest()
    print(f"WAV written: {output_path}", file=sys.stderr)
    print(f"SHA-256: {audio_sha256[:16]}...", file=sys.stderr)
    print(f"Duration: {validation['duration_sec']:.1f}s · "
          f"Peak: {validation['peak']:.2f} · "
          f"Has audio: {validation['has_audio']}", file=sys.stderr)

    # Machine-readable last line for workflow consumption
    print(json.dumps({
        "semantic_snapshot_sha256": semantic_hash,
        "audio_sha256": audio_sha256,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_sec": validation["duration_sec"],
    }))

    return 0


def _load_config(path: str | None) -> FederationConfig:
    if path is None:
        path = str(DEFAULT_CONFIG)
    config_path = Path(path)
    if not config_path.exists():
        return FederationConfig()
    data = json.loads(config_path.read_text())
    return FederationConfig(
        seed_urls=data.get("seed_urls", []),
        outbox_path=data.get("outbox_path", "data/federation/nadi_outbox.json"),
        user_agent=data.get("user_agent", "agent-music/0.1 (observer node)"),
        http_timeout=data.get("http_timeout", 15),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Music — Federation state → deterministic WAV audio",
    )
    sub = parser.add_subparsers(dest="command")

    snap = sub.add_parser("snapshot", help="Print normalized federation snapshot")
    snap.add_argument("--config", default=None, help="Path to config JSON")

    rndr = sub.add_parser("render", help="Render federation state to WAV")
    rndr.add_argument("--config", default=None, help="Path to config JSON")
    rndr.add_argument("--output", default="federation.wav", help="Output WAV path")
    rndr.add_argument("--input", default=None, help="Offline: read topology from fixture JSON")

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
