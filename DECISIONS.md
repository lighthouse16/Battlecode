# Architectural Decision Records (ADRs)

## ADR-001: Separation of R&D Platform from Game Engine
- **Context**: The official competition rules and SDK for Autumn have not been released. Early strategic optimization without rules creates technical debt.
- **Decision**: Keep all game-specific mechanics behind the `GameAdapter` interface. Implement a deterministic `MockAdapter` purely for infrastructure testing and verification.
- **Alternatives Considered**: Attempting to reverse-engineer past Battlecode season specs.
- **Why Chosen**: Past seasons often undergo breaking changes in turn order, networking, bytecode limits, or API paradigms.

## ADR-002: Storage Layer Architecture (SQLite + Content-Addressed Blobs)
- **Context**: The platform generates structured metadata (matches, experiments, bots) and large unstructured data (replays, stdout/stderr logs).
- **Decision**: Use SQLite with WAL mode (`PRAGMA journal_mode=WAL`) and `PRAGMA busy_timeout=5000` for transactional metadata. Store replays and raw outputs on the filesystem under content-addressed paths (`data/replays/<sha256>.jsonl`).
- **Alternatives Considered**: Storing full replays inside SQLite blobs or using an external document database (e.g. MongoDB).
- **Why Chosen**: Keeps SQLite database compact, eliminates lock contention on large blobs, supports atomic file writes, and ensures single-file inspectability.

## ADR-003: Deterministic Bot Artifacts & Immutability
- **Context**: AI coding agents may modify bot files during or between experiments, causing reproducibility bugs.
- **Decision**: Every bot registered is copied into an immutable artifact snapshot under `data/artifacts/<artifact_id>/` with its source content hash, git commit hash, and dirty indicator recorded. Experiments reference immutable artifact IDs only.
- **Alternatives Considered**: Referencing source directories directly.
- **Why Chosen**: Guarantees that past experiments can be accurately reproduced regardless of worktree changes.

## ADR-004: Paired Matchup Evaluation & Statistical Rigor
- **Context**: Aggregate win-rate alone can be misleading due to asymmetric map advantages, starting side bias, or seed variance.
- **Decision**: The evaluation scheduler enforces paired matchups (every challenger vs baseline match is paired with identical map, seed, and both side permutations). Statistics compute Wilson score confidence intervals and paired difference bootstrap distributions.
- **Alternatives Considered**: Raw un-paired round-robin win percentage.
- **Why Chosen**: Reduces variance caused by difficult seeds or asymmetric maps, ensuring fair challenger promotion.

## ADR-005: Strict Placeholder for Official Adapter
- **Context**: Missing official specifications.
- **Decision**: Create an explicit placeholder adapter implementing `GameAdapter` that raises `CapabilityNotSupportedError` on any execution attempt, with comprehensive diagnostics in `doctor`.
- **Alternatives Considered**: Stubs that simulate success or mimic old Gradle tasks.
- **Why Chosen**: Prevents false sense of readiness and adheres to safety requirement against fabricating official APIs.

## ADR-006: Subprocess Isolation and Line Protocol Bot Communication
- **Context**: Executing untrusted or in-development bot code in the engine process risks unhandled crashes, memory leaks, and thread blocking.
- **Decision**: Execute bots in isolated OS subprocesses communicating over line-delimited JSON on stdin/stdout. Per-turn timeouts enforce strict deadlines (< 1s return on 10ms limit) using thread queues, and process trees are terminated recursively via `psutil`.
- **Alternatives Considered**: In-process dynamic importing (`importlib`), containerization (Docker).
- **Why Chosen**: Subprocesses provide process-level crash and memory isolation with minimal overhead and zero Docker setup prerequisites on local developer machines.

## ADR-007: Atomic Job Leasing via Compare-And-Swap (CAS) in SQLite
- **Context**: Multiple worker processes or concurrent schedulers running against a single SQLite database can collide and execute the same match redundantly if transactions are deferred.
- **Decision**: Implement compare-and-swap (CAS) row leasing in SQLite (`BEGIN` with `WHERE status IN ('PENDING', 'RETRYABLE_FAILURE') ... AND attempt_count < max_attempts`). If `cur.rowcount == 0`, retry immediately. Recover expired leases whose lease deadline has passed.
- **Alternatives Considered**: File locks, Redis/external broker, naive deferred queries.
- **Why Chosen**: Keeps the architecture dependency-free (pure SQLite WAL) while guaranteeing exactly-once match execution under high multi-scheduler concurrency.

## ADR-008: Hardened Champion Promotion and Audit Logging with Explicit Risk Acknowledgement
- **Context**: Blindly allowing `--force` to bypass statistical gates undermines the integrity of autonomous bot evaluation.
- **Decision**: Remove `--force` from promotion CLI. Require human identity (`--actor`), mandatory justification (`--override-reason`), and exact acknowledgement (`--acknowledge-risk I_ACKNOWLEDGE_STATISTICAL_RISK`). Write a permanent `MANUAL_OVERRIDE` record in the database. Provide atomic rollback CLI (`battlelab champion rollback`) that logs all rollbacks. Use collision-resistant IDs (`prom_<timestamp>_<short-uuid>`).
- **Alternatives Considered**: Keeping `--force` as a hidden flag; requiring manual database edits for overrides.
- **Why Chosen**: Enforces accountability and auditability for intentional departures from statistical evaluation gates.

## ADR-009: OS Memory Limit Reporting via System Doctor
- **Context**: Memory safeguards vary across operating systems. Python standard library supports `resource.setrlimit` on POSIX systems, but Windows Job Object memory management requires third-party win32 extensions.
- **Decision**: Accurately diagnose and report memory limit support via `battlelab doctor`. On POSIX, support `resource.setrlimit`; on Windows, cleanly report unsupported status without crashing or faking support.
- **Alternatives Considered**: Claiming universal cross-platform memory capping; forcing win32 dependencies.
- **Why Chosen**: Adheres strictly to the engineering requirement of truthfulness over illusion of readiness.

## ADR-010: Cryptographic Artifact Integrity and Fenced Worker Heartbeats
- **Context**: Artifact snapshots can suffer from disk tampering or extraneous injected files, and slow workers could commit results after their leases have expired.
- **Decision**: Compute cryptographic manifests (`manifest.json`) on artifact creation with SHA-256 hashes of every file and verify full tree integrity before running. Implement fencing tokens (`lease_token`) and heartbeat background threads during match execution so stale worker results are unconditionally rejected on lease expiration.
- **Alternatives Considered**: Relying solely on root directory hash; unconditional result writes without lease verification.
- **Why Chosen**: Guarantees complete immutability of registered artifacts and eliminates race conditions in distributed worker execution.

## ADR-011: Strict Citation Grammar and AST-Verified Rule Test Node IDs (Phase 3.0.2)
- **Context**: Rule documentation and citations could contain ambiguous paths, traversal sequences, or point to uncollected/skipped tests.
- **Decision**: Enforce strict `<manifest-relpath>[#<fragment>]` citation grammar without colons, backslashes, or path traversal. Verify test node IDs (`<file.py>::<func>`) through AST parsing, forbidding skipped and xfailed decorators.
- **Alternatives Considered**: Permissive string citations or relying on test filename matches.
- **Why Chosen**: Guarantees verifiable traceability from rule specifications to authoritative documents and executable tests.

## ADR-012: Typed Immutable Command Plans and Executable Mutation Checking (Phase 3.0.3)
- **Context**: Command execution against official SDK binaries could forge arguments, pass the SDK executable as an inert argument, or substitute binaries between probe and match stages.
- **Decision**: Introduce typed immutable `OfficialCommandPlan` (`PROBE`, `DISCOVER_MAPS`, `BUILD`, `RUN_MATCH`). Enforce canonical command prefix (`launcher_argv + [sdk_executable_path]`), forbid inert SDK path appearances elsewhere in argv, and re-hash binaries before and after execution to detect disk mutation (`InfrastructureTamperingError`).
- **Alternatives Considered**: Permissive arbitrary command execution or assuming standard subcommands like `probe`.
- **Why Chosen**: Prevents command identity spoofing and guarantees SDK executable provenance across all execution phases.

## ADR-013: Isolated Rule Test Execution and Cryptographic JUnit Evidence Binding (Phase 3.0.3)
- **Context**: Rule verification could be claimed via static presence or fake self-reported flags without actually running tests.
- **Decision**: Implement `DefaultRuleTestRunner` executing `pytest --collect-only` for exact 1:1 node ID collection matching, isolated execution with `--junitxml`, requiring exit code 0, 0 failures, 0 errors, 0 skips, 0 xfails, 0 deselected tests, and clean worktree checks. Record complete collection/execution hashes and metrics in `RuleTestEvidence`.
- **Alternatives Considered**: Trusting rule verification state flags in YAML.
- **Why Chosen**: Guarantees that rules can only become `TEST_VERIFIED` through genuine, repeatable execution in an untampered worktree.

## ADR-014: Truthful Resource Enforcement and Complete Fail-Closed Lockout (Phase 3.0.3)
- **Context**: Competition readiness checks must not falsely report platform capabilities or allow premature submission.
- **Decision**: Explicitly inspect and report memory enforcement status (`EnforcementStatus`), truthfully declaring Windows platform unisolated memory status as `UNENFORCED`. Lock `can_submit=false` and keep production defaults fail-closed with clear blockers until Day-0 rules and SDK are released.
- **Alternatives Considered**: Faking memory capping or leaving submission status unconstrained.
- **Why Chosen**: Eliminates false senses of security and adheres to the strict requirement of zero fabricated production readiness.

## ADR-015: Qualification-First Solo Operations and Manual Submission Boundary (Phase 3.1)
- **Context**: The execution, evidence, and promotion subsystems are hardened, but a solo operator would still need to remember many commands, dates, freeze rules, and review requirements under competition pressure.
- **Decision**: Add a thin, game-agnostic competition control plane driven by `configs/competition.yaml`. It exposes one next action at a time, records evidenced checkpoints in an atomic SHA-256 hash chain, and creates cryptographically bound release manifests only after artifact integrity, active-champion identity, clean and synchronized Git state, reviewed commit identity, GitHub Actions evidence, and official readiness all pass. Submission remains an explicit manual action.
- **Alternatives Considered**: A fully autonomous agent that edits, promotes, and submits; free-form Markdown checklists; game-specific strategy automation before rule release.
- **Why Chosen**: Reduces solo-operator friction and missed steps without allowing an AI agent to become its own reviewer or silently cross the external submission boundary.
