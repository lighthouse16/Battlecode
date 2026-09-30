"""Official competition adapter with generic orchestration and injected bridge/runner."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from battlelab.adapters.base import GameAdapter
from battlelab.core.errors import CapabilityNotSupportedError
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
from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.command_runner import InfrastructureTamperingError, OfficialCommandRunner
from battlelab.official.models import OfficialCommandPlan


class OfficialAdapter(GameAdapter):
    """Generic orchestration adapter for official competition games.

    Separates process runner security, timeouts, and match lifecycle
    from rule-specific engine commands and result parsing.
    """

    def __init__(
        self,
        bridge: OfficialEngineBridge | None = None,
        command_runner: OfficialCommandRunner | None = None,
    ) -> None:
        self.bridge = bridge if bridge is not None else UnconfiguredOfficialBridge()
        self.command_runner = (
            command_runner if command_runner is not None else OfficialCommandRunner()
        )

    @property
    def name(self) -> str:
        return "official"

    @property
    def version(self) -> str:
        return "0.0.0-unreleased"

    def validate_installation(self) -> tuple[bool, str]:
        if isinstance(self.bridge, UnconfiguredOfficialBridge):
            return (
                False,
                "Official competition SDK not ingested or configured. See docs/day_zero_rule_ingestion.md.",
            )
        exe_raw = self.bridge.get_sdk_executable()
        if exe_raw is None and hasattr(self.bridge, "sdk_path"):
            exe_raw = getattr(self.bridge, "sdk_path")
        if exe_raw is None or not str(exe_raw).strip():
            return False, "Bridge has no declared SDK executable or launcher."

        exe_path = Path(exe_raw)
        if not exe_path.exists():
            return False, f"Official SDK executable does not exist: {exe_path}"

        import os
        import stat

        try:
            st = os.lstat(exe_path)
            if stat.S_ISLNK(st.st_mode):
                return False, f"Official SDK executable cannot be a symlink: {exe_path}"
            if not stat.S_ISREG(st.st_mode):
                return False, f"Official SDK executable must be a regular file: {exe_path}"
        except OSError as e:
            return False, f"Cannot access official SDK executable: {e}"

        try:
            from battlelab.official.readiness import generate_sdk_evidence

            ok, msg, evidence = generate_sdk_evidence(self.bridge, self.command_runner)
            if not ok or evidence is None:
                return False, msg
            return True, f"Official SDK validated: {evidence.sdk_version}"
        except Exception as e:
            return False, f"Official SDK validation failed: {e}"

    def get_capabilities(self) -> Capability:
        if isinstance(self.bridge, UnconfiguredOfficialBridge):
            return Capability(
                can_run_local=False,
                can_run_remote=False,
                can_submit=False,
                can_fetch_replays=False,
                can_list_matches=False,
                supported_languages=[],
                adapter_version=self.version,
                game_version="UNKNOWN",
            )
        try:
            valid_install, _ = self.validate_installation()
            if not valid_install:
                return Capability(
                    can_run_local=False,
                    can_run_remote=False,
                    can_submit=False,
                    can_fetch_replays=False,
                    can_list_matches=False,
                    supported_languages=[],
                    adapter_version=self.version,
                    game_version="UNKNOWN",
                )

            # Derive can_run_local from honest readiness report, never self-reported probe booleans
            from battlelab.official.readiness import OfficialReadinessChecker
            from battlelab.official.spec import load_and_validate_spec

            checker = OfficialReadinessChecker(bridge=self.bridge, runner=self.command_runner)
            report = checker.evaluate()

            game_ver = "UNKNOWN"
            supported_langs: list[str] = []
            if checker.spec_path.is_file():
                is_valid, _, spec_obj, _ = load_and_validate_spec(checker.spec_path)
                if is_valid and spec_obj:
                    if spec_obj.game_version:
                        game_ver = spec_obj.game_version
                    if report.can_run_local and spec_obj.supported_languages:
                        supported_langs = list(spec_obj.supported_languages)

            return Capability(
                can_run_local=report.can_run_local,
                can_run_remote=False,
                can_submit=False,  # Unconditionally false
                can_fetch_replays=False,
                can_list_matches=False,
                supported_languages=supported_langs,
                adapter_version=self.version,
                game_version=game_ver,
            )
        except Exception:
            return Capability(
                can_run_local=False,
                can_run_remote=False,
                can_submit=False,
                can_fetch_replays=False,
                can_list_matches=False,
                supported_languages=[],
                adapter_version=self.version,
                game_version="UNKNOWN",
            )

    def discover_maps(self) -> list[str]:
        return self.bridge.discover_maps()

    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        return self.bridge.validate_bot_compatibility(bot_artifact)

    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        return self.bridge.build_or_prepare_artifact(source_path, output_dir)

    def run_local_match(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
        cancel_event: threading.Event | None = None,
    ) -> MatchResult:
        # 1. Validate limits before launching
        if (
            isinstance(spec.match_wall_clock_limit_ms, bool)
            or not isinstance(spec.match_wall_clock_limit_ms, (int, float))
            or spec.match_wall_clock_limit_ms <= 0
        ):
            raise ValueError(
                f"match_wall_clock_limit_ms must be a positive number, got {spec.match_wall_clock_limit_ms!r}"
            )
        if (
            isinstance(spec.per_turn_limit_ms, bool)
            or not isinstance(spec.per_turn_limit_ms, (int, float))
            or spec.per_turn_limit_ms <= 0
        ):
            raise ValueError(
                f"per_turn_limit_ms must be a positive number, got {spec.per_turn_limit_ms!r}"
            )
        if (
            isinstance(spec.memory_limit_mb, bool)
            or not isinstance(spec.memory_limit_mb, int)
            or spec.memory_limit_mb < 0
        ):
            raise ValueError(
                f"memory_limit_mb must be a non-negative integer, got {spec.memory_limit_mb!r}"
            )

        # 2. Ensure working directory exists
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # 3. Build match command
        cmd = self.bridge.build_match_command(spec, bot_a, bot_b, work_dir)

        timeout_sec = max(0.1, float(spec.match_wall_clock_limit_ms) / 1000.0)

        if isinstance(cmd, OfficialCommandPlan):
            try:
                cmd_result = self.command_runner.execute_plan(
                    plan=cmd,
                    timeout_seconds=timeout_sec,
                    cancel_event=cancel_event,
                    memory_limit_mb=spec.memory_limit_mb,
                )
            except InfrastructureTamperingError as e:
                return MatchResult(
                    match_id=spec.match_id,
                    outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                    winner=None,
                    score_a=0.0,
                    score_b=0.0,
                    turns_played=0,
                    duration_ms=0.0,
                    replay_path=None,
                    replay_hash=None,
                    failure_classification=FailureClassification(
                        category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                        culprit="system",
                        evidence=f"Infrastructure tampering detected: {e}",
                    ),
                )
        else:
            # 4. Verify match command uses the declared SDK executable identity
            declared_exe = self.bridge.get_sdk_executable()
            if declared_exe is not None:
                dec_p = Path(declared_exe).resolve()
                cmd_resolved = [
                    str(Path(c).resolve()) if (isinstance(c, str) and Path(c).exists()) else str(c)
                    for c in cmd
                ]
                if str(dec_p) not in cmd_resolved:
                    return MatchResult(
                        match_id=spec.match_id,
                        outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                        winner=None,
                        score_a=0.0,
                        score_b=0.0,
                        turns_played=0,
                        duration_ms=0.0,
                        replay_path=None,
                        replay_hash=None,
                        failure_classification=FailureClassification(
                            category=FailureCategory.ENGINE_CRASH,
                            culprit="engine",
                            evidence=f"Match command does not invoke verified SDK executable '{dec_p}': {cmd}",
                        ),
                    )

            # 5. Run external process using match_wall_clock_limit_ms
            cmd_result = self.command_runner.run(
                argv=cmd,
                cwd=work_dir,
                timeout_seconds=timeout_sec,
                cancel_event=cancel_event,
                memory_limit_mb=spec.memory_limit_mb,
            )

        # 6. Handle abnormal process outcomes
        if cmd_result.timed_out:
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                winner=None,
                score_a=0.0,
                score_b=0.0,
                turns_played=0,
                duration_ms=cmd_result.duration_ms,
                replay_path=None,
                replay_hash=None,
                failure_classification=FailureClassification(
                    category=FailureCategory.TIMEOUT,
                    culprit="engine",
                    evidence=f"Command exceeded {timeout_sec:.1f}s match wall-clock timeout",
                ),
            )

        if cmd_result.cancelled:
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                winner=None,
                score_a=0.0,
                score_b=0.0,
                turns_played=0,
                duration_ms=cmd_result.duration_ms,
                replay_path=None,
                replay_hash=None,
                failure_classification=FailureClassification(
                    category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                    culprit="system",
                    evidence="Execution cancelled via cancel_event",
                ),
            )

        # Non-zero engine exit MUST always produce INFRASTRUCTURE_FAILURE
        if cmd_result.exit_code != 0:
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                winner=None,
                score_a=0.0,
                score_b=0.0,
                turns_played=0,
                duration_ms=cmd_result.duration_ms,
                replay_path=None,
                replay_hash=None,
                failure_classification=FailureClassification(
                    category=FailureCategory.ENGINE_CRASH,
                    culprit="engine",
                    evidence=f"Official engine non-zero exit code {cmd_result.exit_code}: {cmd_result.stderr[:500]}",
                ),
            )

        # 5. Parse result via bridge
        try:
            res = self.bridge.parse_match_result(spec, cmd_result, work_dir)
        except Exception as e:
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                winner=None,
                score_a=0.0,
                score_b=0.0,
                turns_played=0,
                duration_ms=cmd_result.duration_ms,
                replay_path=None,
                replay_hash=None,
                failure_classification=FailureClassification(
                    category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                    culprit="engine",
                    evidence=f"Failed to parse official match result: {e}",
                ),
            )

        # 6. Discover and parse replay
        replay_path = self.bridge.locate_replay(spec, work_dir)
        if not replay_path or not Path(replay_path).is_file():
            res.outcome = MatchOutcome.INFRASTRUCTURE_FAILURE
            res.winner = None
            res.failure_classification = FailureClassification(
                category=FailureCategory.REPLAY_CORRUPTION,
                culprit="engine",
                evidence=f"Replay file not found or not a regular file: {replay_path}",
            )
            return res

        r_path = Path(replay_path)
        res.replay_path = str(r_path)
        res.replay_hash = hash_file(r_path)
        try:
            _ = self.bridge.parse_replay(r_path)
        except Exception as e:
            res.outcome = MatchOutcome.INFRASTRUCTURE_FAILURE
            res.winner = None
            res.failure_classification = FailureClassification(
                category=FailureCategory.REPLAY_CORRUPTION,
                culprit="engine",
                evidence=f"Failed to parse official replay: {e}",
            )
            return res

        return res

    def run_remote_test(self, bot_artifact: BotArtifact) -> dict[str, Any]:
        raise CapabilityNotSupportedError(
            "run_remote_test", self.name, "Official ladder/API not configured."
        )

    def submit_artifact(self, bot_artifact: BotArtifact, dry_run: bool = True) -> dict[str, Any]:
        raise CapabilityNotSupportedError(
            "submit_artifact", self.name, "Submission endpoints unknown."
        )

    def list_official_matches(self) -> list[dict[str, Any]]:
        raise CapabilityNotSupportedError(
            "list_official_matches", self.name, "Official match listing unavailable."
        )

    def parse_replay(self, replay_path: Path) -> dict[str, Any]:
        return self.bridge.parse_replay(replay_path).to_dict()
