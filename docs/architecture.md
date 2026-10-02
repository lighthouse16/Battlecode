# Battlelab System Architecture

## 1. Overview
Battlelab is a competition-agnostic research, experimentation, and tournament platform designed for automated AI coding agent development in Battlecode-style competitions.

The core design principle is **strict isolation of game rules behind pluggable adapters**. Until the official Autumn competition rules and SDK are released, all platform capabilities are verified using a deterministic `MockAdapter`.

```
+-------------------------------------------------------------------+
|                        Command Line (CLI)                         |
|      battlelab [doctor|bot|match|tournament|experiment|replay]    |
+-------------------------------------------------------------------+
                                  |
    +-----------------------------+-----------------------------+
    |                             |                             |
    v                             v                             v
+------------------+    +-------------------+    +--------------------+
|  Experiments &   |    | Tournament Matrix |    |   Bot Registry &   |
| Promotion Gates  |    |    & Scheduler    |    | Immutable Artifacts|
+------------------+    +-------------------+    +--------------------+
         |                        |                         |
         +------------------------+-------------------------+
                                  |
                                  v
                    +---------------------------+
                    |        GameAdapter        |
                    |      (Abstract Base)      |
                    +---------------------------+
                                  |
                +-----------------+-----------------+
                |                                   |
                v                                   v
    +----------------------+            +-----------------------+
    |     MockAdapter      |            |  OfficialPlaceholder  |
    | (Deterministic Grid) |            |  (Rules & SDK TBD)    |
    +----------------------+            +-----------------------+
                |
                +-------------------+--------------------+
                |                                        |
                v                                        v
+-------------------------------+        +-------------------------------+
|         SQLite Storage        |        |    Content-Addressed Store    |
| (Artifacts, Matches, Metrics) |        |     (Replays, Reports)        |
+-------------------------------+        +-------------------------------+
```

## 2. Core Subsystems

### 2.1 Bot Artifact Immutability
Every evaluated bot is converted into an immutable artifact:
- Source code is hashed (SHA-256) across all files.
- Current Git commit SHA and dirty-worktree status are captured.
- Source files are frozen into `data/artifacts/<artifact_id>/`.
- Experiments and tournament matches reference only immutable artifact IDs.

### 2.2 Paired Evaluation & Resumable Tournaments
To eliminate seed and side bias:
- Every challenger evaluation is paired against a baseline.
- Both bots play on identical maps, identical deterministic seeds, and both starting sides (`side_0` and `side_1`).
- The tournament scheduler records each match spec in SQLite. If execution is interrupted, `battlelab tournament resume <id>` skips already completed matches.

### 2.3 Failure Classification Taxonomy
Failures are categorized into:
- `BOT_CRASH`: Bot code raised exception or segfaulted.
- `TIMEOUT`: Execution exceeded turn or match time limit.
- `PROTOCOL_VIOLATION`: Wire framing, out-of-turn communication, or line limit exceeded (distinct from invalid move).
- `INVALID_ACTION`: Action syntax invalid or movement blocked by rules.
- `BUILD_FAILURE`: Bot syntax or build error.
- `MISSING_DEPENDENCY`: Required module not found in runtime.
- `REPLAY_CORRUPTION`: Replay file missing or unparseable.
- `GAMEPLAY_LOSS`: Normal strategic defeat.
- `INFRASTRUCTURE_FAILURE`: System or worker interruption.

### 2.4 Controlled Champion Promotion
Promotion to champion status is protected by automated gates (`configs/promotion.yaml`):
1. Experiment must be complete with minimum sample size.
2. Zero unexplained crashes or protocol violations.
3. Win rate exceeds configured threshold with positive bootstrap 95% CI lower bound.
4. No critical opponent, map, side, or seed regresses beyond tolerance.
5. Determinism verification succeeds on identical seeds.
6. Atomically updates `bots/champion/champion_manifest.json` with full audit history in SQLite.

### 2.5 Process Isolation & Stream Containment
- **Mock Python Code Boundary**: `MOCK_PYTHON_CODE_BOUNDARY = IMMUTABLE_ARTIFACT + PYTHON_STDLIB`. Python launches in isolated mode (`-I -S -B -u -c <bootstrap>`) ignoring ambient site-packages, editable `.pth` files, `PYTHONPATH`, and user customization scripts. Bootstrap reconfigures stdout/stderr to UTF-8 and sets `sys.path = [artifact_root, *safe_stdlib_paths]` with `-I -S`.
- **Environment Allowlisting**: Subprocesses execute with clean, allowlisted environments (`SAFE_ENV_ALLOWLIST`), stripping coordinator secrets and environment variables.
- **Stream Bounds**: 64 KB per line and 100 lines per turn on stdout; bounded chunk reading (500 lines / 64 KB max buffer) on stderr with non-allocating drain. Unsolicited pre-turn bytes, multiple responses, and internal request ID mismatches are flagged as protocol violations.
- **Process Containment**: On Windows, subprocesses are assigned to a Windows Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Spawning with `CREATE_SUSPENDED` eliminates process creation races; any failure during Job Object creation, assignment, or resumption fails closed immediately. Worker death child containment is `ENFORCED_AND_TESTED` on Windows. On POSIX, child containment is `PLATFORM_DEPENDENT` (process group cleanup, no kernel parent-death signal).
- **Threat Model**: Laboratory trusted-code research environment, not a hostile multi-tenant sandbox. `FILESYSTEM_ISOLATION` is `BEST_EFFORT` via SHA-256 pre/post tamper detection (subject to inherent runtime TOCTOU limitations). `NETWORK_ISOLATION` is `NOT_IMPLEMENTED`.
