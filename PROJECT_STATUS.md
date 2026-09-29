# Battlelab Project Status

## Status Summary
- **Phase**: Phase 2.1.1 Complete (Execution Isolation, Statistical Gates, Configuration Provenance, Active Cancellation, and Cross-Platform CI Hardening)
- **Current Milestone**: Full local R&D platform operational with real subprocess bot execution, per-turn deadline enforcement, atomic SQLite job leases with lease fencing tokens, multi-scheduler safety, paired opponent-pool evaluation with weighted bootstrap confidence intervals, positive lower CI gates, runtime headroom gates, hardened promotion gates with explicit human override audit trails, collision-resistant promotion and match IDs, complete artifact integrity checks with forged-manifest rejection, full configuration provenance tracking, and CI quality gates.
- **Engine Status**: 
  - `MockAdapter`: Real subprocess execution over line-delimited JSON stdin/stdout protocol strictly from frozen, cryptographically verified artifact snapshots (`data/artifacts/<id>/`).
  - `OfficialPlaceholderAdapter`: Strict placeholder awaiting Autumn rulebook and SDK documentation.

## Completed Features
- [x] Initial repository structure and workspace bootstrap.
- [x] Git repository initialization and `.gitignore` setup.
- [x] Base project configuration (`pyproject.toml`, `.env.example`, `ruff`, `mypy`).
- [x] Core domain models (`BotArtifact`, `MatchSpec`, `MatchResult`, `Experiment`, `Capability`, `FailureClassification`, `ResolvedOpponent`).
- [x] Stable canonical hashing (SHA-256) and deterministic identifier generation (with `tournament_id`/`experiment_id` disambiguation).
- [x] Domain error hierarchy (`BattlelabError`, `InfrastructureError`, `CapabilityNotSupportedError`, `PromotionGateError`).
- [x] SQLite database schema V4 with WAL mode, atomic job leasing with unique `lease_token` fencing, heartbeat renewal, configuration provenance columns, and migration framework.
- [x] Content-addressed replay store (`data/replays/<sha256>.jsonl`) with checksum verification and corruption detection.
- [x] Pluggable `GameAdapter` interface contract.
- [x] Real Subprocess Bot Execution: JSON observation on stdin, JSON action on stdout; code executed strictly from immutable artifact snapshot.
- [x] Hard Per-Turn Deadlines: Sub-second return on 10ms limit, cleanly terminating timed-out bot processes.
- [x] Process-Level Isolation: Bots executed in isolated child processes; unhandled bot crashes never crash the engine harness.
- [x] Clean Process Tree Termination: Recursive termination of process hierarchies using `psutil`, escalating from SIGTERM to SIGKILL / TerminateProcess, ensuring no zombie or orphan child processes remain across Linux and Windows.
- [x] Linux typing fix for `CREATE_NEW_PROCESS_GROUP` via guarded `getattr`.
- [x] Linux container PID namespace portability for process tree termination assertions.
- [x] Memory Limit Diagnosis: `battlelab doctor` accurately reports whether portable memory limits are supported on the host OS.
- [x] Atomic SQLite Job-Leasing Scheduler: CAS-based leasing (`PENDING`, `RUNNING`, `COMPLETED`, `RETRYABLE_FAILURE`, `TERMINAL_FAILURE`) with lease fencing tokens; authentic stale worker results are rejected.
- [x] Active Cancellation on Lease Loss: Heartbeat failure triggers match cancellation event, terminating bot subprocesses and aborting immediately without committing stale results or writing replays.
- [x] Expired Lease Recovery: Automatically recovers expired leases from crashed or stalled workers.
- [x] Artifact Integrity and Forgery Defense: Manifest hash recomputed canonically from manifest contents; compares against stored DB hash; rejects forged manifests, modified files, altered entrypoints, path traversals (`..`), and symlinks escaping source dir.
- [x] Pre-Promotion Integrity Verification: Enforces artifact integrity on both challenger and baseline before promotion or determinism checks.
- [x] Configuration Provenance Tracking: `Experiment` stores `evaluation_config`, `evaluation_config_hash`, `opponent_pool_config`, `opponent_pool_config_hash`, and `promotion_config_hash`, displayed in reports and JSON feedback packets.
- [x] Per-Turn Challenger Runtime Headroom: Headroom computed strictly from challenger per-turn bot measurements (`bot_a_stats`/`bot_b_stats`), not whole-match durations.
- [x] Segment Diagnostics: Complete breakdown including `by_map`, `by_opponent`, `by_side`, and `by_seed`.
- [x] Minimum Segment Sample Size Enforcement: Blocks promotion if any opponent group, map, or side has fewer pairs than `min_segment_sample_size`.
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
- [x] Complete automated test suite (56 unit, integration, and contract tests including all 26 Phase 2.1.1 regression tests).

## Currently Verified Behaviors
- `battlelab doctor` accurately diagnoses environment, data directories, active champion, adapter readiness, and OS memory limit capabilities.
- `battlelab config validate` parses and validates all YAML configs in `configs/`.
- `battlelab champion status` and `battlelab champion rollback` manage champion manifest state and write full DB audit logs.
- Hard 10ms timeout verified: returns in < 0.8s on 10ms limit without leaving orphan processes alive.
- Process tree termination verified: terminates nested child processes spawned by target bots across container PID namespaces.
- Multi-scheduler concurrency verified: two concurrent schedulers leasing from the same DB complete all matches exactly once without duplicate runs.
- Expired lease recovery verified: crashed worker leases are safely recovered and re-executed.
- Authentic fencing token verified: stale worker completing after lease expiration is rejected by lease token check.
- Active cancellation on lease loss verified: process tree terminated and match aborted when lease renewal fails.
- Multi-seed determinism verified: passes on deterministic bots, reliably catches nondeterministic bots.
- Manifest forgery defense verified: altered files, altered entrypoints, fake manifest hashes, path traversals, and symlinks rejected.
- Per-turn headroom verified: fast multi-turn matches pass; single slow turns fail headroom gate.
- Promotion gates verified: blocks failing bots, accepts statistical improvements, requires positive lower CI, verifies runtime headroom, enforces opponent weights, checks pre-promotion integrity, and audits rollbacks.
- End-to-end demonstration script passes all 12 steps cleanly twice in clean temporary directories without repository mutation.

## Remaining Work
- Official Autumn competition rules ingestion (pending official release by organizers).
- Implementation of `OfficialAdapter` following `docs/day_zero_rule_ingestion.md` when SDK is released.

## Known Limitations
- Official SDK/rules not released; official adapter is non-functional placeholder by design.
- Mock engine rules are synthetic tests of infrastructure, not strategic proxies for competition mechanics.
- Windows OS stdlib does not support portable POSIX `resource.setrlimit`; truthfully reported as unsupported by `battlelab doctor`.

## Exact Commands Last Run Successfully
- `python -m ruff format --check src tests scripts` (60 files already formatted)
- `python -m ruff check src tests scripts` (All checks passed!)
- `python -m mypy src tests` (Success: no issues found in 59 source files)
- `python -m pytest` (56 passed in 150.54s)
- `python scripts/demonstrate_e2e.py` (Run 1: all 12 steps completed successfully!)
- `python scripts/demonstrate_e2e.py` (Run 2: all 12 steps completed successfully!)
- `python -m battlelab doctor` (Exit code 0)
- `python -m battlelab config validate` (Exit code 0)
- `python -m battlelab tournament run --workers 2` (Exit code 0)
- `git status` (Clean repository status, zero uncommitted data/ files)

