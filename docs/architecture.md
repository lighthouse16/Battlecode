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
- `INVALID_ACTION`: Action syntax invalid or movement blocked by rules.
- `BUILD_FAILURE`: Bot syntax or build error.
- `MISSING_DEPENDENCY`: Required module not found in runtime.
- `REPLAY_CORRUPTION`: Replay file missing or unparseable.
- `GAMEPLAY_LOSS`: Normal strategic defeat.
- `INFRASTRUCTURE_FAILURE`: System or worker interruption.

### 2.4 Controlled Champion Promotion
Promotion to champion status is protected by automated gates (`configs/promotion.yaml`):
1. Experiment must be complete with minimum sample size.
2. Zero unexplained crashes or invalid actions.
3. Win rate exceeds configured threshold with statistical confidence.
4. No critical map regresses beyond tolerance.
5. Determinism verification succeeds on identical seeds.
6. Atomically updates `bots/champion/champion_manifest.json` with full audit history in SQLite.
