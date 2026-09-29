"""Data models for official competition integration subsystem."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from battlelab.core.hashing import hash_dict


@dataclass
class SourceFileEntry:
    """Metadata for a single source file in an ingested bundle."""

    relpath: str
    sha256: str
    size_bytes: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceFileEntry:
        if not isinstance(data, dict):
            raise TypeError("SourceFileEntry data must be a dictionary")
        relpath = data.get("relpath")
        if not isinstance(relpath, str):
            raise TypeError("relpath must be a string")
        if not relpath or relpath.startswith("/") or "\\" in relpath or ":" in relpath:
            raise ValueError(f"Invalid non-POSIX or absolute relpath: {relpath!r}")
        parts = relpath.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"Invalid path segments in relpath: {relpath!r}")

        sha256 = data.get("sha256")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or not all(c in "0123456789abcdef" for c in sha256)
        ):
            raise ValueError(f"sha256 must be 64 lowercase hex characters: {sha256!r}")

        size_bytes = data.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(
                f"size_bytes must be a non-negative integer, not boolean: {size_bytes!r}"
            )

        media_type = data.get("media_type", "application/octet-stream")
        if not isinstance(media_type, str):
            raise TypeError(f"media_type must be a string: {media_type!r}")

        return cls(
            relpath=relpath,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )


@dataclass
class SourceBundleManifest:
    """Manifest for an ingested source bundle."""

    schema_version: str
    bundle_hash: str
    source_path: str
    created_at: str
    file_count: int
    total_size_bytes: int
    files: list[SourceFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bundle_hash": self.bundle_hash,
            "source_path": self.source_path,
            "created_at": self.created_at,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceBundleManifest:
        if not isinstance(data, dict):
            raise TypeError("SourceBundleManifest data must be a dictionary")
        schema_version = data.get("schema_version", "1.0.0")
        if schema_version != "1.0.0" or not isinstance(schema_version, str):
            raise ValueError(f"Unsupported schema_version: {schema_version!r}")

        bundle_hash = data.get("bundle_hash")
        if (
            not isinstance(bundle_hash, str)
            or len(bundle_hash) != 64
            or not all(c in "0123456789abcdef" for c in bundle_hash)
        ):
            raise ValueError(f"bundle_hash must be 64 lowercase hex characters: {bundle_hash!r}")

        source_path = data.get("source_path", "")
        if not isinstance(source_path, str):
            raise TypeError(f"source_path must be a string: {source_path!r}")
        if source_path.startswith("/") or "\\" in source_path or ":" in source_path:
            raise ValueError(
                f"source_path must not be a machine-specific absolute path: {source_path!r}"
            )

        created_at = data.get("created_at", "")
        if not isinstance(created_at, str):
            raise TypeError(f"created_at must be a string: {created_at!r}")

        file_count = data.get("file_count")
        if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
            raise ValueError(
                f"file_count must be a non-negative integer, not boolean: {file_count!r}"
            )

        total_size_bytes = data.get("total_size_bytes")
        if (
            isinstance(total_size_bytes, bool)
            or not isinstance(total_size_bytes, int)
            or total_size_bytes < 0
        ):
            raise ValueError(
                f"total_size_bytes must be a non-negative integer, not boolean: {total_size_bytes!r}"
            )

        raw_files = data.get("files")
        if not isinstance(raw_files, list):
            raise TypeError("files must be a list of file entries")

        files = [
            f if isinstance(f, SourceFileEntry) else SourceFileEntry.from_dict(f) for f in raw_files
        ]

        # Check unique relpaths
        seen_rel = set()
        for f in files:
            if f.relpath in seen_rel:
                raise ValueError(f"Duplicate relpath in manifest: {f.relpath!r}")
            seen_rel.add(f.relpath)

        if file_count != len(files):
            raise ValueError(f"file_count ({file_count}) does not match len(files) ({len(files)})")

        computed_total_size = sum(f.size_bytes for f in files)
        if total_size_bytes != computed_total_size:
            raise ValueError(
                f"total_size_bytes ({total_size_bytes}) does not match sum of file sizes ({computed_total_size})"
            )

        return cls(
            schema_version=schema_version,
            bundle_hash=bundle_hash,
            source_path=source_path,
            created_at=created_at,
            file_count=file_count,
            total_size_bytes=total_size_bytes,
            files=files,
        )


class RuleVerificationState(str, Enum):
    """Allowed states for rule item verification."""

    MISSING = "MISSING"
    DOCUMENTED = "DOCUMENTED"
    TEST_VERIFIED = "TEST_VERIFIED"


@dataclass
class RuleItem:
    """Structure for an individual game rule or integration specification entry."""

    meaning: str = ""
    source_refs: list[str] = field(default_factory=list)
    verification_state: str = RuleVerificationState.MISSING.value
    implementation_impacts: list[str] = field(default_factory=list)
    test_coverage: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "meaning": self.meaning,
            "source_refs": list(self.source_refs),
            "verification_state": self.verification_state,
            "implementation_impacts": list(self.implementation_impacts),
            "test_coverage": list(self.test_coverage),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleItem:
        return cls(
            meaning=str(data.get("meaning", "")),
            source_refs=[str(x) for x in data.get("source_refs", [])],
            verification_state=str(
                data.get("verification_state", RuleVerificationState.MISSING.value)
            ),
            implementation_impacts=[str(x) for x in data.get("implementation_impacts", [])],
            test_coverage=[str(x) for x in data.get("test_coverage", [])],
            notes=str(data.get("notes", "")),
        )


@dataclass
class GameSpec:
    """Typed, source-backed specification for official competition rules."""

    schema_version: str
    competition_name: str
    competition_season: str
    spec_version: str
    source_bundle_hash: str
    official_document_hashes: list[str]
    sdk_version: str
    generated_at: str
    updated_at: str
    rules: dict[str, RuleItem] = field(default_factory=dict)

    def canonical_hash(self) -> str:
        """Compute deterministic hash of specification excluding timestamps."""
        canonical_data = {
            "schema_version": self.schema_version,
            "competition_name": self.competition_name,
            "competition_season": self.competition_season,
            "spec_version": self.spec_version,
            "source_bundle_hash": self.source_bundle_hash,
            "official_document_hashes": sorted(self.official_document_hashes),
            "sdk_version": self.sdk_version,
            "rules": {k: self.rules[k].to_dict() for k in sorted(self.rules.keys())},
        }
        return hash_dict(canonical_data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "competition_name": self.competition_name,
            "competition_season": self.competition_season,
            "spec_version": self.spec_version,
            "source_bundle_hash": self.source_bundle_hash,
            "official_document_hashes": list(self.official_document_hashes),
            "sdk_version": self.sdk_version,
            "generated_at": self.generated_at,
            "updated_at": self.updated_at,
            "rules": {k: v.to_dict() for k, v in self.rules.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameSpec:
        rules_raw = data.get("rules", {})
        rules = {k: RuleItem.from_dict(v) for k, v in rules_raw.items()}
        return cls(
            schema_version=str(data.get("schema_version", "1.0.0")),
            competition_name=str(data.get("competition_name", "")),
            competition_season=str(data.get("competition_season", "")),
            spec_version=str(data.get("spec_version", "1.0.0")),
            source_bundle_hash=str(data.get("source_bundle_hash", "")),
            official_document_hashes=[str(x) for x in data.get("official_document_hashes", [])],
            sdk_version=str(data.get("sdk_version", "")),
            generated_at=str(data.get("generated_at", "")),
            updated_at=str(data.get("updated_at", "")),
            rules=rules,
        )


@dataclass
class CommandResult:
    """Execution output from OfficialCommandRunner."""

    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    cancelled: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedReplay:
    """Rule-independent normalized replay representation.

    Determinism Separation Semantics:
    - raw_replay_hash: SHA-256 checksum of raw byte content of the replay file on disk.
      Captures byte-level serialization differences (formatting, key order, whitespace).
    - canonical_hash(): Deterministic SHA-256 hash of normalized gameplay data.
      Strictly EXCLUDES raw_replay_hash and volatile source_metadata. Two replays with
      identical gameplay moves and events will produce identical canonical hashes even
      if their raw file bytes differ.
    - source_metadata: Volatile engine execution telemetry, timestamps, and diagnostics
      that do not impact gameplay mechanics.
    """

    schema_version: str = "1.0.0"
    adapter_name: str = ""
    adapter_version: str = ""
    game_version: str = ""
    map_id: str = ""
    seed: int = 0
    participants: dict[str, str] = field(default_factory=dict)
    outcome: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    turn_count: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    raw_replay_hash: str = ""
    source_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_frames(self) -> int:
        """Backward-compatible frame count."""
        return max(self.turn_count, len(self.events))

    def canonical_hash(self) -> str:
        """Hash parsed normalized gameplay data.

        Excludes raw_replay_hash and volatile source_metadata.
        """
        payload = {
            "schema_version": self.schema_version,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "game_version": self.game_version,
            "map_id": self.map_id,
            "seed": self.seed,
            "participants": self.participants,
            "outcome": self.outcome,
            "scores": self.scores,
            "turn_count": self.turn_count,
            "events": self.events,
        }
        return hash_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_frames"] = self.total_frames
        return d

    def __getitem__(self, key: str) -> Any:
        if key == "total_frames":
            return self.total_frames
        if key == "meta":
            return self.source_metadata.get("meta", {})
        if key == "final_frame":
            return self.source_metadata.get("final_frame")
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (AttributeError, KeyError):
            return default

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key) or key in ("total_frames", "meta", "final_frame")


@dataclass
class DeterminismComparisonResult:
    """Outcome of comparing two replay runs."""

    raw_replay_equal: bool
    gameplay_equal: bool
    volatile_metadata_equal: bool
    differences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_replay_determinism(
    replay_a: NormalizedReplay, replay_b: NormalizedReplay
) -> DeterminismComparisonResult:
    """Compare two normalized replays distinguishing raw bytes, gameplay, and metadata."""
    differences: list[str] = []

    # 1. Raw replay equality (byte level)
    raw_equal = bool(
        replay_a.raw_replay_hash
        and replay_b.raw_replay_hash
        and replay_a.raw_replay_hash == replay_b.raw_replay_hash
    )
    if not raw_equal:
        differences.append(
            f"Raw replay hash difference: {replay_a.raw_replay_hash} != {replay_b.raw_replay_hash}"
        )

    # 2. Gameplay equality (canonical gameplay hash)
    gameplay_equal = replay_a.canonical_hash() == replay_b.canonical_hash()
    if not gameplay_equal:
        if replay_a.outcome != replay_b.outcome:
            differences.append(f"Outcome difference: {replay_a.outcome} != {replay_b.outcome}")
        if replay_a.scores != replay_b.scores:
            differences.append(f"Scores difference: {replay_a.scores} != {replay_b.scores}")
        if replay_a.turn_count != replay_b.turn_count:
            differences.append(
                f"Turn count difference: {replay_a.turn_count} != {replay_b.turn_count}"
            )
        if replay_a.map_id != replay_b.map_id or replay_a.seed != replay_b.seed:
            differences.append(
                f"Context difference: map({replay_a.map_id} vs {replay_b.map_id}), "
                f"seed({replay_a.seed} vs {replay_b.seed})"
            )
        if replay_a.events != replay_b.events:
            differences.append(
                f"Events difference ({len(replay_a.events)} vs {len(replay_b.events)})"
            )

    # 3. Volatile metadata equality
    volatile_equal = replay_a.source_metadata == replay_b.source_metadata
    if not volatile_equal:
        differences.append("Volatile source metadata differences present")

    return DeterminismComparisonResult(
        raw_replay_equal=raw_equal,
        gameplay_equal=gameplay_equal,
        volatile_metadata_equal=volatile_equal,
        differences=differences,
    )


@dataclass
class ReadinessCheckItem:
    """Individual readiness evaluation check."""

    name: str
    description: str
    passed: bool
    blocker: bool
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReadinessReport:
    """Comprehensive readiness assessment."""

    ready: bool
    can_run_local: bool
    can_submit: bool
    checks: list[ReadinessCheckItem]
    source_bundle_hash: str | None = None
    spec_hash: str | None = None
    sdk_version: str | None = None
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "can_run_local": self.can_run_local,
            "can_submit": self.can_submit,
            "source_bundle_hash": self.source_bundle_hash,
            "spec_hash": self.spec_hash,
            "sdk_version": self.sdk_version,
            "blockers": list(self.blockers),
            "checks": [c.to_dict() for c in self.checks],
        }
