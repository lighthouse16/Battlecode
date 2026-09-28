# Battlelab: Battlecode R&D Platform

A deterministic, competition-agnostic research, experimentation, and tournament platform built for autonomous AI coding agents competing in Battlecode-style programming competitions.

---

## 1. What the Platform Does
- **Deterministic Simulation**: Fast, repeatable turn-based mock matches with full replay generation and runtime accounting.
- **Immutable Bot Artifacts**: Automatic hashing of source code, git commit tracking, dirty-tree checks, and frozen artifact snapshots.
- **Paired Tournament Matrices**: Resumable parallel tournament execution with seed pairing and starting-side swapping.
- **Statistical Evaluation**: Wilson score intervals for win rates, paired bootstrap differences, segment breakdowns (maps, sides, opponents).
- **Automated Failure Classification**: Categorizes crashes, timeouts, invalid actions, and build failures with supporting evidence.
- **Protected Champion Promotion**: Statistical gates preventing regressions, crashes, or premature promotions.
- **AI Improvement Loop**: Generates concise human-readable `report.md` and structured machine-readable `analysis_packet.json`.

## 2. What the Platform Deliberately Does Not Know Yet
The official Autumn competition rules and SDK have **not** been released yet. Therefore:
- The platform contains **zero** assumptions about real game units, economy, combat math, bytecode limits, or official API commands.
- Mock mechanics are synthetic tests for infrastructure only.
- The official adapter (`OfficialPlaceholderAdapter`) is a strict placeholder that reports missing SDK capabilities rather than fabricating endpoints.

---

## 3. Installation

Battlelab requires Python 3.11+.

```bash
# Clone repository
git clone <repo-url>
cd Battlecode

# Install package in editable mode
python -m pip install -e .

# Or install with dev dependencies
python -m pip install -e ".[dev]"
```

---

## 4. Usage (Windows, Linux, macOS)

All commands can be invoked via `battlelab` or `python -m battlelab`.

### 4.1 System Doctor
Inspect platform environment, directory structure, and adapter readiness:
```bash
battlelab doctor
```

### 4.2 Validate Configurations
```bash
battlelab config validate
```

### 4.3 Register Bot Artifacts
Freeze a bot script into an immutable artifact:
```bash
# Register a baseline bot
battlelab bot register bots/baselines/random_bot.py --name BaselineRandom --tags "policy:random,baseline"

# Register a challenger bot
battlelab bot register bots/challengers/challenger_v1.py --name ChallengerV1 --tags "policy:resource"

# List registered artifacts
battlelab bot list
```

### 4.4 Run a Single Mock Match
```bash
battlelab match run --adapter mock --bot-a <ARTIFACT_A> --bot-b <ARTIFACT_B> --map grid_classic_8x8 --seed 42
```

### 4.5 Run a Mock Tournament
Run a parallel paired tournament from configuration:
```bash
battlelab tournament run --config configs/evaluation.yaml --bot-a <ARTIFACT_A> --bot-b <ARTIFACT_B> --workers 4
```

### 4.6 Resume an Interrupted Tournament
If a tournament was stopped or interrupted, resume it seamlessly without re-running completed matches:
```bash
battlelab tournament resume <TOURNAMENT_ID>
```

### 4.7 Create, Run, and Report an Experiment
Follow the hypothesis-driven experiment protocol:
```bash
# 1. Create experiment
battlelab experiment create \
  --hypothesis "Greedy resource collection outscores random movement" \
  --baseline <BASELINE_ARTIFACT_ID> \
  --challenger <CHALLENGER_ARTIFACT_ID> \
  --change "Swapped random walk for greedy tile evaluation"

# 2. Run experiment matrix
battlelab experiment run <EXPERIMENT_ID> --workers 4

# 3. View Markdown Report
battlelab experiment report <EXPERIMENT_ID>

# 4. View JSON Analysis Packet (for AI agent)
cat data/reports/<EXPERIMENT_ID>/analysis_packet.json
```

### 4.8 Champion Promotion and Rollback
Promote an experiment's challenger only if it meets all configured gates:
```bash
# Dry run verification
battlelab experiment promote <EXPERIMENT_ID> --dry-run

# Actual promotion
battlelab experiment promote <EXPERIMENT_ID>
```

To roll back to a historical champion artifact:
```python
from battlelab.experiments.promotion import PromotionGate
PromotionGate().rollback("<HISTORICAL_ARTIFACT_ID>", reason="Safety rollback")
```

### 4.9 Inspect and Verify Replays
```bash
battlelab replay inspect <REPLAY_HASH>
battlelab replay verify <REPLAY_HASH>
```

---

## 5. Storage Layout
- Metadata Database: `data/battlelab.db` (SQLite with WAL mode).
- Replays: `data/replays/<sha256>.jsonl` (content-addressed, verified).
- Experiment Reports: `data/reports/<experiment_id>/` (`report.md` and `analysis_packet.json`).
- Immutable Artifact Snapshots: `data/artifacts/<artifact_id>/`.
- Active Champion Manifest: `bots/champion/champion_manifest.json`.

---

## 6. Official Competition Integration
When the official competition documentation and SDK arrive:
1. Review `docs/day_zero_rule_ingestion.md` and `docs/official_integration_checklist.md`.
2. Ingest documentation:
   ```bash
   battlelab official integrate path/to/official_docs
   ```
3. Complete `configs/game_spec.yaml` with verified rules.
4. Implement `OfficialAdapter` in `src/battlelab/adapters/official/adapter.py`.
5. Connect minimal legal bot and run adapter contract tests:
   ```bash
   pytest tests/contract/
   ```
