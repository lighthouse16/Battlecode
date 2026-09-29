# Official Golden Fixture Workflow

## Purpose
Establishes an immutable record of official competition engine execution to catch engine drift, rule changes, or adapter regressions when official updates are released.

---

## Workflow Steps

### 1. Capture Official SDK Version & Metadata
Record the exact installed SDK version and build metadata:
```bash
python -m battlelab official sdk probe --json > data/official/sdk_probe.json
```

### 2. Capture Official Map Inventory
Discover available maps directly from the official engine:
```bash
python -c "from battlelab.adapters import get_adapter; print(get_adapter('official').discover_maps())"
```

### 3. Execute Baseline Match
Execute a reference match between official starter bots on a canonical map with a fixed seed (e.g., `seed=42`):
```bash
python -m battlelab match \
  --adapter official \
  --bot-a art_official_minimal \
  --bot-b art_official_minimal \
  --map default_map \
  --seed 42 \
  --time-limit 10000
```

### 4. Store Raw Artifacts
Archive the raw outputs into `tests/fixtures/official_golden/`:
- `stdout.log`: Raw engine stdout.
- `stderr.log`: Raw engine stderr.
- `exit_code.txt`: Exact process exit code.
- `replay.raw`: Bit-exact replay file from the engine.
- `spec.json`: Full MatchSpec used for the run.

### 5. Generate Golden Manifest
Compute SHA256 hashes of all inputs and outputs into `manifest.json`:
```json
{
  "golden_fixture_version": "1.0.0",
  "sdk_version": "2026.x.x",
  "seed": 42,
  "map": "default_map",
  "files": {
    "stdout.log": "sha256...",
    "stderr.log": "sha256...",
    "replay.raw": "sha256...",
    "spec.json": "sha256..."
  },
  "expected_normalized_outcome": {
    "outcome": "WIN_A",
    "scores": {"A": 100.0, "B": 45.0},
    "turn_count": 25
  }
}
```

### 6. Parity Verification
Parse the golden replay and match output through `OfficialEngineBridge`:
- Confirm `bridge.parse_match_result` produces expected `MatchResult`.
- Confirm `bridge.parse_replay` extracts normalized gameplay fields.
- Test in CI that re-running the bridge against golden inputs yields bit-exact outputs.

### 7. Version Drift Detection
If the competition organizers release an SDK update:
1. Re-run `battlelab official sdk probe`.
2. Compare detected SDK version with `manifest.json`.
3. If version differs, mark existing golden fixture as superseded and regenerate following this workflow.
