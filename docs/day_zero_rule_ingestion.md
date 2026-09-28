# Day-Zero Rule Ingestion Workflow

## Purpose
When the official Autumn Battlecode competition documentation and SDK become available, follow this exact protocol to ingest official rules without guessing or carrying over assumptions from prior seasons.

## Workflow Sequence

### Stage 1: Document Discovery & Receipt
1. Place official rulebook PDF, markdown, or website dump into `docs/official_specs/`.
2. Record download timestamp, document version, and checksum.
3. Run `battlelab official integrate docs/official_specs/` to instantiate `configs/game_spec.yaml`.

### Stage 2: Extract & Verify Canonical Rule Items
Fill every field in `configs/game_spec.yaml` with:
- `meaning`: Exact rule summary.
- `source_ref`: Section or page number in official documentation.
- `verification_state`: `UNVERIFIED`, `DOCUMENTED`, or `TEST_VERIFIED`.
- `implementation_impact`: Adapter, bot interface, or metrics effect.
- `test_coverage`: Name of test verifying the rule.

**Items to Extract:**
- Victory, loss, draw, and tie-break conditions.
- Turn structure (synchronous, sequential, phase-based).
- Observation model (fog of war, full visibility, sensors).
- Legal action space and execution order.
- Units, structures, entity stats, and costs.
- Resources, economy, and production formulas.
- Movement, pathfinding rules, and collision resolution.
- Combat, damage calculation, range, and cooldowns.
- Map format, coordinate system, and symmetry rules.
- Randomness sources and seeds.
- Communication mechanisms (radio, shared memory, beacons).
- Compute and memory limits (bytecode limits, per-turn time limits).
- Supported programming languages and runtimes.
- Official engine CLI commands for running local matches.
- Official submission CLI commands or endpoints.
- Replay file formats and download mechanisms.

### Stage 3: Adapter Implementation
1. Create `src/battlelab/adapters/official/adapter.py` subclassing `GameAdapter`.
2. Connect official local engine CLI to `run_local_match()`.
3. Implement `parse_replay()` for the official replay format.
4. Implement minimal legal bot (`bots/baselines/official_minimal.py`).
5. Run adapter contract tests: `pytest tests/contract/test_official_adapter.py`.
6. Confirm local-to-remote parity before enabling ladder submissions.
