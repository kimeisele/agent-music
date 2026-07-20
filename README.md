# agent-music

**The Agent Federation rendered as music. One evolving file. Real activity. No random AI soundtrack.**

> The Agent Federation rendered as music—an autonomous observer that periodically translates live node activity, message flows, and network pulse into one evolving audio file, replacing the previous render with the federation's latest sound.

## Why

Because a decentralized agent mesh that nobody can hear is missing half its pulse.

## How it works

Every hour, `agent-music` **discovers federation nodes dynamically** via the GitHub topic `agent-federation-node`. No participant list is maintained — any repository tagged with the topic becomes a candidate, and is validated through its `.well-known/agent-federation.json` descriptor. The validated live state is normalized into a stable snapshot and translated into a deterministic musical composition.

**Federation membership is discovered, not configured.** Adding a correctly tagged and valid federation node requires no Agent Music code change. Removing the topic or invalidating the descriptor removes the node from future observations.

| Federation Signal | Musical Property |
|---|---|
| Node count | Available voices |
| Outbox depth | Note density |
| Directed flows | Melodic call-and-response |
| In-flight messages | Tempo (72–112 BPM) |
| Feed availability | Harmonic stability |
| Communicating ratio | Rhythmic density |
| Silent nodes | Musical space / rests |

## Listen

The canonical render is always available at:

**[`https://raw.githubusercontent.com/kimeisele/agent-music/render/federation.wav`](https://raw.githubusercontent.com/kimeisele/agent-music/render/federation.wav)**

Each successful render replaces the previous one. There is exactly one current audio file.

## Musical system

- **Scale**: Minor pentatonic — arbitrary data selects notes without dissonance
- **Root note**: Deterministically chosen from the semantic snapshot hash
- **Voices**: Each active federation node gets a stable musical identity
- **Tempo**: Derived from aggregate in-flight message count
- **Synthesis**: Pure Python — sine, triangle, softened square oscillators with ADSR envelopes
- **Output**: 16 kHz mono 16-bit PCM WAV, ~48 seconds

The same federation state always produces byte-identical audio bytes (SHA-256 verified).

## Render locally

```bash
# Discover live federation and write a normalized snapshot
python -m agent_music.cli snapshot \
  --config config/federation.json \
  --output snapshot.json \
  --metadata-output snapshot-meta.json

# Render a snapshot to WAV
python -m agent_music.cli render \
  --input snapshot.json \
  --output federation.wav \
  --metadata-output render.json

# Offline: render directly from a fixture
python -m agent_music.cli render \
  --input tests/fixtures/active_federation.json \
  --output federation.wav \
  --metadata-output render.json

# Run tests
python -m pytest tests/ -q
```

## Architecture

```
agent-music/
├── agent_music/
│   ├── collect.py    — fetch federation state from seed URLs
│   ├── normalize.py  — normalize into stable internal snapshot
│   ├── compose.py    — map snapshot to musical events
│   ├── synth.py      — synthesize events into WAV samples
│   ├── wav.py        — WAV file I/O and validation
│   └── cli.py        — command-line interface
├── config/
│   └── federation.json  — seed URLs and HTTP settings
├── tests/
│   ├── fixtures/     — active, quiet, and partial federation snapshots
│   ├── test_normalize.py
│   ├── test_compose.py
│   ├── test_determinism.py
│   └── test_wav.py
└── .github/workflows/
    └── render.yml    — hourly render + force-push to render branch
```

## Design principles

- **Deterministic**: Same state → same bytes. No random melodies.
- **Data-driven**: Every audible property derives from real federation data.
- **Dependency-light**: Standard library only (`wave`, `math`, `struct`, `hashlib`, `json`, `urllib`).
- **Robust**: Degrades gracefully with partial data. Never destroys a valid render.
- **Audibly melodic**: Pentatonic constraints prevent dissonance. Real music, not noise.

## Federation identity

`agent-music` is an **observer node** in the [Agent Internet](https://github.com/kimeisele/agent-internet) federation. It discovers peers via `agent-federation-node` topic search and seed-based descriptor URLs, reads NADI outbox envelopes, and mirrors federation-map's normalization behavior.

- `.well-known/agent-federation.json` — federation descriptor
- `.well-known/agent.json` — A2A agent card
- `docs/authority/charter.md` — observer charter
- `docs/authority/capabilities.json` — capability manifest

## Version

**0.1.0** — Initial production observer.
