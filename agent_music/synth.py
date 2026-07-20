"""Synthesize a Composition into floating-point audio samples.

Pure Python, no external audio dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .compose import Composition, NoteEvent, GRID_DIV, _midi_to_freq

SAMPLE_RATE = 16000  # Hz
BITS_PER_SAMPLE = 16
MAX_AMP = 32767  # 2^15 - 1


@dataclass
class SynthParams:
    attack: float = 0.02   # seconds
    decay: float = 0.05    # seconds
    sustain: float = 0.7   # amplitude ratio
    release: float = 0.08  # seconds
    headroom: float = 0.9  # peak normalization target


def _generate_sine(phase: float) -> float:
    """Sine wave at current phase (0..1)."""
    return math.sin(2.0 * math.pi * phase)


def _generate_triangle(phase: float) -> float:
    """Triangle wave at current phase (0..1)."""
    # 0..0.25 → linear up, 0.25..0.75 → linear down, 0.75..1 → linear up
    if phase < 0.25:
        return 4.0 * phase
    elif phase < 0.75:
        return 2.0 - 4.0 * phase
    else:
        return 4.0 * phase - 4.0


def _generate_soft_square(phase: float) -> float:
    """Softened square wave using a filtered approximation."""
    # Sum of odd harmonics, reduced
    val = 0.0
    val += math.sin(2.0 * math.pi * phase)
    val += 0.3 * math.sin(2.0 * math.pi * 3 * phase)
    val += 0.1 * math.sin(2.0 * math.pi * 5 * phase)
    return val / 1.4


_WAVEFORM_GEN = {
    "sine": _generate_sine,
    "triangle": _generate_triangle,
    "square": _generate_soft_square,
}


def _adsr_envelope(
    t: float,
    duration: float,
    params: SynthParams,
) -> float:
    """ADSR amplitude envelope at sample time *t* within a note of *duration* seconds."""
    a = params.attack
    d = params.decay
    s = params.sustain
    r = params.release

    if t < a:
        # Attack: linear ramp 0 → 1
        return t / a if a > 0 else 1.0
    elif t < a + d:
        # Decay: linear ramp 1 → sustain
        frac = (t - a) / d if d > 0 else 1.0
        return 1.0 - (1.0 - s) * frac
    elif t < duration - r:
        # Sustain
        return s
    else:
        # Release: linear ramp sustain → 0
        release_t = t - (duration - r)
        if r > 0 and release_t < r:
            return s * (1.0 - release_t / r)
        return 0.0


def synth(composition: Composition, params: SynthParams | None = None) -> list[float]:
    """Synthesize a Composition into floating-point mono samples."""
    if params is None:
        params = SynthParams()

    tempo = composition.tempo_bpm
    # Seconds per 16th note
    sec_per_tick = 60.0 / tempo / GRID_DIV
    loop_duration = composition.loop_duration_seconds
    total_samples = int(loop_duration * SAMPLE_RATE)

    if total_samples <= 0:
        return [0.0] * 44100  # 1 second of silence as fallback

    samples = [0.0] * total_samples

    for event in composition.events:
        gen = _WAVEFORM_GEN.get(event.waveform, _generate_sine)
        freq = _midi_to_freq(event.midi_note)
        note_start_sec = event.start_tick * sec_per_tick
        note_dur_sec = event.duration_ticks * sec_per_tick
        note_start_sample = int(note_start_sec * SAMPLE_RATE)
        note_end_sample = int((note_start_sec + note_dur_sec) * SAMPLE_RATE)

        # Extend note_end_sample for release tail
        note_end_sample += int(params.release * SAMPLE_RATE)
        note_end_sample = min(note_end_sample, total_samples)

        phase = 0.0
        phase_increment = freq / SAMPLE_RATE

        for i in range(max(0, note_start_sample), note_end_sample):
            t_sec = (i - note_start_sample) / SAMPLE_RATE
            envelope = _adsr_envelope(t_sec, note_dur_sec, params)
            if envelope <= 0.0:
                continue
            samples[i] += gen(phase) * envelope * event.velocity * 0.3  # per-voice gain
            phase += phase_increment
            phase -= int(phase)  # wrap

    # ── Peak normalization ─────────────────────────────────────────────
    peak = max(abs(s) for s in samples) if samples else 0.0
    if peak > 0.0:
        scale = params.headroom / peak
        samples = [s * scale for s in samples]

    return samples


def samples_to_pcm(samples: list[float]) -> bytes:
    """Convert float samples to 16-bit signed PCM little-endian bytes."""
    import struct
    pcm = bytearray()
    for s in samples:
        # Clamp
        s = max(-1.0, min(1.0, s))
        int_sample = int(round(s * MAX_AMP))
        pcm.extend(struct.pack("<h", int_sample))
    return bytes(pcm)
