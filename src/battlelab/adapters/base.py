"""Abstract Base Class for Game Adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from battlelab.core.errors import CapabilityNotSupportedError
from battlelab.core.models import (
    BotArtifact,
    Capability,
    MatchResult,
    MatchSpec,
)


class GameAdapter(ABC):
    """Abstract interface governing game execution and integration."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the adapter."""
        pass

    @property
    @abstractmethod
    def version(self) -> str:
        """Version of the adapter."""
        pass

    @abstractmethod
    def validate_installation(self) -> tuple[bool, str]:
        """Check if required runtime/SDK is available on this system."""
        pass

    @abstractmethod
    def get_capabilities(self) -> Capability:
        """Return explicit structured capability flags."""
        pass

    @abstractmethod
    def discover_maps(self) -> list[str]:
        """Return list of map identifiers available for this game."""
        pass

    @abstractmethod
    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        """Validate if a bot artifact is supported by this adapter."""
        pass

    @abstractmethod
    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        """Compile or package a bot into an immutable artifact."""
        pass

    @abstractmethod
    def run_local_match(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
    ) -> MatchResult:
        """Run a single local match between two bots deterministically."""
        pass

    def run_remote_test(self, bot_artifact: BotArtifact) -> dict[str, Any]:
        """Run remote official test if supported."""
        raise CapabilityNotSupportedError("run_remote_test", self.name)

    def submit_artifact(self, bot_artifact: BotArtifact, dry_run: bool = True) -> dict[str, Any]:
        """Submit bot to competition ladder if supported and explicitly authorized."""
        raise CapabilityNotSupportedError("submit_artifact", self.name)

    def list_official_matches(self) -> list[dict[str, Any]]:
        """List official remote ladder matches if supported."""
        raise CapabilityNotSupportedError("list_official_matches", self.name)

    @abstractmethod
    def parse_replay(self, replay_path: Path) -> dict[str, Any]:
        """Parse replay file into generic structured representation."""
        pass
