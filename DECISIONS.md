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

