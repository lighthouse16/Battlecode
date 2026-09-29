# Official Adapter Mapping Guide & Template

This document provides the blueprint for implementing `OfficialEngineBridge` once official SDK specifications are published.

---

## 1. Engine Executable & Invocation Syntax

Identify how the official competition engine is invoked from the command line:

```text
Official CLI Pattern:
  <executable> [global-flags] <subcommand> [flags]

Example placeholders (TO BE REPLACED WITH AUTHORITATIVE SYNTAX):
  engine run --map <map> --p1 <bot_a_path> --p2 <bot_b_path> --seed <seed> --output <replay_dir>
```

In `OfficialEngineBridge.build_match_command`:
```python
def build_match_command(
    self,
    spec: MatchSpec,
    bot_a: BotArtifact,
    bot_b: BotArtifact,
    work_dir: Path,
) -> list[str]:
    # Construct exact argv list without shell=True
    return [
        self.sdk_executable_path,
        "run",
        "--map", spec.map_name,
        "--seed", str(spec.seed),
        "--bot-a", str(bot_a.source_location),
        "--bot-b", str(bot_b.source_location),
        "--output", str(work_dir),
    ]
```

---

## 2. Match Result Parsing

Identify how the engine reports match outcomes:
- Exit code semantics (0 = clean, >0 = crash/error).
- Standard output format (JSON, regex line matching, or dedicated summary file).

In `OfficialEngineBridge.parse_match_result`:
```python
def parse_match_result(
    self,
    spec: MatchSpec,
    command_result: CommandResult,
    work_dir: Path,
) -> MatchResult:
    # Parse stdout or output JSON file
    ...
```

---

## 3. Replay Discovery & Parsing

Identify:
- Where the replay is written.
- Replay file format (JSON, JSON lines, protobuf, binary).

In `OfficialEngineBridge.parse_replay`:
```python
def parse_replay(self, replay_path: Path) -> NormalizedReplay:
    # Read raw bytes and hash them
    # Parse frames and populate NormalizedReplay model
    ...
```

---

## 4. Bot Artifact Packaging

Identify:
- Required bot directory structure.
- Main entrypoint name (e.g. `main.py`, `RobotPlayer.java`, etc.).
- Compilation / packaging requirements.

In `OfficialEngineBridge.build_or_prepare_artifact`:
```python
def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
    # Package bot into runnable artifact
    ...
```
