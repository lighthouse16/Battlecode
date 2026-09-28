"""MockAdapter implementation for local deterministic execution and testing."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from battlelab.adapters.base import GameAdapter
from battlelab.adapters.mock.bots import (
    policy_aggressive,
    policy_crash,
    policy_defensive,
    policy_fixed,
    policy_invalid_action,
    policy_nondeterministic,
    policy_random,
    policy_resource_greedy,
    policy_timeout,
)
from battlelab.adapters.mock.engine import MockEngine
from battlelab.adapters.mock.maps import MOCK_MAPS
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


POLICY_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "random": policy_random,
    "fixed": policy_fixed,
    "aggressive": policy_aggressive,
    "defensive": policy_defensive,
    "resource": policy_resource_greedy,
    "crash": policy_crash,
    "timeout": policy_timeout,
    "invalid_action": policy_invalid_action,
    "nondeterministic": policy_nondeterministic,
}


class MockAdapter(GameAdapter):
    """Deterministic mock adapter for testing all infrastructure workflows."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def version(self) -> str:
        return "0.1.0"

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
            game_version="mock-v1",
        )

    def discover_maps(self) -> list[str]:
        return sorted(list(MOCK_MAPS.keys()))

    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        if bot_artifact.language != "python":
            return False, f"Mock adapter only supports python, got '{bot_artifact.language}'"
        return True, "Compatible"

    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        if source_path.is_file():
            shutil.copy2(source_path, output_dir / source_path.name)
        elif source_path.is_dir():
            shutil.copytree(source_path, output_dir, dirs_exist_ok=True)
        return {"status": "SUCCESS", "message": "Artifact staged cleanly"}

    def _resolve_bot_policy(self, bot: BotArtifact) -> Callable[[dict[str, Any]], dict[str, Any]]:
        # Check tags for failure injection or baseline policy
        for tag in bot.tags:
            if tag.startswith("policy:"):
                pname = tag.split(":", 1)[1]
                if pname in POLICY_REGISTRY:
                    return POLICY_REGISTRY[pname]
            elif tag == "fail:crash":
                return policy_crash
            elif tag == "fail:timeout":
                return policy_timeout
            elif tag == "fail:invalid_action":
                return policy_invalid_action
            elif tag == "fail:nondeterministic":
                return policy_nondeterministic

        # Check display name
        name_lower = bot.display_name.lower()
        for k, func in POLICY_REGISTRY.items():
            if k in name_lower:
                return func

        # Default fallback is fixed deterministic policy
        return policy_fixed

    def run_local_match(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
    ) -> MatchResult:
        work_dir.mkdir(parents=True, exist_ok=True)

        if spec.map_name not in MOCK_MAPS:
            fc = FailureClassification(
                category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                evidence=f"Map '{spec.map_name}' not found",
            )
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=fc,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        game_map = MOCK_MAPS[spec.map_name]
        engine = MockEngine(game_map=game_map, seed=spec.seed)

        policy_a = self._resolve_bot_policy(bot_a)
        policy_b = self._resolve_bot_policy(bot_b)

        # Side assignment: side_0 is player 0, side_1 is player 1
        side_a = spec.side_assignment.get("A", "side_0")
        if side_a == "side_0":
            bot_0_policy, bot_1_policy = policy_a, policy_b
            bot_0_is_a = True
        else:
            bot_0_policy, bot_1_policy = policy_b, policy_a
            bot_0_is_a = False

        # Run engine
        engine_res = engine.run_match(
            bot_func_0=bot_0_policy,
            bot_func_1=bot_1_policy,
            time_limit_ms=spec.time_limit_ms,
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
                evidence="Bot A raised an unhandled exception during turn execution",
            )
        elif crashed_b:
            fc = FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_b",
                evidence="Bot B raised an unhandled exception during turn execution",
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
        elif invalid_a:
            fc = FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_a",
                evidence="Bot A submitted an illegal or unparseable action",
            )
        elif invalid_b:
            fc = FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_b",
                evidence="Bot B submitted an illegal or unparseable action",
            )
        elif outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B):
            fc = FailureClassification(
                category=FailureCategory.GAMEPLAY_LOSS,
                culprit="bot_b" if winner == "A" else "bot_a",
                evidence=f"Legitimate score defeat ({score_a:.1f} vs {score_b:.1f})",
            )

        # Write replay file
        replay_filename = f"replay_{spec.match_id}.jsonl"
        replay_path = work_dir / replay_filename
        
        # Controlled failure check for missing/corrupted replay testing
        has_fail_missing = "fail:missing_replay" in bot_a.tags or "fail:missing_replay" in bot_b.tags
        has_fail_corrupt = "fail:corrupted_replay" in bot_a.tags or "fail:corrupted_replay" in bot_b.tags

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
            invalid_action_a=invalid_a,
            invalid_action_b=invalid_b,
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
