"""Official competition placeholder adapter.

Strictly non-functional until Autumn official rules and SDK documentation are released.
Contains ZERO fabricated commands, endpoints, or assumptions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from battlelab.adapters.base import GameAdapter
from battlelab.core.errors import CapabilityNotSupportedError
from battlelab.core.models import (
    BotArtifact,
    Capability,
    MatchResult,
    MatchSpec,
)


class OfficialPlaceholderAdapter(GameAdapter):
    """Placeholder adapter for official competition SDK.

    All operational methods raise CapabilityNotSupportedError until official
    rules ingestion is performed via `docs/day_zero_rule_ingestion.md`.
    """

    @property
    def name(self) -> str:
        return "official_placeholder"

    @property
    def version(self) -> str:
        return "0.0.0-unreleased"

    def validate_installation(self) -> tuple[bool, str]:
        return (
            False,
            "Official competition SDK not ingested. See docs/day_zero_rule_ingestion.md.",
        )

    def get_capabilities(self) -> Capability:
        return Capability(
            can_run_local=False,
            can_run_remote=False,
            can_submit=False,
            can_fetch_replays=False,
            can_list_matches=False,
            supported_languages=[],
            adapter_version=self.version,
            game_version="UNRELEASED_AUTUMN_COMPETITION",
        )

    def discover_maps(self) -> list[str]:
        return []

    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        return False, "Official competition bot format unknown pending rulebook release."

    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        raise CapabilityNotSupportedError(
            "build_or_prepare_artifact", self.name, "Official SDK not released."
        )

    def run_local_match(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
    ) -> MatchResult:
        raise CapabilityNotSupportedError(
            "run_local_match", self.name, "Official game engine not installed."
        )

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
        raise CapabilityNotSupportedError(
            "parse_replay", self.name, "Official replay schema unknown."
        )
