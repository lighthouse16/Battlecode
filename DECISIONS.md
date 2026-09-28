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
