"""Domain models for artifacts, matches, experiments, capabilities, and failures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any


class MatchOutcome(str, Enum):
    WIN_A = "WIN_A"
    WIN_B = "WIN_B"
    DRAW = "DRAW"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"


class FailureCategory(str, Enum):
    BOT_CRASH = "BOT_CRASH"
    ENGINE_CRASH = "ENGINE_CRASH"
    TIMEOUT = "TIMEOUT"
    BUILD_FAILURE = "BUILD_FAILURE"
    INVALID_ACTION = "INVALID_ACTION"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    REPLAY_CORRUPTION = "REPLAY_CORRUPTION"
    NONDETERMINISM = "NONDETERMINISM"
    WORKER_INTERRUPTION = "WORKER_INTERRUPTION"
    STORAGE_FAILURE = "STORAGE_FAILURE"
    UNKNOWN_INFRASTRUCTURE = "UNKNOWN_INFRASTRUCTURE"
    GAMEPLAY_LOSS = "GAMEPLAY_LOSS"


@dataclass
class FailureClassification:
    category: FailureCategory
    culprit: str | None = None  # e.g. "bot_a", "bot_b", "engine", "system"
    evidence: str = ""
    confidence: float = 1.0
    is_inference: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "culprit": self.culprit,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "is_inference": self.is_inference,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailureClassification:
        return cls(
            category=FailureCategory(data["category"]),
            culprit=data.get("culprit"),
            evidence=data.get("evidence", ""),
            confidence=float(data.get("confidence", 1.0)),
            is_inference=bool(data.get("is_inference", False)),
        )


@dataclass
class ResolvedOpponent:
    config_id: str
    artifact_id: str
    name: str = ""
    group: str = "general"
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)
    role: str = "opponent"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Capability:
    can_run_local: bool = True
    can_run_remote: bool = False
    can_submit: bool = False
    can_fetch_replays: bool = False
    can_list_matches: bool = False
    supported_languages: list[str] = field(default_factory=lambda: ["python"])
    adapter_version: str = "0.1.0"
    game_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capability:
        return cls(**data)


@dataclass
class BotArtifact:
    artifact_id: str
    display_name: str
    source_location: str
    language: str
    git_commit: str | None
    dirty_worktree: bool
    source_hash: str
    build_config_hash: str = ""
    parent_artifact_id: str | None = None
    created_at: str = ""
    experiment_id: str | None = None
    hypothesis: str | None = None
    tags: list[str] = field(default_factory=list)
    build_result: dict[str, Any] = field(default_factory=dict)
    build_logs: str = ""
    entrypoint_relpath: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BotArtifact:
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class MatchSpec:
    match_id: str
    adapter_name: str
    adapter_version: str
    bot_a_id: str
    bot_b_id: str
    map_name: str
    seed: int
    side_assignment: dict[str, str] = field(default_factory=lambda: {"A": "side_0", "B": "side_1"})
    execution_mode: str = "local"
    per_turn_limit_ms: int = 5000
    match_wall_clock_limit_ms: int = 60000
    memory_limit_mb: int = 512
    cpu_limit_cores: float | None = None
    time_limit_ms: int = 5000  # Backwards compatibility alias for per_turn_limit_ms
    config_hash: str = ""
    retry_attempt: int = 0
    max_attempts: int = 3
    pair_id: str | None = None
    experiment_id: str | None = None
    tournament_id: str | None = None

    def __post_init__(self) -> None:
        # Keep time_limit_ms and per_turn_limit_ms synchronized
        if self.time_limit_ms != 5000 and self.per_turn_limit_ms == 5000:
            self.per_turn_limit_ms = self.time_limit_ms
        elif self.per_turn_limit_ms != 5000 and self.time_limit_ms == 5000:
            self.time_limit_ms = self.per_turn_limit_ms

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchSpec:
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class MatchResult:
    match_id: str
    outcome: MatchOutcome
    winner: str | None = None  # "A", "B", or None
    score_a: float = 0.0
    score_b: float = 0.0
    duration_ms: float = 0.0
    turns_played: int = 0
    bot_a_stats: dict[str, Any] = field(default_factory=dict)
    bot_b_stats: dict[str, Any] = field(default_factory=dict)
    exit_code_a: int = 0
    exit_code_b: int = 0
    crashed_a: bool = False
    crashed_b: bool = False
    timed_out_a: bool = False
    timed_out_b: bool = False
    invalid_action_a: bool = False
    invalid_action_b: bool = False
    replay_path: str | None = None
    replay_hash: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    adapter_metadata: dict[str, Any] = field(default_factory=dict)
    failure_classification: FailureClassification | None = None
    completed_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        if self.failure_classification:
            d["failure_classification"] = self.failure_classification.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchResult:
        data_copy = dict(data)
        data_copy["outcome"] = MatchOutcome(data_copy["outcome"])
        if fc_data := data_copy.get("failure_classification"):
            data_copy["failure_classification"] = FailureClassification.from_dict(fc_data)
        return cls(**data_copy)


@dataclass
class Experiment:
    experiment_id: str
    hypothesis: str
    baseline_artifact_id: str
    challenger_artifact_id: str
    intended_change: str
    evaluation_matrix: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: dict[str, Any] = field(default_factory=dict)
    status: str = "CREATED"  # CREATED, RUNNING, COMPLETED, FAILED
    created_at: str = ""
    completed_at: str | None = None
    results_summary: dict[str, Any] | None = None
    promotion_decision: str = "PENDING"  # PENDING, PROMOTED, REJECTED
    rejection_reason: str | None = None
    representative_replays: list[str] = field(default_factory=list)
    evaluation_config: dict[str, Any] = field(default_factory=dict)
    evaluation_config_hash: str = ""
    opponent_pool_config: dict[str, Any] = field(default_factory=dict)
    opponent_pool_config_hash: str = ""
    promotion_config_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Experiment:
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)
