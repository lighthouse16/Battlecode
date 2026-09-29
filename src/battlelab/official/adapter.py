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
from battlelab.official.command_runner import OfficialCommandRunner


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
        try:
            probe = self.bridge.probe_sdk()
            if not isinstance(probe, dict):
                return False, "probe_sdk() must return a dictionary."

            # 1. Executable exists
            if hasattr(self.bridge, "sdk_path"):
                sdk_p = Path(getattr(self.bridge, "sdk_path"))
                if not sdk_p.exists():
                    return False, f"Official SDK executable does not exist: {sdk_p}"
            exe_exists = bool(probe.get("executable_exists", False))
            if not exe_exists:
                return False, "Official SDK executable does not exist."

            # 2. Executable runnable
            exe_runnable = bool(probe.get("executable_runnable", False))
            if not exe_runnable:
                return False, "Official SDK executable is not runnable."

            # 3. Non-empty version obtained from actual execution
            sdk_version = probe.get("sdk_version")
            if (
                not sdk_version
                or not isinstance(sdk_version, str)
                or not sdk_version.strip()
                or sdk_version.strip().lower()
                in ("unknown", "missing", "unspecified", "unreleased")
            ):
                return False, "Official SDK version could not be determined from actual execution."

            return True, f"Official SDK validated: {sdk_version.strip()}"
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
            probe = self.bridge.probe_sdk()
            valid_install, _ = self.validate_installation()
            # can_run_local requires valid installation and probe evidence
            can_run_local = valid_install and bool(probe.get("can_run_local", False))
            return Capability(
                can_run_local=can_run_local,
                can_run_remote=False,
                can_submit=False,  # Unconditionally false until explicit audited unlock mechanism exists
                can_fetch_replays=bool(probe.get("can_fetch_replays", False)),
                can_list_matches=bool(probe.get("can_list_matches", False)),
                supported_languages=list(probe.get("supported_languages", [])),
                adapter_version=self.version,
                game_version=str(probe.get("game_version", "UNKNOWN")),
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
        # 1. Ensure working directory exists
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # 2. Build match command
        cmd = self.bridge.build_match_command(spec, bot_a, bot_b, work_dir)

        # 3. Run external process
        timeout_sec = max(1.0, spec.time_limit_ms / 1000.0)
        cmd_result = self.command_runner.run(
            argv=cmd,
            cwd=work_dir,
            timeout_seconds=timeout_sec,
            cancel_event=cancel_event,
        )

        # 4. Handle abnormal process outcomes
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
                    evidence=f"Command exceeded {timeout_sec:.1f}s timeout",
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
