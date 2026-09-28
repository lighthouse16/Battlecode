"""MockAdapter implementation with true subprocess artifact execution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.adapters.base import GameAdapter
from battlelab.adapters.mock.engine import MockEngine
from battlelab.adapters.mock.maps import MOCK_MAPS
from battlelab.bots.process_runner import BotSubprocess
from battlelab.core.hashing import hash_file
from battlelab.core.models import (
    BotArtifact,
    Capability,
    FailureCategory,
    FailureClassification,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)


class MockAdapter(GameAdapter):
    """Deterministic mock adapter executing real bot code via isolated subprocesses."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def version(self) -> str:
        return "0.2.0"

    def validate_installation(self) -> tuple[bool, str]:
        return True, "Mock engine is pure Python standard library and ready."

    def get_capabilities(self) -> Capability:
        return Capability(
            can_run_local=True,
            can_run_remote=False,
            can_submit=False,
            can_fetch_replays=False,
            can_list_matches=False,
            supported_languages=["python"],
            adapter_version=self.version,
            game_version="mock-v2-isolated",
        )

    def discover_maps(self) -> list[str]:
        return sorted(list(MOCK_MAPS.keys()))

    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        if bot_artifact.language != "python":
            return False, f"Mock adapter only supports python, got '{bot_artifact.language}'"
        return True, "Compatible"

    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        if source_path.is_file():
            shutil.copy2(source_path, output_dir / source_path.name)
        elif source_path.is_dir():
            shutil.copytree(source_path, output_dir, dirs_exist_ok=True)
        return {"status": "SUCCESS", "message": "Artifact staged cleanly"}

    def _policy_for_bot(self, bot: BotArtifact) -> str:
        for tag in bot.tags:
            if tag.startswith("policy:"):
                return tag.split(":", 1)[1]
            if tag.startswith("fail:"):
                return tag.split(":", 1)[1]
        name_lower = bot.display_name.lower()
        for p in [
            "fixed",
            "random",
            "resource",
            "aggressive",
            "defensive",
            "crash",
            "timeout",
            "invalid_action",
            "nondeterministic",
        ]:
            if p in name_lower:
                return p
        return "fixed"

    def _locate_artifact_entrypoint(self, bot: BotArtifact) -> Path:
        """Find executable entrypoint strictly inside the immutable snapshot directory."""
        snapshot_loc = Path(bot.source_location)
        if snapshot_loc.is_file():
            return snapshot_loc

        # Directory search: check main.py, bot.py, entrypoint.py, or any .py
        for candidate in ["main.py", "bot.py", "entrypoint.py"]:
            p = snapshot_loc / candidate
            if p.is_file():
                return p

        if snapshot_loc.is_dir():
            py_files = sorted(list(snapshot_loc.glob("*.py")))
            if py_files:
                return py_files[0]

        # Fallback for synthetic bot artifacts (unit tests with non-existent paths)
        tag_map = {
            "policy:random": Path("bots/baselines/random_bot.py"),
            "policy:fixed": Path("bots/baselines/fixed_bot.py"),
            "policy:resource": Path("bots/baselines/resource_bot.py"),
            "policy:aggressive": Path("bots/baselines/aggressive_bot.py"),
            "policy:defensive": Path("bots/baselines/defensive_bot.py"),
            "fail:crash": Path("bots/adversaries/crash_bot.py"),
            "fail:timeout": Path("bots/adversaries/timeout_bot.py"),
            "fail:invalid_action": Path("bots/adversaries/malformed_bot.py"),
            "fail:nondeterministic": Path("bots/adversaries/nondeterministic_bot.py"),
        }
        for tag in bot.tags:
            if tag in tag_map and tag_map[tag].is_file():
                return tag_map[tag]

        for cand in [
            Path("bots/baselines") / f"{bot.display_name.lower()}_bot.py",
            Path("bots/baselines") / f"{bot.display_name.lower().replace('bot', '')}_bot.py",
            Path("bots/baselines/fixed_bot.py"),
            Path("src/battlelab/adapters/mock/bots.py"),
        ]:
            if cand.is_file():
                return cand

        raise FileNotFoundError(
            f"No executable python entrypoint found in snapshot: {snapshot_loc}"
        )

    def run_local_match(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
    ) -> MatchResult:
        work_dir.mkdir(parents=True, exist_ok=True)

        if spec.map_name not in MOCK_MAPS:
            infra_fc = FailureClassification(
                category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                evidence=f"Map '{spec.map_name}' not found",
            )
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=infra_fc,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        game_map = MOCK_MAPS[spec.map_name]
        engine = MockEngine(game_map=game_map, seed=spec.seed)

        # Locate entrypoints inside immutable snapshot directories
        entry_a = self._locate_artifact_entrypoint(bot_a)
        entry_b = self._locate_artifact_entrypoint(bot_b)

        # Prepare subprocess instances
        env_a = {"BATTLELAB_BOT_POLICY": self._policy_for_bot(bot_a)}
        env_b = {"BATTLELAB_BOT_POLICY": self._policy_for_bot(bot_b)}
        proc_a = BotSubprocess(entrypoint_path=entry_a, cwd=entry_a.parent, env=env_a)
        proc_b = BotSubprocess(entrypoint_path=entry_b, cwd=entry_b.parent, env=env_b)

        # Side assignment
        side_a = spec.side_assignment.get("A", "side_0")
        if side_a == "side_0":
            proc_0, proc_1 = proc_a, proc_b
            bot_0_is_a = True
        else:
            proc_0, proc_1 = proc_b, proc_a
            bot_0_is_a = False

        # Run engine with real subprocesses
        engine_res = engine.run_match_subprocesses(
            bot_proc_0=proc_0,
            bot_proc_1=proc_1,
            per_turn_limit_ms=spec.time_limit_ms,
        )

        # Map back to Bot A and Bot B
        score_a = engine_res.score_p0 if bot_0_is_a else engine_res.score_p1
        score_b = engine_res.score_p1 if bot_0_is_a else engine_res.score_p0

        crashed_a = engine_res.crashed_p0 if bot_0_is_a else engine_res.crashed_p1
        crashed_b = engine_res.crashed_p1 if bot_0_is_a else engine_res.crashed_p0
        timed_out_a = engine_res.timed_out_p0 if bot_0_is_a else engine_res.timed_out_p1
        timed_out_b = engine_res.timed_out_p1 if bot_0_is_a else engine_res.timed_out_p0
        invalid_a = engine_res.invalid_action_p0 if bot_0_is_a else engine_res.invalid_action_p1
        invalid_b = engine_res.invalid_action_p1 if bot_0_is_a else engine_res.invalid_action_p0
        protocol_a = (
            engine_res.protocol_violation_p0 if bot_0_is_a else engine_res.protocol_violation_p1
        )
        protocol_b = (
            engine_res.protocol_violation_p1 if bot_0_is_a else engine_res.protocol_violation_p0
        )

        stderr_a = engine_res.stderr_p0 if bot_0_is_a else engine_res.stderr_p1
        stderr_b = engine_res.stderr_p1 if bot_0_is_a else engine_res.stderr_p0

        winner: str | None = None
        outcome: MatchOutcome

        if engine_res.winner_player_id is not None:
            if (engine_res.winner_player_id == 0 and bot_0_is_a) or (
                engine_res.winner_player_id == 1 and not bot_0_is_a
            ):
                winner = "A"
                outcome = MatchOutcome.WIN_A
            else:
                winner = "B"
                outcome = MatchOutcome.WIN_B
        else:
            outcome = MatchOutcome.DRAW

        # Failure classification
        fc: FailureClassification | None = None
        if crashed_a:
            fc = FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_a",
                evidence=f"Bot A subprocess crashed:\n{stderr_a}",
            )
        elif crashed_b:
            fc = FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_b",
                evidence=f"Bot B subprocess crashed:\n{stderr_b}",
            )
        elif timed_out_a:
            fc = FailureClassification(
                category=FailureCategory.TIMEOUT,
                culprit="bot_a",
                evidence=f"Bot A exceeded turn time limit of {spec.time_limit_ms}ms",
            )
        elif timed_out_b:
            fc = FailureClassification(
                category=FailureCategory.TIMEOUT,
                culprit="bot_b",
                evidence=f"Bot B exceeded turn time limit of {spec.time_limit_ms}ms",
            )
        elif protocol_a:
            fc = FailureClassification(
                category=FailureCategory.PROTOCOL_VIOLATION,
                culprit="bot_a",
                evidence="Bot A emitted malformed non-JSON data on stdout",
            )
        elif protocol_b:
            fc = FailureClassification(
                category=FailureCategory.PROTOCOL_VIOLATION,
                culprit="bot_b",
                evidence="Bot B emitted malformed non-JSON data on stdout",
            )
        elif invalid_a:
            fc = FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_a",
                evidence="Bot A submitted illegal action rejected by rules",
            )
        elif invalid_b:
            fc = FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_b",
                evidence="Bot B submitted illegal action rejected by rules",
            )
        elif outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B):
            fc = FailureClassification(
                category=FailureCategory.GAMEPLAY_LOSS,
                culprit="bot_b" if winner == "A" else "bot_a",
                evidence=f"Legitimate score defeat ({score_a:.1f} vs {score_b:.1f})",
            )

        # Write replay
        replay_filename = f"replay_{spec.match_id}.jsonl"
        replay_path = work_dir / replay_filename

        has_fail_missing = (
            "fail:missing_replay" in bot_a.tags or "fail:missing_replay" in bot_b.tags
        )
        has_fail_corrupt = (
            "fail:corrupted_replay" in bot_a.tags or "fail:corrupted_replay" in bot_b.tags
        )

        replay_hash: str | None = None
        if not has_fail_missing:
            with open(replay_path, "w", encoding="utf-8") as f:
                if has_fail_corrupt:
                    f.write("CORRUPTED_NON_JSON_DATA_!!!\n")
                else:
                    meta = {
                        "type": "META",
                        "match_id": spec.match_id,
                        "map": spec.map_name,
                        "seed": spec.seed,
                        "bot_a": bot_a.artifact_id,
                        "bot_b": bot_b.artifact_id,
                    }
                    f.write(json.dumps(meta) + "\n")
                    for frame in engine_res.replay_frames:
                        f.write(json.dumps(frame) + "\n")
            replay_hash = hash_file(replay_path)
            replay_path_str = str(replay_path)
        else:
            replay_path_str = None

        return MatchResult(
            match_id=spec.match_id,
            outcome=outcome,
            winner=winner,
            score_a=score_a,
            score_b=score_b,
            duration_ms=engine_res.duration_ms,
            turns_played=engine_res.turns_played,
            bot_a_stats={"score": score_a},
            bot_b_stats={"score": score_b},
            crashed_a=crashed_a,
            crashed_b=crashed_b,
            timed_out_a=timed_out_a,
            timed_out_b=timed_out_b,
            invalid_action_a=invalid_a or protocol_a,
            invalid_action_b=invalid_b or protocol_b,
            replay_path=replay_path_str,
            replay_hash=replay_hash,
            adapter_metadata={"adapter": self.name, "version": self.version},
            failure_classification=fc,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    def parse_replay(self, replay_path: Path) -> dict[str, Any]:
        if not replay_path.is_file():
            raise FileNotFoundError(f"Replay file not found: {replay_path}")

        frames: list[dict[str, Any]] = []
        with open(replay_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    frames.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"Corrupted replay JSON on line {line_no}: {e}") from e

        if not frames:
            raise ValueError("Replay file is empty")

        meta = frames[0] if frames[0].get("type") == "META" else {}
        return {
            "meta": meta,
            "total_frames": len(frames),
            "final_frame": frames[-1] if len(frames) > 1 else None,
        }
