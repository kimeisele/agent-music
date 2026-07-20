"""Map a NormalizedSnapshot to a deterministic musical Composition.

Every audible property is derived from real federation data.
The same snapshot always produces the same composition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .normalize import NormalizedSnapshot

# ── Musical constants ────────────────────────────────────────────────────────

# Minor pentatonic: scale degrees as semitone offsets from root
MINOR_PENTATONIC = [0, 3, 5, 7, 10]

# Allowed roots (selected deterministically from snapshot seed)
ROOTS: dict[int, float] = {
    # MIDI note numbers for each root, octave 3
    0: 48.0,  # C3  = MIDI 48
    1: 50.0,  # D3  = MIDI 50
    2: 52.0,  # E3  = MIDI 52
    3: 53.0,  # F3  = MIDI 53
    4: 55.0,  # G3  = MIDI 55
    5: 57.0,  # A3  = MIDI 57
}

# Role → base octave offset from root octave
ROLE_OCTAVE: dict[str, int] = {
    "relay": -1,
    "governance": 0,
    "execution": 0,
    "outpost": 0,
    "research": 1,
    "observer": 2,
    "sandbox": 2,
    "generic": 0,
}

# Role → waveform
ROLE_WAVEFORM: dict[str, str] = {
    "relay": "sine",
    "governance": "triangle",
    "research": "triangle",
    "execution": "sine",
    "observer": "sine",
    "sandbox": "sine",
    "outpost": "triangle",
    "generic": "triangle",
}

# Tempo range
TEMPO_MIN = 72.0
TEMPO_MAX = 112.0

# Loop structure
LOOP_BARS = 8
BEATS_PER_BAR = 4
GRID_DIV = 4  # 16th notes per beat
TICKS_PER_BAR = BEATS_PER_BAR * GRID_DIV  # 16
TOTAL_TICKS = LOOP_BARS * TICKS_PER_BAR  # 128

# Target duration range (seconds)
TARGET_DURATION_MIN = 40.0
TARGET_DURATION_MAX = 60.0
TARGET_DURATION = 48.0


@dataclass
class NoteEvent:
    start_tick: int       # 0..TOTAL_TICKS-1, quantized grid position
    duration_ticks: int   # 1..8, duration in 16th notes
    midi_note: float      # MIDI note number (can be fractional for tuning)
    velocity: float       # 0.0 to 1.0
    voice_id: str         # node ID
    waveform: str         # "sine", "triangle", "square"
    octave: int           # octave offset from root


@dataclass
class Composition:
    events: list[NoteEvent] = field(default_factory=list)
    tempo: float = 90.0
    root_midi: float = 60.0  # C4
    total_ticks: int = TOTAL_TICKS

    def loop_duration_seconds(self) -> float:
        """Duration of one loop in seconds."""
        beats = self.total_ticks / GRID_DIV
        return beats / self.tempo * 60.0

    def repeat_count(self, target_duration: float = TARGET_DURATION) -> int:
        """Number of loop repeats to approximate target duration."""
        dur = self.loop_duration_seconds()
        if dur <= 0:
            return 1
        return max(1, round(target_duration / dur))


def _midi_to_freq(midi: float) -> float:
    """Convert MIDI note number to frequency (A4 = 440 Hz)."""
    return 440.0 * math.pow(2.0, (midi - 69.0) / 12.0)


def _stable_hash(value: str, seed: int) -> int:
    """Deterministic integer hash of *value* given a *seed*."""
    import hashlib
    h = hashlib.sha256(f"{seed}:{value}".encode()).digest()
    return int.from_bytes(h[:4], "big")


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def compose(snapshot: NormalizedSnapshot) -> Composition:
    """Transform a normalized snapshot into a deterministic composition."""
    seed = snapshot.seed()

    # ── Select root note ───────────────────────────────────────────────
    root_idx = seed % len(ROOTS)
    root_midi = ROOTS[root_idx]
    pitches = [root_midi + s for s in MINOR_PENTATONIC]

    # ── Tempo from in_flight ───────────────────────────────────────────
    in_flight = snapshot.pulse.in_flight
    # log-scale: 0 → 72, large → 112
    if in_flight > 0:
        tempo = TEMPO_MIN + (TEMPO_MAX - TEMPO_MIN) * min(1.0, math.log1p(in_flight) / math.log1p(500))
    else:
        tempo = TEMPO_MIN
    tempo = _clamp(tempo, TEMPO_MIN, TEMPO_MAX)

    # ── Compute per-node activity normalization ────────────────────────
    if snapshot.nodes:
        max_activity = max(n.activity for n in snapshot.nodes)
    else:
        max_activity = 0

    node_map: dict[str, dict] = {}
    for n in snapshot.nodes:
        if max_activity > 0:
            norm_activity = math.log1p(n.activity) / math.log1p(max_activity)
        else:
            norm_activity = 0.0
        norm_activity = _clamp(norm_activity, 0.0, 1.0)
        octave_offset = ROLE_OCTAVE.get(n.role, 0)
        waveform = ROLE_WAVEFORM.get(n.role, "triangle")
        # Stable voice identity
        node_hash = _stable_hash(n.id_, seed)
        scale_idx = node_hash % len(pitches)
        note_midi = pitches[scale_idx] + octave_offset * 12
        # Phase offset so voices don't all hit at once
        phase = node_hash % 4

        # How many notes this node gets in the loop
        # Active nodes: 2-8 notes; silent nodes: 0-2 notes
        if n.active and norm_activity > 0.01:
            note_count = max(2, round(norm_activity * 8))
        else:
            note_count = max(0, round(norm_activity * 2))

        node_map[n.id_] = {
            "midi": note_midi,
            "waveform": waveform,
            "activity": norm_activity,
            "note_count": note_count,
            "phase": phase,
            "scale_idx": scale_idx,
            "node_hash": node_hash,
        }

    events: list[NoteEvent] = []

    # ── Node voices ────────────────────────────────────────────────────
    for n in snapshot.nodes:
        info = node_map[n.id_]
        n_count = info["note_count"]
        if n_count == 0:
            continue
        # Place n_count notes on the grid, spread by phase
        available_ticks = TOTAL_TICKS
        if n_count > available_ticks:
            n_count = available_ticks
        # Deterministic grid positions using node hash
        step = available_ticks // n_count if n_count > 0 else available_ticks
        for i in range(n_count):
            tick = (info["phase"] * GRID_DIV + i * step + (info["node_hash"] + i) % max(1, step // 2)) % TOTAL_TICKS
            # Velocity from activity
            velocity = _clamp(0.2 + info["activity"] * 0.6, 0.15, 0.85)
            # Duration: 1-4 ticks based on activity
            dur = max(1, min(4, round(1 + info["activity"] * 3)))
            events.append(NoteEvent(
                start_tick=tick,
                duration_ticks=dur,
                midi_note=info["midi"],
                velocity=velocity,
                voice_id=n.id_,
                waveform=info["waveform"],
                octave=ROLE_OCTAVE.get(n.role, 0),
            ))

    # ── Flow events: call-and-response ─────────────────────────────────
    if snapshot.flows:
        max_weight = max(f.weight for f in snapshot.flows)
        # Sort flows by weight, take top N
        ranked = sorted(snapshot.flows, key=lambda f: -f.weight)
        top_flows = ranked[:min(len(ranked), 12)]

        flow_idx = 0
        for flow in top_flows:
            if flow.source not in node_map or flow.target not in node_map:
                continue
            if flow.weight <= 0:
                continue
            src_info = node_map[flow.source]
            tgt_info = node_map[flow.target]
            norm_weight = flow.weight / max(max_weight, 1)

            # How many times this flow repeats in the loop
            reps = max(1, round(norm_weight * 4))
            for r in range(reps):
                tick = (TOTAL_TICKS // 2 + flow_idx * 8 + r * (TOTAL_TICKS // reps)) % TOTAL_TICKS
                # Source note (call)
                events.append(NoteEvent(
                    start_tick=tick,
                    duration_ticks=2,
                    midi_note=src_info["midi"],
                    velocity=_clamp(0.25 + norm_weight * 0.35, 0.2, 0.75),
                    voice_id=flow.source,
                    waveform=src_info["waveform"],
                    octave=ROLE_OCTAVE.get(snapshot._node_role(flow.source), 0),
                ))
                # Target note (response), offset by 2 ticks
                resp_tick = (tick + 2) % TOTAL_TICKS
                interval = (tgt_info["scale_idx"] - src_info["scale_idx"]) % len(MINOR_PENTATONIC)
                response_midi = src_info["midi"] + MINOR_PENTATONIC[interval]
                events.append(NoteEvent(
                    start_tick=resp_tick,
                    duration_ticks=2,
                    midi_note=response_midi,
                    velocity=_clamp(0.25 + norm_weight * 0.35, 0.2, 0.75),
                    voice_id=flow.target,
                    waveform=tgt_info["waveform"],
                    octave=ROLE_OCTAVE.get(snapshot._node_role(flow.target), 0),
                ))
            flow_idx += 1

    # ── Sort events by start_tick ──────────────────────────────────────
    events.sort(key=lambda e: (e.start_tick, e.voice_id))

    return Composition(
        events=events,
        tempo=tempo,
        root_midi=root_midi,
    )


# Fix: NormalizedSnapshot needs _node_role helper for flow events
def _node_role(snapshot: NormalizedSnapshot, node_id: str) -> str:
    for n in snapshot.nodes:
        if n.id_ == node_id:
            return n.role
    return "generic"


# Monkey-patch for clean access
NormalizedSnapshot._node_role = _node_role
