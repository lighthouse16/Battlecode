# Battlelab Project Status

## Status Summary
- **Phase**: Phase 3.1 Complete (Qualification-First Solo Competition Operations)
- **Current Milestone**: The pre-rule platform now includes a frictionless, fail-closed operating layer for stage tracking, evidence checkpoints, GitHub review, and release freezing.
  - **Infrastructure Status**: Complete and verified.
    1. Typed immutable command plans (`OfficialCommandPlan`) with operation discrimination (`PROBE`, `DISCOVER_MAPS`, `BUILD`, `RUN_MATCH`), exact launcher + SDK prefix enforcement, and binary mutation detection before and after execution.
    2. Real isolated rule-test execution (`DefaultRuleTestRunner`) enforcing exact pytest collection matching, execution exit code 0, 0 failed/errored/skipped/xfailed/deselected tests, clean worktree checks, and cryptographic JUnit XML evidence binding (`RuleTestEvidence`).
    3. Evidence-backed map discovery and build manifest verification requiring real command execution and cryptographic bindings (`source_bot_hash`, `sdk_executable_sha256`, `build_command_hash`).
    4. Truthful memory enforcement reporting (`EnforcementStatus`), honestly diagnosing host platform capabilities.
    5. Spec-driven runtime definitions (`supported_languages`, `minimal_bot_language`, independent `game_version`) eliminating hardcoded Python or version derivations.
    6. Qualification-first competition plan with day-precision schedule honesty, one-next-action CLI, atomic hash-chained checkpoints, config-drift detection, and manual submission boundary.
    7. Release freeze bound to the active immutable champion, optional promoted experiment, official provenance, exact reviewed Git commit, synchronized upstream, and GitHub Actions evidence.
    8. 176 automated tests passing across the repository, including 24 Phase 3.1 competition-operations regression cases.
  - **Official Integration Status**: BLOCKED pending official competition release.
    - No official rules, official SDK, production bridge, legal bot, parity fixtures, or submission capability exist yet.
    - `can_submit=false` remains strictly locked.
    - Production adapter defaults to `UnconfiguredOfficialBridge` and remains fail-closed by design.
- **Engine Status**: 
  - `MockAdapter`: Real subprocess execution over line-delimited JSON stdin/stdout protocol strictly from frozen, cryptographically verified artifact snapshots (`data/artifacts/<id>/`). Produces normalized replay contracts.
  - `OfficialAdapter`: Generic orchestration adapter for official competition games, decoupled from rules via `OfficialEngineBridge` and `OfficialCommandRunner`. Defaults to `UnconfiguredOfficialBridge` (fail-closed, non-operational until Day 0).
  - `OfficialPlaceholderAdapter`: Preserved for backward compatibility.


## Completed Features
- [x] Initial repository structure and workspace bootstrap.
- [x] Git repository initialization and `.gitignore` setup.
- [x] Base project configuration (`pyproject.toml`, `.env.example`, `ruff`, `mypy`).
- [x] Core domain models (`BotArtifact`, `MatchSpec`, `MatchResult`, `Experiment`, `Capability`, `FailureClassification`, `ResolvedOpponent`).
- [x] Stable canonical hashing (SHA-256) and deterministic identifier generation (with `tournament_id`/`experiment_id` disambiguation).
- [x] Domain error hierarchy (`BattlelabError`, `InfrastructureError`, `CapabilityNotSupportedError`, `PromotionGateError`).
- [x] SQLite database schema V4 with WAL mode, atomic job leasing with unique `lease_token` fencing, heartbeat renewal, configuration provenance columns (`promotion_config_json`), and migration framework.
- [x] Content-addressed replay store (`data/replays/<sha256>.jsonl`) with checksum verification and corruption detection.
- [x] Pluggable `GameAdapter` interface contract.
- [x] Real Subprocess Bot Execution: JSON observation on stdin, JSON action on stdout; code executed strictly from immutable artifact snapshot with `-B` and `PYTHONDONTWRITEBYTECODE=1` disabling bytecode compilation.
- [x] Hard Per-Turn Deadlines: Sub-second return on 10ms limit, cleanly terminating timed-out bot processes.
- [x] Process-Level Isolation: Bots executed in isolated child processes; unhandled bot crashes never crash the engine harness.
- [x] Clean Process Tree Termination: Recursive termination of process hierarchies using `psutil`, escalating from SIGTERM to SIGKILL / TerminateProcess, ensuring no zombie or orphan child processes remain across Linux and Windows.
- [x] Linux typing fix for `CREATE_NEW_PROCESS_GROUP` via guarded `getattr`.
- [x] Portable Process Liveness and Concurrency Isolation: Termination and liveness checks verify observable behavior without relying solely on `psutil.pid_exists()`; concurrent unrelated processes remain untouched.
- [x] Memory Limit Diagnosis: `battlelab doctor` accurately reports whether portable memory limits are supported on the host OS.
- [x] Atomic SQLite Job-Leasing Scheduler: CAS-based leasing (`PENDING`, `RUNNING`, `COMPLETED`, `RETRYABLE_FAILURE`, `TERMINAL_FAILURE`) with lease fencing tokens; authentic stale worker results are rejected.
- [x] Active Cancellation on Lease Loss: Heartbeat failure triggers match cancellation event, terminating bot subprocesses and aborting immediately without committing stale results or writing replays.
- [x] Expired Lease Recovery: Automatically recovers expired leases from crashed or stalled workers.
- [x] Artifact Integrity and Forgery Defense: Manifest hash recomputed canonically from manifest contents; compares against stored DB hash; rejects forged manifests, modified files, altered entrypoints, path traversals (`..`), directory symlinks, file symlinks, hidden entries (`.*`), `__pycache__`, and extraneous unmanifested files.
- [x] Pre-Promotion Integrity Verification: Enforces artifact integrity on both challenger and baseline before promotion or determinism checks.
- [x] Configuration Provenance Tracking: `Experiment` stores `evaluation_config`, `evaluation_config_hash`, `opponent_pool_config`, `opponent_pool_config_hash`, `promotion_config`, and `promotion_config_hash`, displayed in reports and JSON feedback packets. `PromotionGate.promote()` evaluates using the recorded snapshot.
- [x] Strict Challenger Per-Turn Runtime Headroom: Headroom computed strictly from challenger per-turn bot measurements without whole-match fallbacks; missing telemetry fails closed with explicit violations.
- [x] Complete Segment Evidence: Enforces `min_segment_sample_size` across all expected seeds, maps, opponent groups, and sides; missing segments default to 0; `segment_sample_counts` returned in gate results.
- [x] Opponent Pool Paired Evaluation: Experiments executed against `configs/opponent_pool.yaml` with stable `pair_id` joins and symmetric side-swapping.
- [x] Statistically Valid Evaluation: 95% Wilson intervals for win rates, weighted paired bootstrap difference distributions, segment breakdowns excluding infrastructure failures.
- [x] Statistical Promotion Gates: Requiring positive 95% lower CI bound, non-negative mean win delta, runtime headroom >= 10%, and segmented regression thresholds.
- [x] Multi-Seed Determinism Verification: Normalized frame verification excluding volatile timing fields; passes for deterministic bots and reliably catches nondeterministic bots.
- [x] Hardened Promotion Gates: Removed unprincipled `--force`; requires explicit human actor identity, justification reason (>= 15 chars), and exact risk acknowledgement (`--acknowledge-risk I_ACKNOWLEDGE_STATISTICAL_RISK`), permanently logged as `MANUAL_OVERRIDE`.
- [x] Collision-Resistant Promotion & Match IDs: Standardized on `prom_<timestamp>_<short-uuid>` and distinct match hashes per tournament/experiment.
- [x] Champion Rollback CLI: `battlelab champion rollback` atomically restores historical champion and documents audit record without foreign key violations.
- [x] CLI `--entrypoint` Option: Added `--entrypoint` argument to `battlelab bot register` with validation.
- [x] Isolated & Repeatable E2E Demonstration (`scripts/demonstrate_e2e.py`): Fully isolated via `tempfile.TemporaryDirectory()`, tests pause (`INTERRUPTED 2/8`) and resume (`COMPLETED 8/8`), with zero repository mutation.
- [x] GitHub Actions CI Matrix: Complete matrix workflow (`.github/workflows/ci.yml`) on Ubuntu/Windows for Python 3.11 and 3.12 running ruff format, ruff check, mypy, pytest, demonstrate_e2e twice, CLI tournament, and working tree cleanliness check.
- [x] Complete automated test suite: 151 unit, integration, and contract tests passing.
- [x] Phase 3.0.1 Fail-Closed Hardening: Atomic bundle staging/publishing, subprocess env/timeout sanitization, secret redaction across all exceptions, 23-rule specification binding.
- [x] Phase 3.0.2 Verifiable Evidence: Concrete SDK evidence model, strict citation grammar (`manifest-relpath[#fragment]`), AST-verified node IDs, whole-match vs per-turn timeout semantics.
- [x] Phase 3.0.3 Verifiable Execution: Typed `OfficialCommandPlan`, prefix matching, binary mutation detection, real isolated pytest runner with collection match and JUnit XML evidence, honest resource reporting, spec-driven runtimes, and 20 canonical regression tests.
- [x] Phase 3.1 Qualification-First Operations: Typed competition schedule, honest unconfirmed deadline times, `competition status/next/complete`, atomic checkpoint hash chain, config-drift lockout, fail-closed release freeze/verification, GitHub CI evidence binding, and manual submission boundary.

## Currently Verified Behaviors
- `battlelab doctor` accurately diagnoses environment, data directories, active champion, adapter readiness, and OS memory limit capabilities.
- `battlelab config validate` parses and validates all YAML configs in `configs/`.
- `battlelab champion status` and `battlelab champion rollback` manage champion manifest state and write full DB audit logs.
- Hard 10ms timeout verified: returns in < 0.8s on 10ms limit without leaving orphan processes alive.
- Process tree termination verified: terminates nested child processes spawned by target bots across container PID namespaces without global child reaping, preserving exit code 7 of unrelated children and leaving concurrent running processes alive.
- Multi-scheduler concurrency verified: two concurrent schedulers leasing from the same DB complete all matches exactly once without duplicate runs.
- Expired lease recovery verified: crashed worker leases are safely recovered and re-executed.
- Authentic fencing token verified: stale worker completing after lease expiration is rejected by lease token check.
- Active cancellation on lease loss verified: process tree terminated and match aborted when lease renewal fails.
- Multi-seed determinism verified: passes on deterministic bots, reliably catches nondeterministic bots.
- Manifest forgery defense verified: altered files, altered entrypoints, fake manifest hashes, path traversals, directory symlinks, file symlinks, root symlinks, prohibited hidden/cache/bytecode files, and extraneous files rejected.
- Pure per-turn headroom verified: fast multi-turn matches pass; single slow turns fail headroom gate; missing telemetry fails closed; exact telemetry count invariant enforced.
- Segment evidence enforced: minimum segment sample size threshold of 1 enforced across seeds, maps, opponent groups, and sides; threshold 0 disables check; non-negative integer type validation.
- Promotion gates verified: blocks failing bots, accepts statistical improvements, requires positive lower CI, verifies runtime headroom, enforces opponent weights, checks pre-promotion integrity, audits rollbacks, and uses recorded experiment configuration snapshots.
- End-to-end demonstration script passes all 12 steps cleanly twice in clean temporary directories without repository mutation.
- Official pre-rule readiness and fail-closed hardening verified: source ingestion (`battlelab official ingest`), typed 23-rule specification validation (`battlelab official spec`), secure command execution (`OfficialCommandRunner`), decoupled bridge architecture (`OfficialEngineBridge`), normalized replay model (`NormalizedReplay`), and fail-closed readiness assessment (`battlelab official readiness`).
- All 20 Phase 3.0.3 canonical regression tests passing.
- All 24 Phase 3.1 competition-operations regression cases passing, including audit tampering, reviewed config reconciliation, false readiness, live GitHub CI evidence binding, invalid CI evidence, and release-manifest tampering probes.

## Remaining Work (Day-0 External Blockers)
- Ingestion of authoritative official rulebook and SDK (upon official competition release).
- Implementation of rule-specific `OfficialEngineBridge` methods in `src/battlelab/official/bridge.py`.
- Population of 23 rule sections in `configs/game_spec.yaml` with authoritative citations and passing rule tests.
- Creation of `OfficialMinimalBot` under `bots/baselines/official_minimal/`.
- Execution of `docs/official_activation_runbook.md`.

## Known Limitations
- Official SDK/rules not released; official adapter is non-operational and fail-closed by design (`can_run_local: false`, `can_submit: false`).
- Synthetic test SDK exists strictly under `tests/fixtures/synthetic_sdk/` for testing generic orchestration, timeouts, and contracts; never used in production.
- Windows OS stdlib does not support portable POSIX `resource.setrlimit`; truthfully reported as unsupported by `battlelab doctor`.

## Exact Commands Last Run Successfully
- `python -m ruff format --check src tests scripts` (77 files already formatted)
- `python -m ruff check src tests scripts` (All checks passed!)
- `python -m mypy src tests` (Success: no issues found in 76 source files)
- `python -m pytest tests/` (176 passed in 39.83s on Linux/Python 3.12)
- `python scripts/demonstrate_e2e.py` (Run 1: all 12 steps completed successfully!)
- `python scripts/demonstrate_e2e.py` (Run 2: all 12 steps completed successfully!)
- `python -m battlelab doctor` (Exit code 0, fail-closed reported)
- `python -m battlelab config validate` (Exit code 0)
- `python -m battlelab competition status` (Exit code 0; prelaunch, 0/4, exact next action shown)
- `python -m battlelab competition status --deep --json` (Exit code 0; Git synchronized, official readiness fail-closed with 14 blockers)
- `python -m battlelab official readiness --json` (Exit code 0, ready=False, 14 blockers)
- `python -m battlelab official status --check` (Exit code 1 as expected for unready official adapter)
- `python -m battlelab official sdk probe /definitely/missing --json` (Exit code 1 with clean JSON error)
- `python -m battlelab official activate --dry-run --json` (Exit code 0, status=BLOCKED)
- `git status --porcelain` (Clean repository status)
