"""Deterministic SVG visual score from Composition.

Consumes only Composition — never NormalizedSnapshot.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .compose import Composition, ROLE_ORDER

# ── Layout constants ────────────────────────────────────────────────────────

LEFT_GUTTER = 220
PIXELS_PER_TICK = 8
TIMELINE_WIDTH = 128 * PIXELS_PER_TICK  # 1024
RIGHT_PADDING = 24
CANVAS_WIDTH = LEFT_GUTTER + TIMELINE_WIDTH + RIGHT_PADDING  # 1268
LANE_HEIGHT = 24
HEADER_HEIGHT = 80
LEGEND_HEIGHT = 40
NOTE_HEIGHT = 20

SVG_NS = "http://www.w3.org/2000/svg"

ROLE_COLORS: dict[str, str] = {
    "relay": "#2563eb",
    "governance": "#7c3aed",
    "execution": "#059669",
    "outpost": "#d97706",
    "research": "#dc2626",
    "observer": "#0891b2",
    "sandbox": "#9ca3af",
    "generic": "#6b7280",
}

GRID_COLOR = "#e5e7eb"
BAR_COLOR = "#d1d5db"


def _canvas_height(voice_count: int) -> int:
    return HEADER_HEIGHT + voice_count * LANE_HEIGHT + LEGEND_HEIGHT


# ── SVG construction ────────────────────────────────────────────────────────


def render_svg(composition: Composition, output: Path) -> dict:
    """Render a deterministic SVG score from a Composition.

    Returns a dict with keys: valid (bool), errors (list[str]),
    svg_sha256 (str).
    """
    errors: list[str] = []
    voice_count = len(composition.voices)

    # Sort voices canonically
    voices = sorted(
        composition.voices,
        key=lambda v: (ROLE_ORDER.get(v.role, 99), v.voice_id),
    )
    voice_index: dict[str, int] = {v.voice_id: i for i, v in enumerate(voices)}

    canvas_h = _canvas_height(voice_count)

    # ── Build SVG tree ────────────────────────────────────────────────
    svg = ET.Element(
        "svg",
        {
            "xmlns": SVG_NS,
            "width": str(CANVAS_WIDTH),
            "height": str(canvas_h),
            "viewBox": f"0 0 {CANVAS_WIDTH} {canvas_h}",
        },
    )

    title = ET.SubElement(svg, "title")
    title.text = "Agent Music — Federation Score"

    desc = ET.SubElement(svg, "desc")
    desc.text = (
        f"Deterministic visual score of the federation composition. "
        f"{voice_count} voices, {len(composition.events)} events, "
        f"{composition.tempo_bpm:.0f} BPM, "
        f"loop {composition.loop_duration_seconds:.1f}s × {composition.repeat_count}."
    )

    # Style
    style = ET.SubElement(svg, "style")
    style.text = (
        f".bg {{ fill: #fafafa; }} "
        f".grid {{ stroke: {GRID_COLOR}; stroke-width: 1; }} "
        f".bar {{ stroke: {BAR_COLOR}; stroke-width: 1; }} "
        f".beat {{ stroke: {GRID_COLOR}; stroke-width: 0.5; stroke-dasharray: 2,2; }} "
        f".label {{ font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; font-size: 11px; fill: #1a1a1a; }} "
        f".header {{ font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; font-size: 12px; fill: #1a1a1a; }} "
        f".connector {{ stroke: #6b7280; stroke-width: 1; opacity: 0.4; fill: none; }} "
        f".flow-call {{ fill: #374151; }} "
        f".flow-response {{ fill: #9ca3af; }} "
    )

    # Background
    bg = ET.SubElement(svg, "rect", {"class": "bg", "x": "0", "y": "0", "width": str(CANVAS_WIDTH), "height": str(canvas_h)})

    # ── Grid ──────────────────────────────────────────────────────────
    timeline_x = LEFT_GUTTER
    for tick in range(0, composition.total_ticks + 1):
        x = timeline_x + tick * PIXELS_PER_TICK
        cls = "bar" if tick % 16 == 0 else ("beat" if tick % 4 == 0 else "grid")
        if tick % 16 == 0 or tick % 4 == 0:
            ET.SubElement(svg, "line", {
                "class": cls,
                "x1": str(x), "y1": str(HEADER_HEIGHT),
                "x2": str(x), "y2": str(HEADER_HEIGHT + voice_count * LANE_HEIGHT),
            })

    # ── Voice lanes ───────────────────────────────────────────────────
    for v_idx, voice in enumerate(voices):
        y = HEADER_HEIGHT + v_idx * LANE_HEIGHT
        lane_label = f"{xml_escape(voice.display_id)} ({voice.role}, {voice.waveform})"
        ET.SubElement(svg, "text", {
            "class": "label",
            "x": str(LEFT_GUTTER - 8),
            "y": str(y + 15),
            "text-anchor": "end",
        }).text = lane_label

    # ── Events ────────────────────────────────────────────────────────
    for evt in composition.events:
        v_idx = voice_index.get(evt.voice_id)
        if v_idx is None:
            continue
        y = HEADER_HEIGHT + v_idx * LANE_HEIGHT + 2
        x = timeline_x + evt.start_tick * PIXELS_PER_TICK
        width = evt.duration_ticks * PIXELS_PER_TICK

        if evt.event_type == "node_activity":
            voice = voices[v_idx]
            color = ROLE_COLORS.get(voice.role, ROLE_COLORS["generic"])
            ET.SubElement(svg, "rect", {
                "id": evt.event_id,
                "data-event-type": evt.event_type,
                "data-voice-id": evt.voice_id,
                "data-start-tick": str(evt.start_tick),
                "data-duration-ticks": str(evt.duration_ticks),
                "data-event-id": evt.event_id,
                "x": str(x),
                "y": str(y),
                "width": str(width),
                "height": str(NOTE_HEIGHT),
                "fill": color,
            })
        elif evt.event_type in ("flow_call", "flow_response"):
            direction = "right" if evt.event_type == "flow_call" else "left"
            cls = "flow-call" if evt.event_type == "flow_call" else "flow-response"
            cx = x + width // 2
            cy = y + NOTE_HEIGHT // 2
            r = 4
            ET.SubElement(svg, "polygon", {
                "id": evt.event_id,
                "data-event-type": evt.event_type,
                "data-voice-id": evt.voice_id,
                "data-start-tick": str(evt.start_tick),
                "data-duration-ticks": str(evt.duration_ticks),
                "data-event-id": evt.event_id,
                "data-flow-id": evt.provenance.get("flow_id", ""),
                "data-pair-id": evt.provenance.get("pair_id", ""),
                "class": cls,
                "points": (
                    f"{cx - r},{cy - r} {cx + r},{cy} {cx - r},{cy + r}"
                    if direction == "left" else
                    f"{cx + r},{cy - r} {cx - r},{cy} {cx + r},{cy + r}"
                ),
            })

    # ── Flow connectors ───────────────────────────────────────────────
    # One connector per pair_id, anchored to call and response elements
    pair_events: dict[str, dict[str, tuple[int, int, int]]] = {}
    for evt in composition.events:
        pid = evt.provenance.get("pair_id")
        if not pid:
            continue
        if pid not in pair_events:
            pair_events[pid] = {}
        v_idx = voice_index.get(evt.voice_id)
        if v_idx is None:
            continue
        x = timeline_x + evt.start_tick * PIXELS_PER_TICK + PIXELS_PER_TICK  # center of marker
        y = HEADER_HEIGHT + v_idx * LANE_HEIGHT + 2 + NOTE_HEIGHT // 2
        pair_events[pid][evt.event_type] = (x, y, v_idx)

    for pid, parts in pair_events.items():
        if "flow_call" not in parts or "flow_response" not in parts:
            continue
        call_x, call_y, call_vi = parts["flow_call"]
        resp_x, resp_y, resp_vi = parts["flow_response"]
        # Only draw connector if on different lanes
        if call_vi != resp_vi:
            ET.SubElement(svg, "line", {
                "class": "connector",
                "data-flow-id": pid.split("-pair-")[0] if "-pair-" in pid else "",
                "data-pair-id": pid,
                "x1": str(call_x),
                "y1": str(call_y),
                "x2": str(resp_x),
                "y2": str(resp_y),
            })

    # ── Header ────────────────────────────────────────────────────────
    hash_short = composition.composition_sha256[:16] if composition.composition_sha256 else "?"
    snap_short = composition.semantic_snapshot_sha256[:16] if composition.semantic_snapshot_sha256 else "?"
    lines = [
        "Agent Music — Federation Score",
        f"Tempo: {composition.tempo_bpm:.0f} BPM  ·  "
        f"Root: MIDI {composition.root_midi:.0f}  ·  "
        f"Scale: {composition.scale_name}",
        f"{voice_count} voices  ·  {len(composition.events)} events  ·  "
        f"{composition.repeat_count}× loop = {composition.render_duration_seconds:.1f} s",
        f"Snapshot: {snap_short}…  ·  Composition: {hash_short}…",
    ]
    for i, line in enumerate(lines):
        ET.SubElement(svg, "text", {
            "class": "header",
            "x": str(LEFT_GUTTER),
            "y": str(20 + i * 16),
        }).text = line

    # ── Legend ────────────────────────────────────────────────────────
    legend_y = HEADER_HEIGHT + voice_count * LANE_HEIGHT + 16
    legend_parts = [
        ("█ note (activity)", ROLE_COLORS["generic"]),
        ("► flow call", "#374151"),
        ("◄ flow response", "#9ca3af"),
        ("╌ flow connector", "#6b7280"),
    ]
    for i, (label, color) in enumerate(legend_parts):
        x = LEFT_GUTTER + i * 180
        ET.SubElement(svg, "text", {
            "class": "label",
            "x": str(x),
            "y": str(legend_y),
            "fill": color,
        }).text = label

    # ── Serialize ─────────────────────────────────────────────────────
    raw = ET.tostring(svg, encoding="utf-8", xml_declaration=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)

    svg_hash = hashlib.sha256(raw).hexdigest()
    return {
        "valid": True,
        "errors": [],
        "svg_sha256": svg_hash,
    }


# ── SVG validation ──────────────────────────────────────────────────────────


def validate_svg(path: Path, composition: Composition) -> dict:
    """Validate a rendered SVG against composition invariants.

    Returns dict with: valid (bool), errors (list[str]), svg_sha256 (str).
    """
    errors: list[str] = []

    if not path.exists():
        return {"valid": False, "errors": ["file not found"], "svg_sha256": ""}

    raw = path.read_bytes()
    if not raw.strip():
        return {"valid": False, "errors": ["empty file"], "svg_sha256": ""}

    svg_sha256 = hashlib.sha256(raw).hexdigest()

    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as e:
        errors.append(f"invalid XML: {e}")
        return {"valid": False, "errors": errors, "svg_sha256": svg_sha256}

    # Check root
    if tree.tag != f"{{{SVG_NS}}}svg":
        errors.append(f"root is not svg: {tree.tag}")

    # No scripts
    scripts = tree.findall(".//{http://www.w3.org/2000/svg}script")
    if scripts:
        errors.append(f"found {len(scripts)} script element(s)")

    # No external references
    for el in tree.iter():
        for attr in ("href", "{http://www.w3.org/1999/xlink}href"):
            val = el.get(attr, "")
            if val and (val.startswith("http://") or val.startswith("https://")):
                errors.append(f"external reference: {val}")

    # Check dimensions
    w = tree.get("width", "0")
    if w:
        try:
            if int(w) <= 0:
                errors.append("non-positive width")
        except ValueError:
            errors.append("invalid width")

    # Count event elements
    event_els = tree.findall(f".//*[@data-event-id]")
    event_ids_found = set()
    for el in event_els:
        eid = el.get("data-event-id", "")
        if eid in event_ids_found:
            errors.append(f"duplicate event element: {eid}")
        event_ids_found.add(eid)

    comp_event_ids = {e.event_id for e in composition.events}
    if len(event_els) != len(comp_event_ids):
        errors.append(f"event element count {len(event_els)} != composition event count {len(comp_event_ids)}")

    missing = comp_event_ids - event_ids_found
    if missing:
        errors.append(f"missing event elements: {len(missing)}")

    # Count connectors
    connectors = tree.findall(".//{http://www.w3.org/2000/svg}line[@data-pair-id]")
    pair_ids = {
        e.provenance["pair_id"]
        for e in composition.events
        if e.provenance.get("pair_id")
    }
    # Only pairs on different lanes get connectors
    voice_index = {
        v.voice_id: i
        for i, v in enumerate(
            sorted(composition.voices, key=lambda v: (ROLE_ORDER.get(v.role, 99), v.voice_id))
        )
    }
    expected_connectors = 0
    for pid in pair_ids:
        pair_evts = [e for e in composition.events if e.provenance.get("pair_id") == pid]
        if len(pair_evts) == 2:
            vi0 = voice_index.get(pair_evts[0].voice_id)
            vi1 = voice_index.get(pair_evts[1].voice_id)
            if vi0 is not None and vi1 is not None and vi0 != vi1:
                expected_connectors += 1

    if len(connectors) != expected_connectors:
        errors.append(f"connector count {len(connectors)} != expected {expected_connectors}")

    valid = len(errors) == 0
    return {"valid": valid, "errors": errors, "svg_sha256": svg_sha256}
