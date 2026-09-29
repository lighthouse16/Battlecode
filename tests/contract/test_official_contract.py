"""Contract tests for OfficialAdapter and OfficialEngineBridge using synthetic SDK."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from battlelab.adapters import get_adapter
from battlelab.core.errors import CapabilityNotSupportedError
from battlelab.core.models import (
    BotArtifact,
    FailureCategory,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)
from battlelab.official.adapter import OfficialAdapter
from battlelab.official.bridge import OfficialEngineBridge
from battlelab.official.command_runner import OfficialCommandRunner
from battlelab.official.models import CommandResult, NormalizedReplay
from battlelab.official.readiness import OfficialReadinessChecker

SYNTHETIC_SDK_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_sdk" / "synthetic_engine.py"
)


class FakeOfficialBridge(OfficialEngineBridge):
    """Injected test bridge driving the synthetic SDK executable.

    SYNTHETIC TEST FIXTURE — NOT AN OFFICIAL COMPETITION INTERFACE.
    """

    def __init__(self, sdk_path: Path = SYNTHETIC_SDK_PATH) -> None:
        self.sdk_path = sdk_path
        self.runner = OfficialCommandRunner()

    def validate_spec(self, spec_data: dict[str, Any]) -> tuple[bool, str]:
        return True, "Synthetic spec valid"

    def probe_sdk(self) -> dict[str, Any]:
        res = self.runner.run(
            [sys.executable, str(self.sdk_path), "probe"],
            cwd=self.sdk_path.parent,
        )
        if res.exit_code != 0:
            raise RuntimeError(f"Synthetic probe failed: {res.stderr}")
        return json.loads(res.stdout)

    def discover_maps(self) -> list[str]:
        res = self.runner.run(
            [sys.executable, str(self.sdk_path), "maps"],
            cwd=self.sdk_path.parent,
        )
        if res.exit_code != 0:
            return []
        return json.loads(res.stdout)

    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        if bot_artifact.language != "python":
            return False, f"Unsupported language: {bot_artifact.language}"
        return True, "Compatible"

    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        res = self.runner.run(
            [sys.executable, str(self.sdk_path), "build", str(source_path), str(output_dir)],
            cwd=output_dir,
        )
        if res.exit_code != 0:
            raise RuntimeError(f"Build failed: {res.stderr}")
        return json.loads(res.stdout)

    def build_match_command(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
    ) -> list[str]:
        cmd = [
            sys.executable,
            str(self.sdk_path),
            "run-match",
            "--map",
            spec.map_name,
            "--seed",
            str(spec.seed),
            "--bot-a",
            bot_a.artifact_id,
            "--bot-b",
            bot_b.artifact_id,
            "--output",
            str(work_dir),
        ]
        # Inspect bot tags for simulation flags
        all_tags = set(bot_a.tags + bot_b.tags)
        if "fail:crash" in all_tags:
            cmd.append("--fail-crash")
        if "fail:timeout" in all_tags:
            cmd.extend(["--timeout-sim", "5.0"])
        if "fail:malformed_result" in all_tags:
            cmd.append("--malformed-result")
        if "fail:malformed_replay" in all_tags:
            cmd.append("--malformed-replay")
        return cmd

    def parse_match_result(
        self,
        spec: MatchSpec,
        command_result: CommandResult,
        work_dir: Path,
    ) -> MatchResult:
        data = json.loads(command_result.stdout)
        winner = data.get("winner")
        outcome = self.normalize_outcome(data.get("outcome"))
        return MatchResult(
            match_id=spec.match_id,
            outcome=outcome,
            winner=winner,
            score_a=float(data.get("score_a", 0.0)),
            score_b=float(data.get("score_b", 0.0)),
            turns_played=int(data.get("turns_played", 0)),
            duration_ms=command_result.duration_ms,
            replay_path=data.get("replay_file"),
            replay_hash=None,
        )

    def locate_replay(self, spec: MatchSpec, work_dir: Path) -> Path | None:
        p = work_dir / "synthetic_replay.json"
        return p if p.is_file() else None

    def parse_replay(self, replay_path: Path) -> NormalizedReplay:
        with open(replay_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return NormalizedReplay(
            schema_version=data.get("schema_version", "1.0.0"),
            adapter_name=data.get("adapter_name", "official_synthetic"),
            adapter_version=data.get("adapter_version", "1.0.0"),
            game_version=data.get("game_version", "synth"),
            map_id=data.get("map_id", ""),
            seed=data.get("seed", 0),
            participants=data.get("participants", {}),
            outcome=data.get("outcome", ""),
            scores=data.get("scores", {}),
            turn_count=data.get("turn_count", 0),
            events=data.get("events", []),
            raw_replay_hash=data.get("raw_replay_hash", ""),
            source_metadata=data.get("source_metadata", {}),
        )

    def normalize_outcome(self, raw_outcome: Any) -> MatchOutcome:
        if raw_outcome == "WIN_A":
            return MatchOutcome.WIN_A
        elif raw_outcome == "WIN_B":
            return MatchOutcome.WIN_B
        elif raw_outcome == "DRAW":
            return MatchOutcome.DRAW
        return MatchOutcome.INFRASTRUCTURE_FAILURE


# ---------------------------------------------------------
# Test Cases
# ---------------------------------------------------------
def test_default_official_adapter_fails_closed():
    adapter = get_adapter("official")
    ok, msg = adapter.validate_installation()
    assert ok is False
    assert "not ingested" in msg

    caps = adapter.get_capabilities()
    assert caps.can_run_local is False
    assert caps.can_submit is False

    dummy_bot = BotArtifact(
        artifact_id="art_x",
        display_name="X",
        source_location="bots/x",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_x",
    )
    bot_compat_ok, _ = adapter.validate_bot_compatibility(dummy_bot)
    assert bot_compat_ok is False

    with pytest.raises(CapabilityNotSupportedError):
        adapter.build_or_prepare_artifact(Path("bots/x"), Path("out"))


def test_fake_official_bridge_sdk_probe_and_maps():
    bridge = FakeOfficialBridge()
    probe = bridge.probe_sdk()
    assert probe["sdk_name"] == "SyntheticTestEngine"
    assert probe["sdk_version"] == "1.0.0-synthetic"
    assert probe["can_run_local"] is True

    maps = bridge.discover_maps()
    assert len(maps) == 3
    assert "synth_grid_8x8" in maps


def test_fake_official_adapter_local_match_deterministic(tmp_path: Path):
    bridge = FakeOfficialBridge()
    runner = OfficialCommandRunner()
    adapter = OfficialAdapter(bridge=bridge, command_runner=runner)

    bot_a = BotArtifact(
        artifact_id="synth_bot_a",
        display_name="SynthBotA",
        source_location="bots/a",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_a",
    )
    bot_b = BotArtifact(
        artifact_id="synth_bot_b",
        display_name="SynthBotB",
        source_location="bots/b",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_b",
    )

    spec = MatchSpec(
        match_id="synth_match_1",
        adapter_name="official",
        adapter_version="1.0.0",
        bot_a_id="synth_bot_a",
        bot_b_id="synth_bot_b",
        map_name="synth_grid_8x8",
        seed=1337,
        time_limit_ms=5000,
    )

    res1 = adapter.run_local_match(spec, bot_a, bot_b, tmp_path / "run1")
    assert res1.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B)
    assert res1.replay_path is not None
    assert res1.replay_hash is not None

    res2 = adapter.run_local_match(spec, bot_a, bot_b, tmp_path / "run2")
    assert res1.winner == res2.winner
    assert res1.score_a == res2.score_a
    assert res1.score_b == res2.score_b
    assert res1.turns_played == res2.turns_played
    assert res1.replay_hash == res2.replay_hash


def test_fake_official_adapter_nonzero_exit(tmp_path: Path):
    bridge = FakeOfficialBridge()
    runner = OfficialCommandRunner()
    adapter = OfficialAdapter(bridge=bridge, command_runner=runner)

    bot_a = BotArtifact(
        artifact_id="bot_a",
        display_name="BotA",
        source_location="bots/a",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_a",
        tags=["fail:crash"],
    )
    bot_b = BotArtifact(
        artifact_id="bot_b",
        display_name="BotB",
        source_location="bots/b",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_b",
    )

    spec = MatchSpec(
        match_id="synth_crash_1",
        adapter_name="official",
        adapter_version="1.0.0",
        bot_a_id="bot_a",
        bot_b_id="bot_b",
        map_name="synth_grid_8x8",
        seed=42,
        time_limit_ms=5000,
    )

    res = adapter.run_local_match(spec, bot_a, bot_b, tmp_path)
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.ENGINE_CRASH


def test_fake_official_adapter_timeout(tmp_path: Path):
    bridge = FakeOfficialBridge()
    runner = OfficialCommandRunner()
    adapter = OfficialAdapter(bridge=bridge, command_runner=runner)

    bot_a = BotArtifact(
        artifact_id="bot_a",
        display_name="BotA",
        source_location="bots/a",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_a",
        tags=["fail:timeout"],
    )
    bot_b = BotArtifact(
        artifact_id="bot_b",
        display_name="BotB",
        source_location="bots/b",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_b",
    )

    spec = MatchSpec(
        match_id="synth_timeout_1",
        adapter_name="official",
        adapter_version="1.0.0",
        bot_a_id="bot_a",
        bot_b_id="bot_b",
        map_name="synth_grid_8x8",
        seed=42,
        time_limit_ms=500,  # 0.5s timeout vs 5s sleep in engine
    )

    res = adapter.run_local_match(spec, bot_a, bot_b, tmp_path)
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.TIMEOUT


def test_fake_official_adapter_cancellation(tmp_path: Path):
    bridge = FakeOfficialBridge()
    runner = OfficialCommandRunner()
    adapter = OfficialAdapter(bridge=bridge, command_runner=runner)

    bot_a = BotArtifact(
        artifact_id="bot_a",
        display_name="BotA",
        source_location="bots/a",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_a",
        tags=["fail:timeout"],
    )
    bot_b = BotArtifact(
        artifact_id="bot_b",
        display_name="BotB",
        source_location="bots/b",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_b",
    )

    spec = MatchSpec(
        match_id="synth_cancel_1",
        adapter_name="official",
        adapter_version="1.0.0",
        bot_a_id="bot_a",
        bot_b_id="bot_b",
        map_name="synth_grid_8x8",
        seed=42,
        time_limit_ms=10000,
    )

    cancel_evt = threading.Event()

    def cancel_after_delay():
        import time

        time.sleep(0.1)
        cancel_evt.set()

    threading.Thread(target=cancel_after_delay, daemon=True).start()
    res = adapter.run_local_match(spec, bot_a, bot_b, tmp_path, cancel_event=cancel_evt)
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.UNKNOWN_INFRASTRUCTURE


def test_fake_official_adapter_malformed_replay(tmp_path: Path):
    bridge = FakeOfficialBridge()
    runner = OfficialCommandRunner()
    adapter = OfficialAdapter(bridge=bridge, command_runner=runner)

    bot_a = BotArtifact(
        artifact_id="bot_a",
        display_name="BotA",
        source_location="bots/a",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_a",
        tags=["fail:malformed_replay"],
    )
    bot_b = BotArtifact(
        artifact_id="bot_b",
        display_name="BotB",
        source_location="bots/b",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_b",
    )

    spec = MatchSpec(
        match_id="synth_bad_replay",
        adapter_name="official",
        adapter_version="1.0.0",
        bot_a_id="bot_a",
        bot_b_id="bot_b",
        map_name="synth_grid_8x8",
        seed=42,
        time_limit_ms=5000,
    )

    res = adapter.run_local_match(spec, bot_a, bot_b, tmp_path)
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.REPLAY_CORRUPTION


def test_fake_official_readiness_with_synthetic_bridge():
    bridge = FakeOfficialBridge()
    checker = OfficialReadinessChecker(bridge=bridge)
    report = checker.evaluate()

    # Synthetic SDK allows can_run_local=True because probe, build, match, replay, determinism all succeed!
    assert report.can_run_local is True
    # But overall ready is still False because source bundle and minimal official bot don't exist
    assert report.ready is False
    assert report.can_submit is False
