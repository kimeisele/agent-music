"""Map a NormalizedSnapshot to a deterministic, serializable Composition.

Every audible property is derived from real federation data.
The same snapshot always produces the same canonical Composition.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from .normalize import NormalizedSnapshot

# ── Musical constants ────────────────────────────────────────────────────────

MINOR_PENTATONIC = [0, 3, 5, 7, 10]

ROOTS: dict[int, float] = {
    0: 48.0, 1: 50.0, 2: 52.0, 3: 53.0, 4: 55.0, 5: 57.0,
}

ROLE_OCTAVE: dict[str, int] = {
    "relay": -1, "governance": 0, "execution": 0, "outpost": 0,
    "research": 1, "observer": 2, "sandbox": 2, "generic": 0,
}

ROLE_WAVEFORM: dict[str, str] = {
    "relay": "sine", "governance": "triangle", "research": "triangle",
    "execution": "sine", "observer": "sine", "sandbox": "sine",
    "outpost": "triangle", "generic": "triangle",
}

ROLE_ORDER: dict[str, int] = {
    "relay": 0, "governance": 1, "execution": 2, "outpost": 3,
    "research": 4, "observer": 5, "sandbox": 6, "generic": 7,
}

TEMPO_MIN = 72.0
TEMPO_MAX = 112.0

LOOP_BARS = 8
BEATS_PER_BAR = 4
GRID_DIV = 4
TICKS_PER_BAR = BEATS_PER_BAR * GRID_DIV  # 16
TOTAL_TICKS = LOOP_BARS * TICKS_PER_BAR  # 128

TARGET_DURATION = 48.0

VALID_WAVEFORMS = {"sine", "triangle", "square"}
VALID_EVENT_TYPES = {"node_activity", "flow_call", "flow_response"}

COMPOSITION_SCHEMA_VERSION = 1


# ── Numerical canonicalization ──────────────────────────────────────────────


def canonical_float(value: float) -> float:
    """Round to six decimal places. Reject NaN and Infinity."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"canonical_float: got {value}")
    return round(value, 6)


def _fmt_f(value: float) -> float:
    return canonical_float(value)


# ── Voice definition ────────────────────────────────────────────────────────


@dataclass
class Voice:
    voice_id: str             # canonical "owner/repo"
    display_id: str           # short node name
    role: str
    waveform: str
    base_midi_note: float
    octave_offset: int
    scale_index: int
    normalized_activity: float
    note_count: int
    active: bool
    feed_available: bool
    phase_offset: int

    def to_dict(self) -> dict:
        return {
            "voice_id": self.voice_id,
            "display_id": self.display_id,
            "role": self.role,
            "waveform": self.waveform,
            "base_midi_note": _fmt_f(self.base_midi_note),
            "octave_offset": self.octave_offset,
            "scale_index": self.scale_index,
            "normalized_activity": _fmt_f(self.normalized_activity),
            "note_count": self.note_count,
            "active": self.active,
            "feed_available": self.feed_available,
            "phase_offset": self.phase_offset,
        }

    @staticmethod
    def from_dict(data: dict) -> Voice:
        return Voice(
            voice_id=_require_str(data, "voice_id"),
            display_id=_require_str(data, "display_id"),
            role=_require_str(data, "role"),
            waveform=_require_str(data, "waveform"),
            base_midi_note=_require_float_range(data, "base_midi_note", 20.0, 120.0),
            octave_offset=_require_int_range(data, "octave_offset", -3, 3),
            scale_index=_require_int_range(data, "scale_index", 0, 10),
            normalized_activity=_require_float_range(data, "normalized_activity", 0.0, 1.0),
            note_count=_require_int_range(data, "note_count", 0, 128),
            active=_require_bool(data, "active"),
            feed_available=_require_bool(data, "feed_available"),
            phase_offset=_require_int_range(data, "phase_offset", 0, 127),
        )


# ── Note event ──────────────────────────────────────────────────────────────


@dataclass
class NoteEvent:
    event_id: str = ""
    event_type: str = "node_activity"
    start_tick: int = 0
    duration_ticks: int = 1
    midi_note: float = 60.0
    frequency_hz: float = 261.625565
    velocity: float = 0.5
    voice_id: str = ""
    waveform: str = "sine"
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "start_tick": self.start_tick,
            "duration_ticks": self.duration_ticks,
            "midi_note": _fmt_f(self.midi_note),
            "frequency_hz": _fmt_f(self.frequency_hz),
            "velocity": _fmt_f(self.velocity),
            "voice_id": self.voice_id,
            "waveform": self.waveform,
            "provenance": dict(self.provenance),
        }

    @staticmethod
    def from_dict(data: dict) -> NoteEvent:
        return NoteEvent(
            event_id=_require_str(data, "event_id"),
            event_type=_require_str(data, "event_type"),
            start_tick=_require_int_range(data, "start_tick", 0, 127),
            duration_ticks=_require_int_range(data, "duration_ticks", 1, 8),
            midi_note=_require_float_range(data, "midi_note", 20.0, 120.0),
            frequency_hz=canonical_float(data.get("frequency_hz", 0.0)),
            velocity=_require_float_range(data, "velocity", 0.0, 1.0),
            voice_id=_require_str(data, "voice_id"),
            waveform=_require_str(data, "waveform"),
            provenance=dict(data.get("provenance", {})),
        )

    @property
    def sort_key(self):
        prov = self.provenance
        if self.event_type == "node_activity":
            prov_tuple = (prov.get("node", ""),)
        else:
            prov_tuple = (
                prov.get("flow_id", ""),
                prov.get("pair_id", ""),
                prov.get("source", ""),
                prov.get("target", ""),
                prov.get("flow_weight", 0),
            )
        return (
            self.start_tick,
            self.voice_id,
            self.event_type,
            self.midi_note,
            self.duration_ticks,
            self.velocity,
            prov_tuple,
        )


# ── Composition ─────────────────────────────────────────────────────────────


@dataclass
class Composition:
    schema_version: int = COMPOSITION_SCHEMA_VERSION
    semantic_snapshot_sha256: str = ""
    composition_sha256: str = ""
    tempo_bpm: float = 90.0
    root_midi: float = 60.0
    scale_name: str = "minor_pentatonic"
    scale_intervals: list[int] = field(default_factory=lambda: [0, 3, 5, 7, 10])
    bars: int = LOOP_BARS
    beats_per_bar: int = BEATS_PER_BAR
    ticks_per_beat: int = GRID_DIV
    total_ticks: int = TOTAL_TICKS
    loop_duration_seconds: float = 0.0
    repeat_count: int = 1
    render_duration_seconds: float = 0.0
    voices: list[Voice] = field(default_factory=list)
    events: list[NoteEvent] = field(default_factory=list)

    # ── Serialization ──────────────────────────────────────────────────

    def semantic_dict(self) -> dict:
        """All canonical fields EXCEPT composition_sha256."""
        return {
            "schema_version": self.schema_version,
            "semantic_snapshot_sha256": self.semantic_snapshot_sha256,
            "tempo_bpm": _fmt_f(self.tempo_bpm),
            "root_midi": _fmt_f(self.root_midi),
            "scale": {
                "name": self.scale_name,
                "intervals": list(self.scale_intervals),
            },
            "grid": {
                "bars": self.bars,
                "beats_per_bar": self.beats_per_bar,
                "ticks_per_beat": self.ticks_per_beat,
                "total_ticks": self.total_ticks,
            },
            "loop_duration_seconds": _fmt_f(self.loop_duration_seconds),
            "repeat_count": self.repeat_count,
            "render_duration_seconds": _fmt_f(self.render_duration_seconds),
            "voices": [v.to_dict() for v in self.voices],
            "events": [e.to_dict() for e in self.events],
        }

    def semantic_bytes(self) -> bytes:
        return json.dumps(
            self.semantic_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    def semantic_hash(self) -> str:
        return hashlib.sha256(self.semantic_bytes()).hexdigest()

    def to_artifact_dict(self) -> dict:
        d = self.semantic_dict()
        d["composition_sha256"] = self.semantic_hash()
        return d

    def to_artifact_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_artifact_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def from_dict(data: dict) -> Composition:
        sv = data.get("schema_version")
        if sv != COMPOSITION_SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema_version: {sv!r}")

        embedded_hash = _require_str(data, "composition_sha256")
        snapshot_hash = _require_str(data, "semantic_snapshot_sha256")

        tempo = _require_float_range(data, "tempo_bpm", 72.0, 112.0)
        root_midi = _require_float_range(data, "root_midi", 20.0, 120.0)

        scale = data.get("scale")
        if not isinstance(scale, dict):
            raise ValueError("scale must be a dict")
        scale_name = _require_str(scale, "name")
        intervals = scale.get("intervals")
        if not isinstance(intervals, list) or not all(isinstance(i, int) for i in intervals):
            raise ValueError("scale.intervals must be a list of ints")

        grid = data.get("grid")
        if not isinstance(grid, dict):
            raise ValueError("grid must be a dict")
        bars = _require_int_range(grid, "bars", 1, 32)
        beats_per_bar = _require_int_range(grid, "beats_per_bar", 1, 16)
        ticks_per_beat = _require_int_range(grid, "ticks_per_beat", 1, 16)
        total_ticks = _require_int_range(grid, "total_ticks", 1, 4096)

        loop_dur = canonical_float(data.get("loop_duration_seconds", 0.0))
        repeat_count = _require_int_range(data, "repeat_count", 1, 100)
        render_dur = canonical_float(data.get("render_duration_seconds", 0.0))

        # Validate derived fields
        expected_loop = canonical_float((bars * beats_per_bar * 60.0) / (tempo * ticks_per_beat))
        if abs(loop_dur - expected_loop) > 1e-6:
            raise ValueError(
                f"loop_duration_seconds {loop_dur} does not match "
                f"expected {expected_loop} (tolerance 1e-6)"
            )
        expected_render = canonical_float(loop_dur * repeat_count)
        if abs(render_dur - expected_render) > 1e-6:
            raise ValueError(
                f"render_duration_seconds {render_dur} does not match "
                f"expected {expected_render} (tolerance 1e-6)"
            )

        # Validate voices
        raw_voices = data.get("voices")
        if not isinstance(raw_voices, list):
            raise ValueError("voices must be a list")
        voices = [Voice.from_dict(v) for v in raw_voices]
        voice_ids = {v.voice_id for v in voices}

        # Validate events
        raw_events = data.get("events")
        if not isinstance(raw_events, list):
            raise ValueError("events must be a list")
        events: list[NoteEvent] = []
        seen_event_ids: set[str] = set()
        pair_map: dict[str, list[NoteEvent]] = {}

        for e in raw_events:
            evt = NoteEvent.from_dict(e)
            if evt.event_id in seen_event_ids:
                raise ValueError(f"duplicate event_id: {evt.event_id}")
            seen_event_ids.add(evt.event_id)
            if evt.voice_id not in voice_ids:
                raise ValueError(f"event {evt.event_id} references unknown voice {evt.voice_id}")
            if evt.waveform not in VALID_WAVEFORMS:
                raise ValueError(f"unknown waveform: {evt.waveform}")
            if evt.event_type not in VALID_EVENT_TYPES:
                raise ValueError(f"unknown event_type: {evt.event_type}")

            # Validate provenance
            prov = evt.provenance
            if evt.event_type == "node_activity":
                if "node" not in prov:
                    raise ValueError(f"event {evt.event_id}: node_activity missing provenance.node")
            elif evt.event_type in ("flow_call", "flow_response"):
                for k in ("flow_id", "pair_id", "source", "target"):
                    if k not in prov:
                        raise ValueError(f"event {evt.event_id}: missing provenance.{k}")
                pair_id = prov["pair_id"]
                pair_map.setdefault(pair_id, []).append(evt)

            # Validate frequency
            expected_freq = canonical_float(440.0 * math.pow(2.0, (evt.midi_note - 69.0) / 12.0))
            if abs(evt.frequency_hz - expected_freq) > 0.01:
                raise ValueError(
                    f"event {evt.event_id}: frequency {evt.frequency_hz} does not match "
                    f"midi_note {evt.midi_note} (expected {expected_freq}, tolerance 0.01)"
                )

            events.append(evt)

        # Validate pair invariants
        for pair_id, pair_events in pair_map.items():
            if len(pair_events) != 2:
                raise ValueError(f"pair_id {pair_id}: expected 2 events, got {len(pair_events)}")
            types = {e.event_type for e in pair_events}
            if types != {"flow_call", "flow_response"}:
                raise ValueError(f"pair_id {pair_id}: expected one flow_call + one flow_response, got {types}")
            flow_ids = {e.provenance.get("flow_id") for e in pair_events}
            if len(flow_ids) != 1:
                raise ValueError(f"pair_id {pair_id}: inconsistent flow_id across pair")

        comp = Composition(
            schema_version=sv,
            semantic_snapshot_sha256=snapshot_hash,
            composition_sha256="",  # recomputed below
            tempo_bpm=tempo,
            root_midi=root_midi,
            scale_name=scale_name,
            scale_intervals=list(intervals),
            bars=bars,
            beats_per_bar=beats_per_bar,
            ticks_per_beat=ticks_per_beat,
            total_ticks=total_ticks,
            loop_duration_seconds=loop_dur,
            repeat_count=repeat_count,
            render_duration_seconds=render_dur,
            voices=voices,
            events=events,
        )

        computed_hash = comp.semantic_hash()
        if computed_hash != embedded_hash:
            raise ValueError(
                f"composition_sha256 mismatch: embedded {embedded_hash[:16]}..., "
                f"computed {computed_hash[:16]}..."
            )

        comp.composition_sha256 = computed_hash
        return comp


# ── Validation helpers ──────────────────────────────────────────────────────


def _require_str(obj: dict, key: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val:
        raise ValueError(f"'{key}' must be a non-empty string")
    return val


def _require_bool(obj: dict, key: str) -> bool:
    val = obj.get(key)
    if not isinstance(val, bool):
        raise ValueError(f"'{key}' must be a bool")
    return val


def _require_int_range(obj: dict, key: str, lo: int, hi: int) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or val < lo or val > hi:
        raise ValueError(f"'{key}' must be int in [{lo}, {hi}], got {type(val).__name__}")
    return val


def _require_float_range(obj: dict, key: str, lo: float, hi: float) -> float:
    val = obj.get(key)
    if not isinstance(val, (int, float)):
        raise ValueError(f"'{key}' must be a number")
    v = canonical_float(float(val))
    if v < lo or v > hi:
        raise ValueError(f"'{key}' must be in [{lo}, {hi}], got {v}")
    return v


# ── Core utilities ──────────────────────────────────────────────────────────


def _midi_to_freq(midi: float) -> float:
    return canonical_float(440.0 * math.pow(2.0, (midi - 69.0) / 12.0))


def _stable_hash(value: str, seed: int) -> int:
    h = hashlib.sha256(f"{seed}:{value}".encode()).digest()
    return int.from_bytes(h[:4], "big")


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ── Composition engine ──────────────────────────────────────────────────────


def compose(snapshot: NormalizedSnapshot) -> Composition:
    """Transform a normalized snapshot into a deterministic canonical Composition."""
    seed = snapshot.seed()

    # ── Root note ──────────────────────────────────────────────────────
    root_idx = seed % len(ROOTS)
    root_midi = ROOTS[root_idx]
    pitches = [root_midi + s for s in MINOR_PENTATONIC]

    # ── Tempo from in_flight ───────────────────────────────────────────
    in_flight = snapshot.pulse.in_flight
    if in_flight > 0:
        tempo = TEMPO_MIN + (TEMPO_MAX - TEMPO_MIN) * min(
            1.0, math.log1p(in_flight) / math.log1p(500)
        )
    else:
        tempo = TEMPO_MIN
    tempo = _clamp(tempo, TEMPO_MIN, TEMPO_MAX)

    # ── Loop timing ────────────────────────────────────────────────────
    loop_dur = canonical_float((LOOP_BARS * BEATS_PER_BAR * 60.0) / (tempo * GRID_DIV))
    repeats = max(1, round(TARGET_DURATION / loop_dur)) if loop_dur > 0 else 1
    render_dur = canonical_float(loop_dur * repeats)

    # ── Per-node voice info ────────────────────────────────────────────
    if snapshot.nodes:
        max_activity = max(n.activity for n in snapshot.nodes)
    else:
        max_activity = 0

    voice_list: list[Voice] = []
    node_map: dict[str, dict] = {}
    role_lookup: dict[str, str] = {}

    for n in snapshot.nodes:
        role = n.role
        role_lookup[n.id_] = role
        role_lookup[n.full_name] = role

        if max_activity > 0:
            norm_activity = canonical_float(math.log1p(n.activity) / math.log1p(max_activity))
        else:
            norm_activity = 0.0
        norm_activity = _clamp(norm_activity, 0.0, 1.0)

        octave_offset = ROLE_OCTAVE.get(role, 0)
        waveform = ROLE_WAVEFORM.get(role, "triangle")
        node_hash = _stable_hash(n.full_name, seed)
        scale_idx = node_hash % len(pitches)
        note_midi = pitches[scale_idx] + octave_offset * 12
        phase = node_hash % 4

        if n.active and norm_activity > 0.01:
            note_count = max(2, round(norm_activity * 8))
        else:
            note_count = max(0, round(norm_activity * 2))

        voice = Voice(
            voice_id=n.full_name,
            display_id=n.id_,
            role=role,
            waveform=waveform,
            base_midi_note=canonical_float(note_midi),
            octave_offset=octave_offset,
            scale_index=scale_idx,
            normalized_activity=norm_activity,
            note_count=note_count,
            active=n.active,
            feed_available=n.feed_available,
            phase_offset=phase,
        )
        voice_list.append(voice)

        voice_info = {
            "midi": note_midi,
            "waveform": waveform,
            "activity": norm_activity,
            "note_count": note_count,
            "phase": phase,
            "scale_idx": scale_idx,
            "node_hash": node_hash,
        }
        node_map[n.id_] = voice_info
        node_map[n.full_name] = voice_info

    # ── Sort voices canonically ────────────────────────────────────────
    voice_list.sort(key=lambda v: (ROLE_ORDER.get(v.role, 99), v.voice_id))

    # ── Node activity events ───────────────────────────────────────────
    raw_events: list[NoteEvent] = []

    for n in snapshot.nodes:
        info = node_map[n.id_]
        n_count = info["note_count"]
        if n_count == 0:
            continue
        available_ticks = TOTAL_TICKS
        if n_count > available_ticks:
            n_count = available_ticks
        step = available_ticks // n_count if n_count > 0 else available_ticks
        for i in range(n_count):
            tick = (
                info["phase"] * GRID_DIV
                + i * step
                + (info["node_hash"] + i) % max(1, step // 2)
            ) % TOTAL_TICKS
            velocity = _clamp(0.2 + info["activity"] * 0.6, 0.15, 0.85)
            dur = max(1, min(4, round(1 + info["activity"] * 3)))
            midi_note = info["midi"]
            raw_events.append(NoteEvent(
                event_type="node_activity",
                start_tick=tick,
                duration_ticks=dur,
                midi_note=canonical_float(midi_note),
                frequency_hz=_midi_to_freq(midi_note),
                velocity=canonical_float(velocity),
                voice_id=n.full_name,
                waveform=info["waveform"],
                provenance={"node": n.full_name},
            ))

    # ── Flow events: call-and-response ─────────────────────────────────
    if snapshot.flows:
        max_weight = max(f.weight for f in snapshot.flows)
        ranked = sorted(snapshot.flows, key=lambda f: (-f.weight, f.source, f.target))
        top_flows = ranked[:min(len(ranked), 12)]

        for flow_idx, flow in enumerate(top_flows):
            if flow.source not in node_map or flow.target not in node_map:
                continue
            if flow.weight <= 0:
                continue
            src_info = node_map[flow.source]
            tgt_info = node_map[flow.target]
            norm_weight = canonical_float(flow.weight / max(max_weight, 1))

            reps = max(1, round(norm_weight * 4))
            flow_id = f"flow-{flow_idx:04d}"

            for r in range(reps):
                pair_id = f"{flow_id}-pair-{r:04d}"
                tick = (TOTAL_TICKS // 2 + flow_idx * 8 + r * (TOTAL_TICKS // reps)) % TOTAL_TICKS

                # Call
                call_midi = canonical_float(src_info["midi"])
                raw_events.append(NoteEvent(
                    event_type="flow_call",
                    start_tick=tick,
                    duration_ticks=2,
                    midi_note=call_midi,
                    frequency_hz=_midi_to_freq(call_midi),
                    velocity=canonical_float(_clamp(0.25 + norm_weight * 0.35, 0.2, 0.75)),
                    voice_id=flow.source,
                    waveform=src_info["waveform"],
                    provenance={
                        "flow_id": flow_id,
                        "pair_id": pair_id,
                        "source": flow.source,
                        "target": flow.target,
                        "flow_weight": flow.weight,
                    },
                ))

                # Response
                interval = (tgt_info["scale_idx"] - src_info["scale_idx"]) % len(MINOR_PENTATONIC)
                resp_midi = canonical_float(src_info["midi"] + MINOR_PENTATONIC[interval])
                resp_tick = (tick + 2) % TOTAL_TICKS
                raw_events.append(NoteEvent(
                    event_type="flow_response",
                    start_tick=resp_tick,
                    duration_ticks=2,
                    midi_note=resp_midi,
                    frequency_hz=_midi_to_freq(resp_midi),
                    velocity=canonical_float(_clamp(0.25 + norm_weight * 0.35, 0.2, 0.75)),
                    voice_id=flow.target,
                    waveform=tgt_info["waveform"],
                    provenance={
                        "flow_id": flow_id,
                        "pair_id": pair_id,
                        "source": flow.source,
                        "target": flow.target,
                        "flow_weight": flow.weight,
                    },
                ))

    # ── Sort and assign event IDs ──────────────────────────────────────
    raw_events.sort(key=lambda e: e.sort_key)

    dup_counter: dict[tuple, int] = {}
    events: list[NoteEvent] = []
    for idx, evt in enumerate(raw_events):
        key = evt.sort_key
        count = dup_counter.get(key, 0)
        dup_counter[key] = count + 1
        if count > 0:
            evt.event_id = f"evt-{idx:06d}-dup-{count}"
        else:
            evt.event_id = f"evt-{idx:06d}"
        events.append(evt)

    comp = Composition(
        semantic_snapshot_sha256=snapshot.semantic_hash(),
        tempo_bpm=canonical_float(tempo),
        root_midi=canonical_float(root_midi),
        scale_intervals=list(MINOR_PENTATONIC),
        loop_duration_seconds=loop_dur,
        repeat_count=repeats,
        render_duration_seconds=render_dur,
        voices=voice_list,
        events=events,
    )
    comp.composition_sha256 = comp.semantic_hash()
    return comp


def validate_composition(comp: Composition) -> list[str]:
    """Validate composition invariants. Returns list of error messages."""
    errors: list[str] = []

    # Recompute and verify hash
    computed = comp.semantic_hash()
    if computed != comp.composition_sha256:
        errors.append(f"composition_sha256 mismatch")

    if comp.tempo_bpm < TEMPO_MIN or comp.tempo_bpm > TEMPO_MAX:
        errors.append(f"tempo out of range: {comp.tempo_bpm}")

    if comp.repeat_count < 1:
        errors.append("repeat_count must be positive")

    if comp.loop_duration_seconds <= 0:
        errors.append("loop_duration_seconds must be positive")

    expected_loop = canonical_float(
        (comp.bars * comp.beats_per_bar * 60.0) / (comp.tempo_bpm * comp.ticks_per_beat)
    )
    if abs(comp.loop_duration_seconds - expected_loop) > 1e-6:
        errors.append(f"loop_duration mismatch: {comp.loop_duration_seconds} vs {expected_loop}")

    expected_render = canonical_float(comp.loop_duration_seconds * comp.repeat_count)
    if abs(comp.render_duration_seconds - expected_render) > 1e-6:
        errors.append(f"render_duration mismatch: {comp.render_duration_seconds} vs {expected_render}")

    voice_ids = {v.voice_id for v in comp.voices}
    seen_ids: set[str] = set()
    pair_map: dict[str, list[NoteEvent]] = {}

    for evt in comp.events:
        if evt.event_id in seen_ids:
            errors.append(f"duplicate event_id: {evt.event_id}")
        seen_ids.add(evt.event_id)

        if evt.voice_id not in voice_ids:
            errors.append(f"event {evt.event_id}: unknown voice {evt.voice_id}")

        if evt.waveform not in VALID_WAVEFORMS:
            errors.append(f"event {evt.event_id}: invalid waveform {evt.waveform}")

        if evt.event_type not in VALID_EVENT_TYPES:
            errors.append(f"event {evt.event_id}: invalid event_type {evt.event_type}")

        if evt.start_tick < 0 or evt.start_tick >= comp.total_ticks:
            errors.append(f"event {evt.event_id}: start_tick out of range")

        if evt.duration_ticks < 1 or evt.duration_ticks > 8:
            errors.append(f"event {evt.event_id}: duration_ticks out of range")

        if evt.velocity < 0.0 or evt.velocity > 1.0:
            errors.append(f"event {evt.event_id}: velocity out of range")

        expected_freq = canonical_float(
            440.0 * math.pow(2.0, (evt.midi_note - 69.0) / 12.0)
        )
        if abs(evt.frequency_hz - expected_freq) > 0.01:
            errors.append(f"event {evt.event_id}: frequency mismatch")

        if evt.event_type in ("flow_call", "flow_response"):
            pair_id = evt.provenance.get("pair_id", "")
            pair_map.setdefault(pair_id, []).append(evt)

    for pair_id, pair_events in pair_map.items():
        if not pair_id:
            continue
        if len(pair_events) != 2:
            errors.append(f"pair {pair_id}: expected 2 events, got {len(pair_events)}")
        else:
            types = {e.event_type for e in pair_events}
            if types != {"flow_call", "flow_response"}:
                errors.append(f"pair {pair_id}: wrong types {types}")

    return errors
