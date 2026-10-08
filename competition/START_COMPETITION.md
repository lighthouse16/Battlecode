# Official Competition Runbook

Concise operator runbook for official Autumn competition launch.
Follow phases strictly in order. Do not skip phases. Do not invent unverified rules or behaviors.
Leverages Battlelab's existing authoritative official subsystem (`battlelab.official`).

---

## WORKFLOW OVERVIEW

```text
official package arrives
→ PHASE A: INGEST (canonical source bundle created)
→ PHASE B: VERIFY (initialize & validate 23-rule configs/game_spec.yaml)
→ resolve phase-blocking UNKNOWN fields (continue non-blocking reversible tasks)
→ PHASE C: OFFICIAL ADAPTER (probe SDK & run official contract checks)
→ PHASE D: CHAMPION V0 (minimal legal deterministic full-match bot)
→ verify submission package & run smoke match
→ PHASE E: SUBMISSION (strict operator approval)
→ PHASE F: RESEARCH (hypothesis-driven loop)
```

---

## OPERATOR CONTROL RULES

Automation proceeds without interruption for normal, deterministic, reversible tasks.

### Continuation Rule
Do NOT stop the entire workflow because of an unrelated UNKNOWN field. Continue independent reversible tasks (e.g. starter bot compiling, baseline strategy framing) when possible.

### STOP Conditions (Mandatory Operator Intervention)
Stop immediately and request operator review ONLY when:
1. An official rule is ambiguous or contradictory, and directly blocks the current phase.
2. Required official material or SDK executable is missing, halting progress.
3. Official SDK runtime behavior contradicts official documentation.
4. A destructive repository or architecture change appears necessary.
5. Official submission action would occur. (Never submit automatically.)
6. Promotion would require a manual risk override (`I_ACKNOWLEDGE_STATISTICAL_RISK`).

---

## PHASE A — INGEST

Capture, isolate, and inventory authoritative official competition materials into a content-addressed source bundle.

### Command
```pwsh
python scripts/bootstrap_competition.py <path-to-official-materials>
# OR directly via existing CLI:
python -m battlelab official ingest <path-to-official-materials> --copy --json
```

### Checklist
- [ ] Official rules documents captured.
- [ ] Official SDK / engine distribution captured.
- [ ] Official starter / reference bot captured.
- [ ] Canonical `source_bundle_hash` generated and verified.
- [ ] Exact files staged into `data/official/source_bundles/<bundle_hash>/files/`.

### Automation Prompt
`competition/prompts/01_ingest_official_materials.md`

---

## PHASE B — VERIFY

Initialize and populate the canonical typed 23-rule game specification.

### Commands
```pwsh
# 1. Initialize canonical 23-rule spec from ingested bundle
python -m battlelab official spec init --source-bundle <BUNDLE_HASH> --output configs/game_spec.yaml

# 2. Populate rules in configs/game_spec.yaml (referencing docs/game_spec.template.yaml)
# Every rule requires meaning, source_refs (<relpath>#<fragment>), and verification_state.

# 3. Validate structural and citation integrity
python -m battlelab official spec validate configs/game_spec.yaml
```

### Operational Checklist (Human Tracking)
Mark each item as `VERIFIED`, `UNKNOWN`, or `CONFLICTING` in operator notes:
- [ ] Game version and season.
- [ ] Primary game objective and win/draw conditions.
- [ ] Scoring rules and tiebreakers.
- [ ] Turn structure and lifecycle.
- [ ] Observations and bot visibility scope.
- [ ] Legal action space and per-turn action limits.
- [ ] Map model (coordinates, dimensions, obstacles, symmetry).
- [ ] Entities, units, and resources.
- [ ] Termination conditions.
- [ ] Timing limits (per-turn ms, cumulative bank ms).
- [ ] Memory limit (MB) and CPU restrictions.
- [ ] Language and runtime restrictions.
- [ ] Randomness and seed control.
- [ ] Replay format and parser availability.
- [ ] Official build command.
- [ ] Official local match command.
- [ ] Official submission packaging format.

---

## PHASE C — OFFICIAL ADAPTER

Bind and verify the official execution subsystem using Battlelab's existing official adapter infrastructure. Preserve fail-closed semantics.

### Commands
```pwsh
# 1. Inspect official readiness
python -m battlelab official status
python -m battlelab official readiness

# 2. Probe official SDK executable
python -m battlelab official sdk probe

# 3. Run adapter contract tests
python -m pytest tests/contract/test_official_contract.py
```

### Checklist
- [ ] Official engine installation discovered and probed.
- [ ] Executable identity and SDK version verified.
- [ ] Map discovery verified from engine, not hardcoded.
- [ ] Starter bot build verified through official toolchain.
- [ ] Starter bot local match executed successfully.
- [ ] Replay parser verified against generated replay file.
- [ ] Battlelab normalized `MatchResult` verified against official engine stdout.
- [ ] Official adapter contract tests green.

### Automation Prompt
`competition/prompts/02_configure_official_adapter.md`

---

## PHASE D — CHAMPION V0

Build the simplest robust legal bot. Objective is zero failures, not high Elo.

### Minimum Quality Criteria
- Minimum legal strategy (valid moves only, no illegal actions).
- Fully deterministic where rules permit.
- Completes full game from turn 0 to termination.
- Zero crashes (`crashed == False`).
- Zero protocol violations (`protocol_violation == False`).
- Zero timeouts (`timed_out == False`) across representative maps.
- Verified runtime headroom (turn duration significantly below timeout).
- Clean immutable artifact created and registered in Battlelab:
  ```pwsh
  python -m battlelab bot register bots/autumn2026/champion_v0/ --name "Champion_v0" --tags "policy:baseline,version:v0"
  python -m battlelab champion init <artifact_id> --reason "Initial baseline Champion v0" --actor "<operator_name>"
  # Note: During pre-competition development without a verified adapter, append --allow-unverified-adapter
  ```
- Workflow state marked `CHAMPION_V0_PROMOTED`.

### Automation Prompt
`competition/prompts/03_build_champion_v0.md`

---

## PHASE E — SUBMISSION

Submission is strictly operator-controlled. Never submit automatically.

### Pre-Submission Checklist
- [ ] Exact candidate artifact ID identified.
- [ ] Clean working tree (`git status --porcelain` is empty).
- [ ] Git commit SHA recorded.
- [ ] Official build command succeeds cleanly.
- [ ] Submission package structure validated against official documentation.
- [ ] Smoke match completed with candidate package.
- [ ] Operator review and explicit written authorization given.

---

## PHASE F — RESEARCH

Continuous strategy improvement loop. Starts only after Champion v0 is active.

```text
hypothesis
→ challenger
→ paired evaluation
→ promotion gates
→ champion
→ selective ladder reality check
→ replay analysis
→ next hypothesis
```

### Automation Prompts
- Strategy Research: `competition/prompts/04_strategy_research.md`
- Ladder Replay Analysis: `competition/prompts/05_ladder_replay_analysis.md`
