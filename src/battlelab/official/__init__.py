"""Official competition integration subsystem for Battlelab."""

from battlelab.official.adapter import OfficialAdapter
from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.command_runner import OfficialCommandRunner
from battlelab.official.models import (
    CommandResult,
    DeterminismComparisonResult,
    GameSpec,
    NormalizedReplay,
    ReadinessCheckItem,
    ReadinessReport,
    RuleItem,
    RuleVerificationState,
    SourceBundleManifest,
    SourceFileEntry,
    compare_replay_determinism,
)
from battlelab.official.readiness import OfficialReadinessChecker
from battlelab.official.sources import ingest_sources, load_source_bundle_manifest
from battlelab.official.spec import init_game_spec, load_and_validate_spec, validate_game_spec

__all__ = [
    "CommandResult",
    "DeterminismComparisonResult",
    "GameSpec",
    "NormalizedReplay",
    "OfficialAdapter",
    "OfficialCommandRunner",
    "OfficialEngineBridge",
    "OfficialReadinessChecker",
    "ReadinessCheckItem",
    "ReadinessReport",
    "RuleItem",
    "RuleVerificationState",
    "SourceBundleManifest",
    "SourceFileEntry",
    "UnconfiguredOfficialBridge",
    "compare_replay_determinism",
    "ingest_sources",
    "init_game_spec",
    "load_and_validate_spec",
    "load_source_bundle_manifest",
    "validate_game_spec",
]
