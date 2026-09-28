# Battlelab Project Status

## Status Summary
- **Phase**: Phase 2 Hardened (Execution Isolation, Statistically Valid Evaluation, Atomic Scheduler, CI Quality Gates)
- **Current Milestone**: Full local R&D platform operational with real subprocess bot execution, per-turn deadline enforcement, atomic SQLite job leases, multi-scheduler safety, paired opponent-pool evaluation with bootstrap confidence intervals, hardened promotion gates with explicit human override audit trails, collision-resistant promotion IDs, and CI quality gates.
- **Engine Status**: 
  - `MockAdapter`: Real subprocess execution over line-delimited JSON stdin/stdout protocol strictly from frozen artifact snapshots (`data/artifacts/<id>/`).
  - `OfficialPlaceholderAdapter`: Strict placeholder awaiting Autumn rulebook and SDK documentation.

## Completed Features
- [x] Initial repository structure and workspace bootstrap.
- [x] Git repository initialization and `.gitignore` setup.
- [x] Base project configuration (`pyproject.toml`, `.env.example`, `ruff`, `mypy`).
- [x] Core domain models (`BotArtifact`, `MatchSpec`, `MatchResult`, `Experiment`, `Capability`, `FailureClassification`).
- [x] Stable canonical hashing (SHA-256) and deterministic identifier generation.
- [x] Domain error hierarchy (`BattlelabError`, `InfrastructureError`, `CapabilityNotSupportedError`, `PromotionGateError`).
- [x] SQLite database schema V2 with WAL mode, atomic job leasing, worker attribution, and migration framework.
- [x] Content-addressed replay store (`data/replays/<sha256>.jsonl`) with checksum verification and corruption detection.
- [x] Pluggable `GameAdapter` interface contract.
- [x] Real Subprocess Bot Execution: JSON observation on stdin, JSON action on stdout; code executed strictly from immutable artifact snapshot.
- [x] Hard Per-Turn Deadlines: Sub-second return on 10ms limit, cleanly terminating timed-out bot processes.
- [x] Process-Level Isolation: Bots executed in isolated child processes; unhandled bot crashes never crash the engine harness.
- [x] Clean Process Tree Termination: Recursive termination of process hierarchies using `psutil` to prevent orphan worker processes.
- [x] Memory Limit Diagnosis: `battlelab doctor` accurately reports whether portable memory limits are supported on the host OS.
- [x] Atomic SQLite Job-Leasing Scheduler: CAS-based leasing (`PENDING`, `RUNNING`, `COMPLETED`, `RETRYABLE_FAILURE`, `TERMINAL_FAILURE`) with multi-scheduler concurrency safety verified.
- [x] Expired Lease Recovery: Automatically recovers expired leases from crashed or stalled workers.
- [x] Opponent Pool Paired Evaluation: Experiments executed against `configs/opponent_pool.yaml` with stable `pair_id` joins and symmetric side-swapping.
- [x] Statistically Valid Evaluation: 95% Wilson intervals for win rates, paired bootstrap difference distributions, segment breakdowns excluding infrastructure failures.
- [x] Multi-Seed Determinism Verification: Normalized frame verification excluding volatile timing fields; passes for deterministic bots and reliably catches nondeterministic bots.
- [x] Hardened Promotion Gates: Removed unprincipled `--force`; requires explicit human actor identity, justification reason, and exact risk acknowledgement (`--acknowledge-risk I_ACKNOWLEDGE_STATISTICAL_RISK`), permanently logged as `MANUAL_OVERRIDE`.
- [x] Collision-Resistant Promotion IDs: Standardized on `prom_<timestamp>_<short-uuid>`.
- [x] Champion Rollback CLI: `battlelab champion rollback` atomically restores historical champion and documents audit record.
- [x] GitHub Actions CI: Complete matrix workflow (`.github/workflows/ci.yml`) on Ubuntu/Windows running ruff, mypy, pytest, and smoke tests.
- [x] Full 12-step end-to-end demonstration script (`scripts/demonstrate_e2e.py`).
- [x] Complete automated test suite (30 unit, integration, and contract tests).

## Currently Verified Behaviors
- `battlelab doctor` accurately diagnoses environment, data directories, active champion, adapter readiness, and OS memory limit capabilities.
- `battlelab config validate` parses and validates all YAML configs in `configs/`.
- `battlelab champion status` and `battlelab champion rollback` manage champion manifest state and write full DB audit logs.
- Hard 10ms timeout verified: returns in < 0.8s on 10ms limit without leaving orphan processes alive.
- Process tree termination verified: terminates child processes spawned by target bots.
- Multi-scheduler concurrency verified: two concurrent schedulers leasing from the same DB complete all matches exactly once without duplicate runs.
- Expired lease recovery verified: crashed worker leases are safely recovered and re-executed.
- Multi-seed determinism verified: passes on deterministic bots, reliably catches nondeterministic bots.
- Promotion gates verified: blocks failing bots, accepts statistical improvements, allows audited manual overrides, and audits rollbacks.
- End-to-end demonstration script passes all 12 steps cleanly.

## Remaining Work
- Official Autumn competition rules ingestion (pending official release by organizers).
- Implementation of `OfficialAdapter` following `docs/day_zero_rule_ingestion.md` when SDK is released.

## Known Limitations
- Official SDK/rules not released; official adapter is non-functional placeholder by design.
- Mock engine rules are synthetic tests of infrastructure, not strategic proxies for competition mechanics.
- Windows OS stdlib does not support portable POSIX `resource.setrlimit`; truthfully reported as unsupported by `battlelab doctor`.

## Exact Commands Last Run Successfully
- `python -m ruff check src tests` (All checks passed!)
- `python -m ruff format --check src tests` (57 files already formatted)
- `python -m mypy src tests` (Success: no issues found in 57 source files)
- `python -m pytest` (30 passed in 94.39s)
- `python scripts/demonstrate_e2e.py` (All 12 steps completed successfully!)
- `python -m battlelab.cli doctor`
- `python -m battlelab.cli champion status`
- `python -m battlelab.cli champion rollback --help`

