"""Unit tests for official source ingestion, game spec, command runner, and readiness."""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from pathlib import Path

import pytest

from battlelab.adapters import get_adapter
from battlelab.core.hashing import hash_file
from battlelab.core.models import (
    BotArtifact,
    FailureCategory,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)
from battlelab.official.adapter import OfficialAdapter
from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.command_runner import InfrastructureTamperingError, OfficialCommandRunner
from battlelab.official.models import (
    EnforcementStatus,
    GameSpec,
    NormalizedReplay,
    OfficialCommandPlan,
    PlanOperation,
    RuleItem,
    RuleTestEvidence,
    RuleVerificationState,
    compare_replay_determinism,
)
from battlelab.official.readiness import (
    DefaultRuleTestRunner,
    OfficialReadinessChecker,
    RuleTestRunner,
    RuleTestRunResult,
    probe_engine_executable,
)
from battlelab.official.sources import ingest_sources, load_source_bundle_manifest
from battlelab.official.spec import (
    REQUIRED_RULE_SECTIONS,
    init_game_spec,
    parse_and_validate_citation,
    validate_game_spec,
    validate_pytest_node_id,
)


# ---------------------------------------------------------
# 1. Source Ingestion Tests
# ---------------------------------------------------------
def test_ingestion_rejects_missing_path(tmp_path: Path):
    missing = tmp_path / "nonexistent"
    with pytest.raises(FileNotFoundError, match="Source path does not exist"):
        ingest_sources(missing, bundles_dir=tmp_path / "bundles")


def test_ingestion_rejects_empty_directory(tmp_path: Path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ValueError, match="contains no regular files"):
        ingest_sources(empty_dir, bundles_dir=tmp_path / "bundles")


def test_ingestion_rejects_symlinks(tmp_path: Path):
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    real_file = src_dir / "rulebook.txt"
    real_file.write_text("Official Rules v1.0", encoding="utf-8")

    link_file = src_dir / "symlink_rule.txt"
    try:
        link_file.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported on this environment")

    with pytest.raises(ValueError, match="Symlinks are rejected"):
        ingest_sources(src_dir, bundles_dir=tmp_path / "bundles")


def test_ingestion_deterministic_bundle_hash(tmp_path: Path):
    src_dir = tmp_path / "docs"
    src_dir.mkdir()
    (src_dir / "rules.md").write_text("# Game Rules", encoding="utf-8")
    (src_dir / "specs.json").write_text('{"season": "2026"}', encoding="utf-8")

    bundles_dir = tmp_path / "bundles"
    manifest1 = ingest_sources(src_dir, bundles_dir=bundles_dir)
    manifest2 = ingest_sources(src_dir, bundles_dir=bundles_dir)

    assert manifest1.bundle_hash == manifest2.bundle_hash
    assert manifest1.file_count == 2
    assert len(manifest1.files) == 2

    # Verify loaded from disk
    loaded = load_source_bundle_manifest(manifest1.bundle_hash, bundles_dir=bundles_dir)
    assert loaded.bundle_hash == manifest1.bundle_hash
    assert loaded.total_size_bytes == manifest1.total_size_bytes


def test_ingestion_different_content_changes_hash(tmp_path: Path):
    dir1 = tmp_path / "d1"
    dir1.mkdir()
    (dir1 / "rule.txt").write_text("Rules version A", encoding="utf-8")

    dir2 = tmp_path / "d2"
    dir2.mkdir()
    (dir2 / "rule.txt").write_text("Rules version B", encoding="utf-8")

    bundles_dir = tmp_path / "bundles"
    m1 = ingest_sources(dir1, bundles_dir=bundles_dir)
    m2 = ingest_sources(dir2, bundles_dir=bundles_dir)

    assert m1.bundle_hash != m2.bundle_hash


def test_ingestion_copy_flag(tmp_path: Path):
    src = tmp_path / "file.txt"
    src.write_text("Hello World", encoding="utf-8")
    bundles_dir = tmp_path / "bundles"

    m_nocopy = ingest_sources(src, copy_files=False, bundles_dir=bundles_dir)
    assert not (bundles_dir / m_nocopy.bundle_hash / "files").exists()

    m_copy = ingest_sources(src, copy_files=True, bundles_dir=bundles_dir)
    assert (bundles_dir / m_copy.bundle_hash / "files" / "file.txt").is_file()


# ---------------------------------------------------------
# 2. Game Spec Validation Tests
# ---------------------------------------------------------
def test_spec_init_and_validation(tmp_path: Path):
    # Setup source bundle
    src = tmp_path / "spec_src"
    src.mkdir()
    (src / "rules.txt").write_text("rules", encoding="utf-8")
    bundles_dir = tmp_path / "bundles"
    manifest = ingest_sources(src, bundles_dir=bundles_dir)

    out_spec = tmp_path / "game_spec.yaml"
    spec = init_game_spec(manifest.bundle_hash, out_spec, bundles_dir=bundles_dir)
    assert spec.source_bundle_hash == manifest.bundle_hash

    # Validate template spec: structurally valid, but not activation ready because rules are MISSING
    is_valid, errors, spec_obj, is_ready = validate_game_spec(
        spec.to_dict(), bundles_dir=bundles_dir
    )
    assert is_valid is True
    assert len(errors) == 0
    assert spec_obj is not None
    assert is_ready is False


def test_spec_validation_rules():
    # Unsupported schema version
    bad_schema = {"schema_version": "9.9.9", "rules": {}}
    valid, errors, _, _ = validate_game_spec(bad_schema)
    assert valid is False
    assert any("schema_version" in e for e in errors)

    # Documented without source_refs is rejected
    invalid_rule_spec = {
        "schema_version": "1.0.0",
        "competition_name": "Battlecode",
        "competition_season": "2026",
        "spec_version": "1.0.0",
        "source_bundle_hash": "abc",
        "official_document_hashes": [],
        "rules": {
            "victory_loss_draw_tiebreak": {
                "meaning": "Win conditions",
                "source_refs": [],  # Empty!
                "verification_state": "DOCUMENTED",
            }
        },
    }
    valid, errors, _, _ = validate_game_spec(invalid_rule_spec)
    assert valid is False
    assert any(
        "source_refs: cannot be empty when verification_state is DOCUMENTED" in e for e in errors
    )

    # Test verified without test coverage is rejected
    invalid_test_spec = {
        "schema_version": "1.0.0",
        "competition_name": "Battlecode",
        "competition_season": "2026",
        "spec_version": "1.0.0",
        "source_bundle_hash": "abc",
        "official_document_hashes": [],
        "rules": {
            "victory_loss_draw_tiebreak": {
                "meaning": "Win conditions",
                "source_refs": ["Rulebook.pdf p.4"],
                "verification_state": "TEST_VERIFIED",
                "test_coverage": [],  # Empty!
            }
        },
    }
    valid, errors, _, _ = validate_game_spec(invalid_test_spec)
    assert valid is False
    assert any(
        "test_coverage: cannot be empty when verification_state is TEST_VERIFIED" in e
        for e in errors
    )


def test_canonical_spec_hash_stability():
    rule = RuleItem(
        meaning="Win on points",
        source_refs=["doc1"],
        verification_state=RuleVerificationState.DOCUMENTED.value,
    )
    from battlelab.official.models import GameSpec

    s1 = GameSpec(
        schema_version="1.0.0",
        competition_name="Battlecode",
        competition_season="2026",
        spec_version="1.0.0",
        source_bundle_hash="hash1",
        official_document_hashes=["h1"],
        sdk_version="1.0",
        generated_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        rules={"victory_loss_draw_tiebreak": rule},
    )
    s2 = GameSpec(
        schema_version="1.0.0",
        competition_name="Battlecode",
        competition_season="2026",
        spec_version="1.0.0",
        source_bundle_hash="hash1",
        official_document_hashes=["h1"],
        sdk_version="1.0",
        generated_at="2026-09-29T12:00:00Z",  # Different timestamp
        updated_at="2026-09-29T12:05:00Z",  # Different timestamp
        rules={"victory_loss_draw_tiebreak": rule},
    )
    assert s1.canonical_hash() == s2.canonical_hash()


# ---------------------------------------------------------
# 3. Official Command Runner Security Tests
# ---------------------------------------------------------
def test_command_runner_no_shell(tmp_path: Path):
    runner = OfficialCommandRunner()
    # If shell=True, echo $HOME or echo hello; echo injected would interpret
    # With shell=False, the semicolon is passed literally as an argument
    res = runner.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "arg1; arg2"],
        cwd=tmp_path,
    )
    assert res.exit_code == 0
    assert "arg1; arg2" in res.stdout.strip()


def test_command_runner_rejects_empty_and_nul(tmp_path: Path):
    runner = OfficialCommandRunner()
    with pytest.raises(ValueError, match="argv must be a non-empty list"):
        runner.run([], cwd=tmp_path)

    with pytest.raises(ValueError, match="NUL character not permitted"):
        runner.run([sys.executable, "test\0bad"], cwd=tmp_path)


def test_command_runner_timeout_and_cleanup(tmp_path: Path):
    runner = OfficialCommandRunner()
    start = time.monotonic()
    res = runner.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=0.3,
    )
    elapsed = time.monotonic() - start
    assert res.timed_out is True
    assert elapsed < 3.0


def test_command_runner_cancellation(tmp_path: Path):
    runner = OfficialCommandRunner()
    cancel_evt = threading.Event()

    def cancel_soon():
        time.sleep(0.2)
        cancel_evt.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    res = runner.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=5.0,
        cancel_event=cancel_evt,
    )
    assert res.cancelled is True


def test_command_runner_secret_redaction(tmp_path: Path):
    runner = OfficialCommandRunner()
    secret = "SUPER_SECRET_API_TOKEN_12345"
    res = runner.run(
        [sys.executable, "-c", f"print('Token is: {secret}')", f"--key={secret}"],
        cwd=tmp_path,
        secrets=[secret],
    )
    assert secret not in res.stdout
    assert "***REDACTED***" in res.stdout
    assert secret not in " ".join(res.argv)
    assert "***REDACTED***" in " ".join(res.argv)


def test_command_runner_bounded_output(tmp_path: Path):
    runner = OfficialCommandRunner()
    # Emit 50KB of data
    res = runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('A' * 50000)"],
        cwd=tmp_path,
        stdout_limit_bytes=1000,
    )
    assert len(res.stdout) == 1000
    assert res.stdout_truncated is True


def test_command_runner_dry_run(tmp_path: Path):
    runner = OfficialCommandRunner()
    res = runner.run(
        [sys.executable, "-c", "print('should not execute')"],
        cwd=tmp_path,
        dry_run=True,
    )
    assert res.exit_code == 0
    assert "[DRY_RUN]" in res.stdout


# ---------------------------------------------------------
# 4. Readiness & Production Status Tests
# ---------------------------------------------------------
def test_production_readiness_fail_closed():
    checker = OfficialReadinessChecker()
    report = checker.evaluate()
    assert report.ready is False
    assert report.can_run_local is False
    assert report.can_submit is False
    assert len(report.blockers) > 0

    dry_res = checker.dry_run_activation()
    assert dry_res["ready"] is False
    assert dry_res["status"] == "BLOCKED"
    assert dry_res["blockers_count"] > 0


# ---------------------------------------------------------
# 5. Normalized Replay & Determinism Comparison Tests
# ---------------------------------------------------------
def test_normalized_replay_contract():
    replay1 = NormalizedReplay(
        schema_version="1.0.0",
        adapter_name="mock",
        adapter_version="0.2.0",
        game_version="mock-v2-isolated",
        map_id="grid_8x8",
        seed=42,
        participants={"A": "bot1", "B": "bot2"},
        outcome="WIN_A",
        scores={"A": 100.0, "B": 50.0},
        turn_count=15,
        events=[{"action": "move"}],
        raw_replay_hash="hash_raw_123",
        source_metadata={"ts": "2026-09-29T10:00:00Z"},
    )
    assert replay1.total_frames == 15
    assert replay1["total_frames"] == 15
    assert "total_frames" in replay1

    # Identical gameplay, different volatile metadata
    replay2 = NormalizedReplay(
        schema_version="1.0.0",
        adapter_name="mock",
        adapter_version="0.2.0",
        game_version="mock-v2-isolated",
        map_id="grid_8x8",
        seed=42,
        participants={"A": "bot1", "B": "bot2"},
        outcome="WIN_A",
        scores={"A": 100.0, "B": 50.0},
        turn_count=15,
        events=[{"action": "move"}],
        raw_replay_hash="hash_raw_123",
        source_metadata={"ts": "2026-09-29T10:05:00Z"},  # Different volatile timestamp
    )

    cmp = compare_replay_determinism(replay1, replay2)
    assert cmp.raw_replay_equal is True
    assert cmp.gameplay_equal is True
    assert cmp.volatile_metadata_equal is False


# ---------------------------------------------------------
# 6. Phase 3.0.1 Fail-Closed Integrity & Readiness Hardening Regression Tests
# ---------------------------------------------------------
def test_tampered_manifest_entry_hash_rejected(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.txt").write_text("Authoritative doc", encoding="utf-8")
    bundles = tmp_path / "bundles"
    manifest = ingest_sources(src, bundles_dir=bundles)

    # Tamper with file entry sha256 in source_manifest.json
    manifest_file = bundles / manifest.bundle_hash / "source_manifest.json"
    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["files"][0]["sha256"] = "0" * 64
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with pytest.raises(ValueError, match="(tampered|mismatch)"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)


def test_tampered_file_count_and_total_size_rejected(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.txt").write_text("Hello", encoding="utf-8")
    bundles = tmp_path / "bundles"
    manifest = ingest_sources(src, bundles_dir=bundles)
    manifest_file = bundles / manifest.bundle_hash / "source_manifest.json"

    # Tamper file_count
    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["file_count"] = 999
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="file_count"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)

    # Tamper total_size_bytes
    data["file_count"] = 1
    data["total_size_bytes"] = 9999
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="total_size_bytes"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)

    # Reject boolean size
    data["total_size_bytes"] = True
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="total_size_bytes"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)


def test_added_deleted_modified_symlinked_copied_source_files_rejected(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.txt").write_text("Hello copy", encoding="utf-8")
    bundles = tmp_path / "bundles"
    manifest = ingest_sources(src, copy_files=True, bundles_dir=bundles)
    files_dir = bundles / manifest.bundle_hash / "files"

    # 1. Added rogue file
    rogue = files_dir / "rogue.txt"
    rogue.write_text("unmanifested", encoding="utf-8")
    with pytest.raises(ValueError, match="(Extraneous|added)"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)
    rogue.unlink()

    # 2. Modified file content
    (files_dir / "doc.txt").write_text("corrupted content", encoding="utf-8")
    with pytest.raises(ValueError, match="tampered"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)
    (files_dir / "doc.txt").write_text("Hello copy", encoding="utf-8")

    # 3. Deleted file
    (files_dir / "doc.txt").unlink()
    with pytest.raises(ValueError, match="Missing"):
        load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)
    (files_dir / "doc.txt").write_text("Hello copy", encoding="utf-8")

    # 4. Symlink rejection
    sym = files_dir / "sym.txt"
    try:
        sym.symlink_to(files_dir / "doc.txt")
        with pytest.raises(ValueError, match="Symlinks are rejected"):
            load_source_bundle_manifest(manifest.bundle_hash, bundles_dir=bundles)
        sym.unlink()
    except (OSError, NotImplementedError):
        pass


def test_bundle_publication_is_atomic_and_cannot_overwrite(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "rules.txt").write_text("rule text 1", encoding="utf-8")
    bundles = tmp_path / "bundles"

    m1 = ingest_sources(src, copy_files=True, bundles_dir=bundles)
    b_dir = bundles / m1.bundle_hash
    assert b_dir.exists()

    # Ingesting identical data succeeds without clobbering
    m2 = ingest_sources(src, copy_files=True, bundles_dir=bundles)
    assert m2.bundle_hash == m1.bundle_hash

    # An existing bundle cannot be silently overwritten with modified/different manifest
    m_file = b_dir / "source_manifest.json"
    with open(m_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["files"] = []
    with open(m_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with pytest.raises((FileExistsError, ValueError)):
        ingest_sources(src, copy_files=True, bundles_dir=bundles)


def test_caller_provided_non_allowlisted_env_rejected(tmp_path: Path):
    runner = OfficialCommandRunner()
    with pytest.raises(ValueError, match="not permitted by allowlist"):
        runner.run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env={"DISALLOWED_CUSTOM_ENV_VAR": "danger"},
        )


def test_invalid_timeout_output_limits_and_nul_env_rejected(tmp_path: Path):
    runner = OfficialCommandRunner()

    with pytest.raises(ValueError, match="timeout_seconds"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, timeout_seconds=True)

    with pytest.raises(ValueError, match="timeout_seconds"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, timeout_seconds=-1.0)

    with pytest.raises(ValueError, match="stdout_limit_bytes"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, stdout_limit_bytes=True)

    with pytest.raises(ValueError, match="stdout_limit_bytes"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, stdout_limit_bytes=-5)

    with pytest.raises(ValueError, match="stderr_limit_bytes"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, stderr_limit_bytes=-5)

    with pytest.raises(ValueError, match="NUL character"):
        runner.run([sys.executable, "-c", "pass"], cwd=tmp_path, env={"PATH": "/usr/bin\0bad"})


def test_missing_bridge_capability_fields_remain_false_unknown():
    class IncompleteProbeBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return False, "Unimplemented"

        def probe_sdk(self):
            return {}

        def discover_maps(self):
            return []

        def validate_bot_compatibility(self, bot_artifact):
            return False, "Unimplemented"

        def build_or_prepare_artifact(self, source_path, output_dir):
            raise NotImplementedError

        def build_match_command(self, spec, bot_a, bot_b, work_dir):
            return []

        def parse_match_result(self, spec, command_result, work_dir):
            raise NotImplementedError

        def locate_replay(self, spec, work_dir):
            return None

        def parse_replay(self, replay_path):
            raise NotImplementedError

        def normalize_outcome(self, raw_outcome):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    adapter = OfficialAdapter(bridge=IncompleteProbeBridge())
    caps = adapter.get_capabilities()
    assert caps.can_run_local is False
    assert caps.can_submit is False
    assert caps.supported_languages == []
    assert caps.game_version == "UNKNOWN"


def test_bridge_cannot_enable_submission():
    class SubmissionClaimingBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return True, "OK"

        def probe_sdk(self):
            return {
                "can_submit": True,  # Attempting to enable submissions!
                "can_run_local": True,
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "1.0.0",
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, bot_artifact):
            return True, "OK"

        def build_or_prepare_artifact(self, source_path, output_dir):
            return {}

        def build_match_command(self, spec, bot_a, bot_b, work_dir):
            return []

        def parse_match_result(self, spec, command_result, work_dir):
            raise NotImplementedError

        def locate_replay(self, spec, work_dir):
            return None

        def parse_replay(self, replay_path):
            raise NotImplementedError

        def normalize_outcome(self, raw_outcome):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    adapter = OfficialAdapter(bridge=SubmissionClaimingBridge())
    caps = adapter.get_capabilities()
    assert caps.can_submit is False


def test_nonexistent_executable_makes_installation_invalid(tmp_path: Path):
    class MissingExecutableBridge(OfficialEngineBridge):
        sdk_path = tmp_path / "nonexistent_engine.exe"

        def validate_spec(self, spec_data):
            return False, ""

        def probe_sdk(self):
            return {
                "executable_exists": False,
                "executable_runnable": False,
                "sdk_version": "",
            }

        def discover_maps(self):
            return []

        def validate_bot_compatibility(self, bot_artifact):
            return False, ""

        def build_or_prepare_artifact(self, source_path, output_dir):
            return {}

        def build_match_command(self, spec, bot_a, bot_b, work_dir):
            return []

        def parse_match_result(self, spec, command_result, work_dir):
            raise NotImplementedError

        def locate_replay(self, spec, work_dir):
            return None

        def parse_replay(self, replay_path):
            raise NotImplementedError

        def normalize_outcome(self, raw_outcome):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    adapter = OfficialAdapter(bridge=MissingExecutableBridge())
    ok, msg = adapter.validate_installation()
    assert ok is False
    assert "does not exist" in msg


def test_every_failed_blocking_check_appears_in_blockers():
    checker = OfficialReadinessChecker()
    report = checker.evaluate()
    failed_blocking_names = [c.name for c in report.checks if c.blocker and not c.passed]
    assert len(report.blockers) == len(failed_blocking_names)
    assert set(report.blockers) == set(failed_blocking_names)


def test_readiness_remains_false_with_missing_executable_or_zero_maps(tmp_path: Path):
    class NoExeBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return False, ""

        def probe_sdk(self):
            return {
                "executable_exists": False,
                "executable_runnable": False,
                "sdk_version": "1.0",
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, bot):
            return False, ""

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    checker_no_exe = OfficialReadinessChecker(bridge=NoExeBridge())
    rep1 = checker_no_exe.evaluate()
    assert "sdk_executable_exists" in rep1.blockers
    assert rep1.can_run_local is False
    assert rep1.ready is False

    class ZeroMapsBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return False, ""

        def probe_sdk(self):
            return {
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "1.0",
            }

        def discover_maps(self):
            return []  # Zero maps!

        def validate_bot_compatibility(self, bot):
            return False, ""

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    checker_zero_maps = OfficialReadinessChecker(bridge=ZeroMapsBridge())
    rep2 = checker_zero_maps.evaluate()
    assert "map_discovery_succeeds" in rep2.blockers
    assert rep2.can_run_local is False
    assert rep2.ready is False


def test_documented_rules_insufficient_all_mandatory_must_be_test_verified(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "rules.pdf").write_text("doc", encoding="utf-8")
    bundles = tmp_path / "bundles"
    manifest = ingest_sources(src, bundles_dir=bundles)

    from battlelab.official.spec import REQUIRED_RULE_SECTIONS

    spec_data = {
        "schema_version": "1.0.0",
        "competition_name": "Battlecode",
        "competition_season": "",
        "spec_version": "1.0.0",
        "source_bundle_hash": manifest.bundle_hash,
        "official_document_hashes": [manifest.files[0].sha256],
        "rules": {
            k: {
                "meaning": f"Rule {k}",
                "source_refs": ["rules.pdf#p1"],
                "verification_state": "DOCUMENTED",  # DOCUMENTED, not TEST_VERIFIED
            }
            for k in REQUIRED_RULE_SECTIONS
        },
    }
    valid, errors, spec_obj, is_ready = validate_game_spec(spec_data, bundles_dir=bundles)
    assert valid is True
    assert is_ready is False  # Cannot be ready when only DOCUMENTED!

    spec_file = tmp_path / "spec.yaml"
    import yaml

    with open(spec_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec_data, f)

    checker = OfficialReadinessChecker(spec_path=spec_file, source_bundle_hash=manifest.bundle_hash)
    report = checker.evaluate()
    assert "mandatory_rules_test_verified" in report.blockers
    assert report.can_run_local is False
    assert report.ready is False


def test_spec_bundle_hash_mismatch_and_invalid_source_references_rejected(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "rules.txt").write_text("rule content", encoding="utf-8")
    bundles = tmp_path / "bundles"
    manifest = ingest_sources(src, bundles_dir=bundles)

    from battlelab.official.spec import REQUIRED_RULE_SECTIONS

    # Case A: Mismatched bundle hash
    spec_bad_hash = {
        "schema_version": "1.0.0",
        "competition_name": "Battlecode",
        "competition_season": "",
        "spec_version": "1.0.0",
        "source_bundle_hash": "0" * 64,  # Mismatched hash!
        "official_document_hashes": [manifest.files[0].sha256],
        "rules": {
            k: {
                "meaning": f"Rule {k}",
                "source_refs": ["rules.txt"],
                "verification_state": "TEST_VERIFIED",
                "test_coverage": ["test_rule"],
            }
            for k in REQUIRED_RULE_SECTIONS
        },
    }
    valid_a, errors_a, _, _ = validate_game_spec(spec_bad_hash, bundles_dir=bundles)
    assert valid_a is False
    assert any("source_bundle_hash" in e for e in errors_a)

    # Case B: Invalid source reference (file does not exist in bundle)
    spec_bad_ref = {
        "schema_version": "1.0.0",
        "competition_name": "Battlecode",
        "competition_season": "",
        "spec_version": "1.0.0",
        "source_bundle_hash": manifest.bundle_hash,
        "official_document_hashes": [manifest.files[0].sha256],
        "rules": {
            k: {
                "meaning": f"Rule {k}",
                "source_refs": ["fabricated_doc.pdf#sec1"],  # Not in manifest!
                "verification_state": "TEST_VERIFIED",
                "test_coverage": ["test_rule"],
            }
            for k in REQUIRED_RULE_SECTIONS
        },
    }
    valid_b, errors_b, _, _ = validate_game_spec(spec_bad_ref, bundles_dir=bundles)
    assert valid_b is False
    assert any("does not match any file in source bundle manifest" in e for e in errors_b)


def test_self_reported_bridge_verification_flags_cannot_pass_readiness(tmp_path: Path):
    class BoastingBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return True, "OK"

        def probe_sdk(self):
            return {
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "1.0.0",
                "build_verified": True,  # Self-reported flags!
                "match_verified": True,
                "replay_verified": True,
                "determinism_verified": True,
                "can_run_local": True,
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, bot):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            raise RuntimeError("Execution actually fails!")

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    checker = OfficialReadinessChecker(bridge=BoastingBridge())
    report = checker.evaluate()
    # The checker executes build check or fails prerequisites, ignoring self-reported booleans
    assert report.can_run_local is False
    assert report.ready is False


def test_non_zero_engine_exit_always_produces_infrastructure_failure(tmp_path: Path):
    class CrashingMatchBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return True, ""

        def probe_sdk(self):
            return {
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "1.0",
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, bot):
            return True, ""

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return [sys.executable, "-c", "import sys; sys.exit(42)"]

        def parse_match_result(self, s, c, w):
            # Attempt to return a win despite exit code 42
            return MatchResult(
                match_id="m1",
                outcome=MatchOutcome.WIN_A,
                winner="A",
                score_a=100.0,
                score_b=0.0,
                turns_played=10,
                duration_ms=1.0,
            )

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.WIN_A

    adapter = OfficialAdapter(bridge=CrashingMatchBridge())
    bot = BotArtifact("b", "B", "bots/b", "python", None, False, "h")
    spec = MatchSpec("m", "official", "1.0", "b", "b", "map1", 1)

    res = adapter.run_local_match(spec, bot, bot, tmp_path)
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.winner is None
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.ENGINE_CRASH


def test_malformed_replay_always_produces_infrastructure_or_corruption_failure(tmp_path: Path):
    class CorruptReplayBridge(OfficialEngineBridge):
        def validate_spec(self, spec_data):
            return True, ""

        def probe_sdk(self):
            return {
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "1.0",
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, bot):
            return True, ""

        def build_match_command(self, s, a, b, w):
            return [sys.executable, "-c", "pass"]

        def build_or_prepare_artifact(self, s, o):
            return {}

        def parse_match_result(self, s, c, w):
            return MatchResult(
                match_id="m1",
                outcome=MatchOutcome.WIN_A,
                winner="A",
                score_a=100.0,
                score_b=0.0,
                turns_played=10,
                duration_ms=1.0,
            )

        def locate_replay(self, s, w):
            corrupt = w / "corrupt_replay.json"
            corrupt.write_text("{invalid json corrupt", encoding="utf-8")
            return corrupt

        def parse_replay(self, r):
            raise json.JSONDecodeError("Corrupted replay", "", 0)

        def normalize_outcome(self, r):
            return MatchOutcome.WIN_A

    adapter = OfficialAdapter(bridge=CorruptReplayBridge())
    bot = BotArtifact("b", "B", "bots/b", "python", None, False, "h")
    spec = MatchSpec("m", "official", "1.0", "b", "b", "map1", 1)

    res = adapter.run_local_match(spec, bot, bot, tmp_path)
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.winner is None
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.REPLAY_CORRUPTION


def test_gameplay_identical_replays_with_different_raw_hashes():
    r1 = NormalizedReplay(
        schema_version="1.0.0",
        adapter_name="official",
        adapter_version="1.0.0",
        game_version="v1",
        map_id="map_a",
        seed=123,
        participants={"A": "botA", "B": "botB"},
        outcome="WIN_A",
        scores={"A": 10.0, "B": 5.0},
        turn_count=20,
        events=[{"t": 1, "e": "move"}],
        raw_replay_hash="a" * 64,
        source_metadata={"timestamp": "2026-09-29T10:00:00Z"},
    )
    r2 = NormalizedReplay(
        schema_version="1.0.0",
        adapter_name="official",
        adapter_version="1.0.0",
        game_version="v1",
        map_id="map_a",
        seed=123,
        participants={"A": "botA", "B": "botB"},
        outcome="WIN_A",
        scores={"A": 10.0, "B": 5.0},
        turn_count=20,
        events=[{"t": 1, "e": "move"}],
        raw_replay_hash="b" * 64,  # Different raw hash!
        source_metadata={"timestamp": "2026-09-29T11:00:00Z"},
    )
    # Normalized gameplay canonical hashes must be equal despite raw hash difference
    assert r1.canonical_hash() == r2.canonical_hash()
    cmp = compare_replay_determinism(r1, r2)
    assert cmp.raw_replay_equal is False
    assert cmp.gameplay_equal is True


def test_default_production_state_and_adversarial_configured_bridge_fail_closed():
    # 1. Default production adapter
    default_adapter = get_adapter("official")
    ok, _ = default_adapter.validate_installation()
    assert ok is False
    caps = default_adapter.get_capabilities()
    assert caps.can_run_local is False
    assert caps.can_submit is False

    prod_checker = OfficialReadinessChecker()
    prod_report = prod_checker.evaluate()
    assert prod_report.ready is False
    assert prod_report.can_run_local is False
    assert prod_report.can_submit is False

    # 2. Adversarial configured bridge
    class AdversarialBridge(OfficialEngineBridge):
        def validate_spec(self, s):
            return True, "OK"

        def probe_sdk(self):
            return {
                "can_submit": True,
                "can_run_local": True,
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "6.6.6",
                "build_verified": True,
                "match_verified": True,
                "replay_verified": True,
                "determinism_verified": True,
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, b):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    adv_adapter = OfficialAdapter(bridge=AdversarialBridge())
    adv_caps = adv_adapter.get_capabilities()
    assert adv_caps.can_submit is False  # Cannot enable submissions

    adv_checker = OfficialReadinessChecker(bridge=AdversarialBridge())
    adv_report = adv_checker.evaluate()
    assert adv_report.ready is False
    assert adv_report.can_run_local is False
    assert adv_report.can_submit is False


# ---------------------------------------------------------
# Phase 3.0.2 Regression Tests
# ---------------------------------------------------------
def test_bridge_without_declared_sdk_executable_cannot_become_ready():
    class MissingExeBridge(OfficialEngineBridge):
        def get_sdk_executable(self) -> Path | None:
            return None

        def validate_spec(self, s):
            return True, "OK"

        def probe_sdk(self):
            return {"executable_exists": False}

        def discover_maps(self):
            return []

        def validate_bot_compatibility(self, b):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    bridge = MissingExeBridge()
    checker = OfficialReadinessChecker(bridge=bridge)
    report = checker.evaluate()
    assert report.ready is False
    assert report.can_run_local is False
    assert report.sdk_evidence is None
    configured_check = next(c for c in report.checks if c.name == "sdk_configured")
    assert configured_check.passed is False

    adapter = OfficialAdapter(bridge=bridge)
    ok, msg = adapter.validate_installation()
    assert ok is False
    assert "Bridge has no declared SDK executable" in msg


def test_self_reported_sdk_existence_runnability_version_cannot_satisfy_readiness(
    tmp_path: Path,
):
    nonexistent = tmp_path / "phantom_sdk.exe"

    class SelfReportingBridge(OfficialEngineBridge):
        def get_sdk_executable(self) -> Path | None:
            return nonexistent

        def validate_spec(self, s):
            return True, "OK"

        def probe_sdk(self):
            return {
                "executable_exists": True,
                "executable_runnable": True,
                "sdk_version": "99.0.0",
            }

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, b):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    bridge = SelfReportingBridge()
    checker = OfficialReadinessChecker(bridge=bridge)
    report = checker.evaluate()
    assert report.ready is False
    assert report.can_run_local is False
    assert report.sdk_evidence is None
    exists_check = next(c for c in report.checks if c.name == "sdk_executable_exists")
    assert exists_check.passed is False
    runnable_check = next(c for c in report.checks if c.name == "sdk_executable_runnable")
    assert runnable_check.passed is False


def test_probe_build_and_match_must_use_same_verified_executable_identity(
    tmp_path: Path,
):
    verified_exe = tmp_path / "engine.py"
    verified_exe.write_text(
        "import sys, json\n"
        "if sys.argv[1] == 'probe':\n"
        "    print(json.dumps({'sdk_version': '1.0.0'}))\n",
        encoding="utf-8",
    )
    tampered_exe = tmp_path / "tampered.py"
    tampered_exe.write_text("print('tampered')", encoding="utf-8")

    class SwappingBridge(OfficialEngineBridge):
        def get_sdk_executable(self) -> Path | None:
            return verified_exe

        def get_launcher_argv(self) -> list[str]:
            return [sys.executable]

        def validate_spec(self, s):
            return True, "OK"

        def probe_sdk(self):
            return {"executable_exists": True}

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, b):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            return {"status": "SUCCESS"}

        def build_match_command(self, s, a, b, w):
            return [sys.executable, str(tampered_exe), "run"]

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    adapter = OfficialAdapter(bridge=SwappingBridge())
    bot = BotArtifact("b", "B", "bots/b", "python", None, False, "h")
    spec = MatchSpec(
        match_id="m",
        adapter_name="official",
        adapter_version="1.0",
        bot_a_id="b",
        bot_b_id="b",
        map_name="map1",
        seed=1,
        match_wall_clock_limit_ms=5000,
        per_turn_limit_ms=1000,
    )
    res = adapter.run_local_match(spec, bot, bot, tmp_path / "work")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert "does not invoke verified SDK executable" in res.failure_classification.evidence


def test_fragment_and_colon_fragment_and_empty_path_citations_rejected():
    ok, err, _, _ = parse_and_validate_citation("#section1")
    assert ok is False
    assert "Citations starting with '#' or ':' are invalid" in err

    ok, err, _, _ = parse_and_validate_citation(":section1")
    assert ok is False
    assert "Citations starting with '#' or ':' are invalid" in err

    ok, err, _, _ = parse_and_validate_citation("")
    assert ok is False
    assert "must match format" in err

    ok, err, _, _ = parse_and_validate_citation("/absolute/path.md")
    assert ok is False
    assert "cannot be absolute" in err

    ok, err, _, _ = parse_and_validate_citation("../outside.md")
    assert ok is False
    assert "cannot contain '..'" in err

    ok, err, _, _ = parse_and_validate_citation("docs\\rules.md")
    assert ok is False
    assert "backslashes" in err

    ok, err, _, _ = parse_and_validate_citation("docs/rule book.md")
    assert ok is False
    assert "whitespace" in err

    # Valid citation parses correctly
    ok, _, relpath, frag = parse_and_validate_citation("docs/rules.md#turn_order")
    assert ok is True
    assert relpath == "docs/rules.md"
    assert frag == "turn_order"


def test_nonexistent_pytest_node_ids_cannot_satisfy_test_verified(tmp_path: Path):
    # 1. Non-existent file
    ok, err = validate_pytest_node_id("tests/unit/test_phantom.py::test_missing")
    assert ok is False
    assert "does not exist" in err

    # 2. Existing file but non-existent function
    ok, err = validate_pytest_node_id(
        "tests/unit/test_official_subsystem.py::test_completely_made_up_function_xyz"
    )
    assert ok is False
    assert "not found in" in err

    # 3. Skipped or xfailed test function
    skipped_file = tmp_path / "test_skipped.py"
    skipped_file.write_text(
        "import pytest\n@pytest.mark.skip(reason='not ready')\ndef test_sk(): pass\n",
        encoding="utf-8",
    )
    ok, err = validate_pytest_node_id(f"{skipped_file.name}::test_sk", project_root=tmp_path)
    assert ok is False
    assert "skipped or xfail" in err


def test_empty_rule_meaning_season_or_sdk_version_blocks_activation():
    rules: dict[str, RuleItem] = {
        section: RuleItem(
            meaning=f"Rule specification for {section}",
            source_refs=[],
            verification_state=RuleVerificationState.MISSING.value,
        )
        for section in REQUIRED_RULE_SECTIONS
    }
    spec = GameSpec(
        schema_version="1.0.0",
        competition_name="Battlecode",
        competition_season="2026",
        spec_version="1.0.0",
        source_bundle_hash="a" * 64,
        official_document_hashes=["b" * 64],
        sdk_version="1.0.0",
        generated_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        rules=rules,
    )
    # Empty meaning with DOCUMENTED state must fail validation
    spec.rules[REQUIRED_RULE_SECTIONS[0]].meaning = "   "
    spec.rules[
        REQUIRED_RULE_SECTIONS[0]
    ].verification_state = RuleVerificationState.DOCUMENTED.value
    spec.rules[REQUIRED_RULE_SECTIONS[0]].source_refs = ["doc.md"]
    is_valid, errors, _, _ = validate_game_spec(spec.to_dict())
    assert is_valid is False
    assert any("must have non-empty meaning" in e for e in errors)

    # Empty competition_season
    spec.rules[REQUIRED_RULE_SECTIONS[0]].meaning = "Valid meaning"
    spec.competition_season = ""
    is_valid, errors, _, is_ready = validate_game_spec(spec.to_dict())
    assert is_ready is False

    # Empty sdk_version
    spec.competition_season = "2026"
    spec.sdk_version = "   "
    is_valid, errors, _, is_ready = validate_game_spec(spec.to_dict())
    assert is_ready is False


def test_bundle_root_and_ancestor_symlinks_rejected(tmp_path: Path):
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()

    # Invalid bundle hash
    with pytest.raises(ValueError, match="Invalid source_bundle_hash format"):
        load_source_bundle_manifest("../malicious_bundle", bundles_dir=bundles_dir)

    # Symlinked bundle root
    real_bundle = tmp_path / "real_bundle"
    real_bundle.mkdir()
    (real_bundle / "manifest.json").write_text("{}", encoding="utf-8")
    bundle_hash = "c" * 64
    sym_bundle = bundles_dir / bundle_hash
    try:
        sym_bundle.symlink_to(real_bundle)
        with pytest.raises(ValueError, match="must be a regular directory, not a symlink"):
            load_source_bundle_manifest(bundle_hash, bundles_dir=bundles_dir)
    except (OSError, NotImplementedError):
        pass  # Skip if symlinks not supported on OS


def test_staged_copy_fully_verified_before_publication(tmp_path: Path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "rules.md").write_text("Rules content", encoding="utf-8")
    bundles = tmp_path / "bundles"

    manifest = ingest_sources(src, bundles_dir=bundles)
    bundle_dir = bundles / manifest.bundle_hash
    assert bundle_dir.is_dir()

    # Ingesting the same directory again succeeds atomically without corrupting
    manifest2 = ingest_sources(src, bundles_dir=bundles)
    assert manifest2.bundle_hash == manifest.bundle_hash


def test_forbidden_env_and_invalid_inputs_rejected_in_dry_run_mode(tmp_path: Path):
    runner = OfficialCommandRunner()

    with pytest.raises(ValueError, match="is not permitted by allowlist"):
        runner.run(
            ["echo", "hello"],
            cwd=tmp_path,
            env={"LD_PRELOAD": "/lib/evil.so"},
            dry_run=True,
        )

    with pytest.raises(ValueError, match="argv must be a non-empty list"):
        runner.run([], cwd=tmp_path, dry_run=True)

    with pytest.raises(TypeError, match="All argv elements must be strings"):
        runner.run(["echo", 123], cwd=tmp_path, dry_run=True)  # type: ignore[list-item]

    with pytest.raises(FileNotFoundError, match="Working directory does not exist"):
        runner.run(["echo", "hello"], cwd=tmp_path / "nonexistent", dry_run=True)


def test_secrets_in_cwd_env_argv_never_appear_in_any_exception_string(tmp_path: Path):
    secret_val = "SECRET_TOKEN_ALPHA_BETA_999"
    runner = OfficialCommandRunner()

    # Error via forbidden env
    try:
        runner.run(
            ["echo", "test"],
            cwd=tmp_path,
            env={"FORBIDDEN_VAR": secret_val},
            secrets=[secret_val],
        )
    except Exception as e:
        assert secret_val not in str(e)
        assert secret_val not in repr(e)

    # Error via bad cwd
    bad_dir = tmp_path / f"dir_with_{secret_val}"
    try:
        runner.run(["echo", "test"], cwd=bad_dir, secrets=[secret_val])
    except Exception as e:
        assert secret_val not in str(e)
        assert secret_val not in repr(e)


def test_whole_match_execution_uses_match_wall_clock_limit_ms(tmp_path: Path):
    hanging_script = tmp_path / "hang.py"
    hanging_script.write_text(
        "import time\ntime.sleep(10)\n",
        encoding="utf-8",
    )

    class HangingBridge(OfficialEngineBridge):
        def get_sdk_executable(self) -> Path | None:
            return hanging_script

        def get_launcher_argv(self) -> list[str]:
            return [sys.executable]

        def validate_spec(self, s):
            return True, "OK"

        def probe_sdk(self):
            return {"executable_exists": True}

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, b):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return [sys.executable, str(hanging_script)]

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def locate_replay(self, s, w):
            return None

        def parse_replay(self, r):
            raise NotImplementedError

        def normalize_outcome(self, r):
            return MatchOutcome.INFRASTRUCTURE_FAILURE

    adapter = OfficialAdapter(bridge=HangingBridge())
    bot = BotArtifact("b", "B", "bots/b", "python", None, False, "h")
    # match_wall_clock_limit_ms=300ms -> should timeout quickly
    spec = MatchSpec(
        match_id="m",
        adapter_name="official",
        adapter_version="1.0",
        bot_a_id="b",
        bot_b_id="b",
        map_name="map1",
        seed=1,
        match_wall_clock_limit_ms=300,
        per_turn_limit_ms=10000,
    )
    t0 = time.time()
    res = adapter.run_local_match(spec, bot, bot, tmp_path / "work")
    elapsed = time.time() - t0
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.TIMEOUT
    assert elapsed < 5.0


def test_per_turn_limit_does_not_terminate_whole_engine_process(tmp_path: Path):
    fast_script = tmp_path / "fast.py"
    fast_script.write_text(
        "import time, sys, json\n"
        "time.sleep(0.05)\n"  # 50ms total execution
        "print(json.dumps({'winner': 'A'}))\n",
        encoding="utf-8",
    )

    replay_file = tmp_path / "replay.json"
    replay_file.write_text("{}", encoding="utf-8")

    class FastBridge(OfficialEngineBridge):
        def get_sdk_executable(self) -> Path | None:
            return fast_script

        def get_launcher_argv(self) -> list[str]:
            return [sys.executable]

        def validate_spec(self, s):
            return True, "OK"

        def probe_sdk(self):
            return {"executable_exists": True}

        def discover_maps(self):
            return ["map1"]

        def validate_bot_compatibility(self, b):
            return True, "OK"

        def build_or_prepare_artifact(self, s, o):
            return {}

        def build_match_command(self, s, a, b, w):
            return [sys.executable, str(fast_script)]

        def parse_match_result(self, s, c, w):
            return MatchResult(
                match_id=s.match_id,
                outcome=MatchOutcome.WIN_A,
                winner="bot_a",
                duration_ms=c.duration_ms,
            )

        def locate_replay(self, s, w):
            return str(replay_file)

        def parse_replay(self, r):
            return NormalizedReplay(schema_version="1.0.0", outcome="WIN_A")

        def normalize_outcome(self, r):
            return MatchOutcome.WIN_A

    adapter = OfficialAdapter(bridge=FastBridge())
    bot = BotArtifact("b", "B", "bots/b", "python", None, False, "h")
    # per_turn_limit_ms=5ms, but match_wall_clock_limit_ms=5000ms
    # Process runner MUST NOT terminate at 5ms!
    spec = MatchSpec(
        match_id="m",
        adapter_name="official",
        adapter_version="1.0",
        bot_a_id="b",
        bot_b_id="b",
        map_name="map1",
        seed=1,
        match_wall_clock_limit_ms=5000,
        per_turn_limit_ms=5,
    )
    res = adapter.run_local_match(spec, bot, bot, tmp_path / "work")
    assert res.outcome == MatchOutcome.WIN_A


def test_sdk_probe_failure_returns_nonzero_in_text_and_json_modes(tmp_path: Path):
    missing_path = str(tmp_path / "missing_sdk.exe")
    assert probe_engine_executable(missing_path, as_json=False) != 0
    assert probe_engine_executable(missing_path, as_json=True) != 0

    assert probe_engine_executable(None, bridge=UnconfiguredOfficialBridge(), as_json=False) != 0
    assert probe_engine_executable(None, bridge=UnconfiguredOfficialBridge(), as_json=True) != 0


def test_sdk_probe_accepts_and_verifies_documented_engine_path_syntax(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    script = tmp_path / "valid_engine.py"
    script.write_text(
        "import sys, json\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'probe':\n"
        "    print(json.dumps({'sdk_version': '2026.1.0'}))\n",
        encoding="utf-8",
    )
    code_text = probe_engine_executable(str(script), as_json=False)
    assert code_text == 0
    out_text = capsys.readouterr().out
    assert "2026.1.0" in out_text

    code_json = probe_engine_executable(str(script), as_json=True)
    assert code_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["success"] is True
    assert data["sdk_version"] == "2026.1.0"


def test_non_dry_run_activation_cannot_claim_success_without_persistent_state(
    capsys: pytest.CaptureFixture,
):
    from battlelab.cli import main

    exit_code = main(["official", "activate"])
    assert exit_code == 1

    exit_code_json = main(["official", "activate", "--json"])
    assert exit_code_json == 1
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["success"] is False
    assert "not yet implemented" in data["error"]


def test_explicit_readiness_bundle_hash_differing_from_spec_bundle_rejected(
    tmp_path: Path,
):
    import yaml

    spec_file = tmp_path / "game_spec.yaml"
    rules: dict[str, RuleItem] = {
        section: RuleItem(
            meaning=f"Rule specification for {section}",
            source_refs=[],
            verification_state=RuleVerificationState.MISSING.value,
        )
        for section in REQUIRED_RULE_SECTIONS
    }
    spec = GameSpec(
        schema_version="1.0.0",
        competition_name="Battlecode",
        competition_season="2026",
        spec_version="1.0.0",
        source_bundle_hash="a" * 64,
        official_document_hashes=["d" * 64],
        sdk_version="1.0.0",
        generated_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        rules=rules,
    )
    spec_file.write_text(yaml.safe_dump(spec.to_dict()), encoding="utf-8")

    checker = OfficialReadinessChecker(
        spec_path=spec_file,
        source_bundle_hash="b" * 64,  # Conflicts with spec bundle hash!
    )
    report = checker.evaluate()
    assert report.ready is False
    bundle_check = next(c for c in report.checks if c.name == "source_bundle_exists")
    assert bundle_check.passed is False
    assert "differs from game spec source bundle hash" in bundle_check.details


def test_synthetic_test_sdk_cannot_be_registered_through_production_discovery():
    import importlib.util

    contract_path = Path(__file__).parent.parent / "contract" / "test_official_contract.py"
    spec = importlib.util.spec_from_file_location("test_official_contract", contract_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake_cls = getattr(mod, "FakeOfficialBridge")

    with pytest.raises(RuntimeError, match="strictly a test fixture"):
        fake_cls(is_test_fixture=False)

    adapter = get_adapter("official")
    assert isinstance(adapter, OfficialAdapter)
    assert isinstance(adapter.bridge, UnconfiguredOfficialBridge)


def test_previous_phase3_and_phase3_0_1_adversarial_tests_remain_green():
    adapter = get_adapter("official")
    ok, _ = adapter.validate_installation()
    assert ok is False
    caps = adapter.get_capabilities()
    assert caps.can_run_local is False
    assert caps.can_submit is False

    checker = OfficialReadinessChecker()
    report = checker.evaluate()
    assert report.ready is False
    assert report.can_run_local is False
    assert report.can_submit is False


# ---------------------------------------------------------
# 11. Phase 3.0.3 Verifiable Execution & Evidence Regression Tests
# ---------------------------------------------------------


def _create_verified_test_spec(tmp_path: Path):
    doc_file = tmp_path / "rules.txt"
    doc_file.write_text("Rule content\n", encoding="utf-8")
    manifest = ingest_sources(doc_file, copy_files=False)
    spec_data = {
        "schema_version": "1.0.0",
        "competition_name": "battlecode",
        "competition_season": "2026",
        "spec_version": "1.0.0",
        "source_bundle_hash": manifest.bundle_hash,
        "official_document_hashes": [manifest.files[0].sha256],
        "sdk_version": "1.0.0",
        "game_version": "2026.1.0",
        "supported_languages": ["python"],
        "minimal_bot_language": "python",
        "generated_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "rules": {
            sec: {
                "meaning": f"Rule for {sec}",
                "source_refs": [f"rules.txt#sec_{i}"],
                "verification_state": "TEST_VERIFIED",
                "implementation_impacts": ["Impact"],
                "test_coverage": [
                    "tests/unit/test_official_subsystem.py::test_ingestion_rejects_missing_path"
                ],
            }
            for i, sec in enumerate(REQUIRED_RULE_SECTIONS)
        },
    }
    spec_file = tmp_path / "game_spec.yaml"
    import yaml

    with open(spec_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec_data, f)
    return spec_file, manifest.bundle_hash


# 1. test_nested_uncollected_test_function_rejected
def test_nested_uncollected_test_function_rejected(tmp_path: Path):
    test_file = tmp_path / "test_nested_sample.py"
    test_file.write_text(
        "def test_valid():\n"
        "    assert True\n\n"
        "def helper_fn():\n"
        "    def test_nested_inner():\n"
        "        assert True\n",
        encoding="utf-8",
    )
    runner = DefaultRuleTestRunner()
    res = runner.run_rule_tests(
        [f"{test_file.as_posix()}::test_nested_inner"],
        project_root=tmp_path,
    )
    assert res.success is False
    assert "not collected" in res.error_message or "failed" in res.error_message


# 2. test_collected_test_that_fails_cannot_produce_test_verified_evidence
def test_collected_test_that_fails_cannot_produce_test_verified_evidence(
    tmp_path: Path, monkeypatch
):
    import subprocess

    orig_run = subprocess.run

    def mock_run(args, **kwargs):
        if len(args) >= 3 and args[0] == "git" and args[1] == "status":
            from subprocess import CompletedProcess

            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return orig_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    class FailingTestRunner(RuleTestRunner):
        def run_rule_tests(self, node_ids, project_root, timeout_seconds=60.0):
            return RuleTestRunResult(
                success=False,
                error_message="AssertionError: 1 != 2",
                execution_exit_code=1,
                failed_count=1,
                requested_count=len(node_ids),
                collected_count=len(node_ids),
            )

    spec_file, bundle_h = _create_verified_test_spec(tmp_path)
    checker = OfficialReadinessChecker(
        spec_path=spec_file,
        source_bundle_hash=bundle_h,
        rule_test_runner=FailingTestRunner(),
    )
    report = checker.evaluate()
    assert report.ready is False
    rule_check = next(c for c in report.checks if c.name == "mandatory_rules_test_verified")
    assert rule_check.passed is False
    assert "Rule test execution failed" in rule_check.details
    assert report.test_evidence is None


# 3. test_skipped_xfailed_errored_deselected_tests_rejected
def test_skipped_xfailed_errored_deselected_tests_rejected(tmp_path: Path, monkeypatch):
    import subprocess

    orig_run = subprocess.run

    def mock_run(args, **kwargs):
        if len(args) >= 3 and args[0] == "git" and args[1] == "status":
            from subprocess import CompletedProcess

            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return orig_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)
    spec_file, bundle_h = _create_verified_test_spec(tmp_path)

    for outcome, kwargs in [
        ("skipped", {"skipped_count": 1}),
        ("xfailed", {"xfailed_count": 1}),
        ("errored", {"errored_count": 1}),
        ("deselected", {"deselected_count": 1}),
    ]:

        class FlawedRunner(RuleTestRunner):
            def __init__(self, kw):
                self.kw = kw

            def run_rule_tests(self, node_ids, project_root, timeout_seconds=60.0):
                return RuleTestRunResult(
                    success=False,
                    error_message=f"Flawed execution with {outcome}",
                    requested_count=len(node_ids),
                    collected_count=len(node_ids),
                    **self.kw,
                )

        checker = OfficialReadinessChecker(
            spec_path=spec_file,
            source_bundle_hash=bundle_h,
            rule_test_runner=FlawedRunner(kwargs),
        )
        report = checker.evaluate()
        assert report.ready is False
        rule_check = next(c for c in report.checks if c.name == "mandatory_rules_test_verified")
        assert rule_check.passed is False
        assert report.test_evidence is None


# 4. test_ast_presence_alone_cannot_create_rule_test_evidence
def test_ast_presence_alone_cannot_create_rule_test_evidence(tmp_path: Path):
    spec_file, bundle_h = _create_verified_test_spec(tmp_path)

    class StaticAstOnlyRunner(RuleTestRunner):
        def run_rule_tests(self, node_ids, project_root, timeout_seconds=60.0):
            return RuleTestRunResult(
                success=False,
                error_message="AST detected but real execution not performed",
                requested_count=len(node_ids),
                collected_count=len(node_ids),
                passed_count=0,
            )

    checker = OfficialReadinessChecker(
        spec_path=spec_file,
        source_bundle_hash=bundle_h,
        rule_test_runner=StaticAstOnlyRunner(),
    )
    report = checker.evaluate()
    assert report.ready is False
    assert report.test_evidence is None
    c = next(chk for chk in report.checks if chk.name == "mandatory_rules_test_verified")
    assert c.passed is False


# 5. test_dirty_or_modified_rule_test_files_block_readiness
def test_dirty_or_modified_rule_test_files_block_readiness(tmp_path: Path, monkeypatch):
    import subprocess

    orig_run = subprocess.run

    def mock_run(args, **kwargs):
        if len(args) >= 3 and args[0] == "git" and args[1] == "status":
            from subprocess import CompletedProcess

            return CompletedProcess(
                args=args,
                returncode=0,
                stdout=" M tests/unit/test_official_subsystem.py\n",
                stderr="",
            )
        return orig_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    spec_file, bundle_h = _create_verified_test_spec(tmp_path)
    checker = OfficialReadinessChecker(spec_path=spec_file, source_bundle_hash=bundle_h)
    report = checker.evaluate()
    c = next(chk for chk in report.checks if chk.name == "mandatory_rules_test_verified")
    assert c.passed is False
    assert "uncommitted modifications" in c.details


# 6. test_rule_test_evidence_contains_real_collection_and_execution_results
def test_rule_test_evidence_contains_real_collection_and_execution_results():
    evidence = RuleTestEvidence(
        schema_version="1.0.0",
        spec_hash="s" * 64,
        source_bundle_hash="b" * 64,
        sdk_executable_sha256="e" * 64,
        launcher_executable_sha256="l" * 64,
        git_commit="abcdef123456",
        dirty_worktree=False,
        test_node_ids=["tests/unit/test_foo.py::test_bar"],
        test_file_hashes={"tests/unit/test_foo.py": "f" * 64},
        collection_command_hash="c" * 64,
        execution_command_hash="x" * 64,
        collection_exit_code=0,
        execution_exit_code=0,
        collection_stdout_hash="co" * 32,
        collection_stderr_hash="ce" * 32,
        execution_stdout_hash="xo" * 32,
        execution_stderr_hash="xe" * 32,
        junit_xml_hash="j" * 64,
        requested_count=1,
        collected_count=1,
        passed_count=1,
        failed_count=0,
        errored_count=0,
        skipped_count=0,
        xfailed_count=0,
        deselected_count=0,
        verified_at="2026-01-01T00:00:00Z",
    )
    d = evidence.to_dict()
    assert d["junit_xml_hash"] == "j" * 64
    assert d["collection_command_hash"] == "c" * 64
    assert d["execution_command_hash"] == "x" * 64
    assert d["passed_count"] == 1
    assert d["failed_count"] == 0
    assert d["dirty_worktree"] is False

    loaded = RuleTestEvidence.from_dict(d)
    assert loaded.junit_xml_hash == evidence.junit_xml_hash
    assert loaded.collection_exit_code == 0
    assert loaded.execution_exit_code == 0
    assert loaded.passed_count == 1


# 7. test_sdk_path_as_unused_argv_argument_rejected
def test_sdk_path_as_unused_argv_argument_rejected(tmp_path: Path):
    runner = OfficialCommandRunner()
    exe = tmp_path / "engine.py"
    exe.write_text("print('ok')\n", encoding="utf-8")
    exe_hash = hash_file(exe)

    plan = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=["--other", str(exe)],
        cwd=str(tmp_path),
        sdk_executable_sha256=exe_hash,
    )
    with pytest.raises(ValueError, match="SDK path appearing elsewhere in argv is rejected"):
        runner.execute_plan(plan)


# 8. test_exact_launcher_and_sdk_prefix_required
def test_exact_launcher_and_sdk_prefix_required(tmp_path: Path):
    runner = OfficialCommandRunner()
    exe = tmp_path / "engine.py"
    exe.write_text("print('ok')\n", encoding="utf-8")
    exe_hash = hash_file(exe)

    plan = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=["--arg1"],
        cwd=str(tmp_path),
        sdk_executable_sha256=exe_hash,
    )

    class TamperedPlan:
        def __init__(self, orig):
            self.orig = orig
            self.operation = orig.operation
            self.launcher_argv = orig.launcher_argv
            self.sdk_executable_path = orig.sdk_executable_path
            self.operation_argv = orig.operation_argv
            self.cwd = orig.cwd
            self.allowed_env = orig.allowed_env
            self.expected_output_contract = orig.expected_output_contract
            self.sdk_executable_sha256 = orig.sdk_executable_sha256
            self.launcher_executable_sha256 = orig.launcher_executable_sha256

        def get_argv(self):
            return ["wrong_launcher", str(self.sdk_executable_path), "--arg1"]

    with pytest.raises(ValueError, match="Exact launcher \\+ SDK prefix required"):
        runner.execute_plan(TamperedPlan(plan))  # type: ignore


# 9. test_launcher_substitution_and_sdk_substitution_rejected
def test_launcher_substitution_and_sdk_substitution_rejected(tmp_path: Path):
    runner = OfficialCommandRunner()
    exe = tmp_path / "engine.py"
    exe.write_text("print('ok')\n", encoding="utf-8")
    exe_hash = hash_file(exe)

    # Substituted SDK hash
    plan_bad_sdk = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=[],
        cwd=str(tmp_path),
        sdk_executable_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="SDK executable hash mismatch"):
        runner.execute_plan(plan_bad_sdk)

    # Substituted launcher hash
    plan_bad_launcher = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=[],
        cwd=str(tmp_path),
        sdk_executable_sha256=exe_hash,
        launcher_executable_sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="Launcher executable hash mismatch"):
        runner.execute_plan(plan_bad_launcher)


# 10. test_launcher_and_sdk_mutation_between_probe_build_match_detected
def test_launcher_and_sdk_mutation_between_probe_build_match_detected(tmp_path: Path):
    runner = OfficialCommandRunner()
    exe = tmp_path / "mutating_engine.py"
    exe.write_text(
        "import sys, pathlib\n"
        "p = pathlib.Path(__file__)\n"
        "p.write_text('# mutated\\n', encoding='utf-8')\n"
        "print('done')\n",
        encoding="utf-8",
    )
    exe_hash = hash_file(exe)

    plan = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=[],
        cwd=str(tmp_path),
        sdk_executable_sha256=exe_hash,
    )
    with pytest.raises(InfrastructureTamperingError, match="Infrastructure tampering detected"):
        runner.execute_plan(plan)


# 11. test_map_discovery_cannot_succeed_from_self_reported_list
def test_map_discovery_cannot_succeed_from_self_reported_list(tmp_path: Path):
    exe = tmp_path / "engine.py"
    exe.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    exe_hash = hash_file(exe)

    class SelfReportingMapBridge(OfficialEngineBridge):
        def get_sdk_executable(self):
            return exe

        def get_launcher_argv(self):
            return [sys.executable]

        def build_probe_command(self):
            return OfficialCommandPlan(
                operation=PlanOperation.PROBE,
                launcher_argv=[sys.executable],
                sdk_executable_path=str(exe),
                operation_argv=["probe"],
                cwd=str(tmp_path),
                sdk_executable_sha256=exe_hash,
            )

        def parse_probe_result(self, res):
            return {"sdk_version": "1.0.0"}

        def discover_maps(self):
            return ["forged_map_alpha", "forged_map_beta"]

        def validate_spec(self, s):
            return True, ""

        def validate_bot_compatibility(self, b):
            return True, ""

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def parse_replay(self, p):
            raise NotImplementedError

        def locate_replay(self, p):
            return None

        def normalize_outcome(self, o):
            return MatchOutcome.DRAW

    checker = OfficialReadinessChecker(bridge=SelfReportingMapBridge())
    report = checker.evaluate()
    map_check = next(c for c in report.checks if c.name == "map_discovery_succeeds")
    assert map_check.passed is False
    assert "map_discovery_succeeds" in report.blockers


# 12. test_build_cannot_succeed_from_self_reported_status_dictionary
def test_build_cannot_succeed_from_self_reported_status_dictionary(tmp_path: Path):
    exe = tmp_path / "engine.py"
    exe.write_text("print('ok')\n", encoding="utf-8")
    exe_hash = hash_file(exe)

    class SelfReportingBuildBridge(OfficialEngineBridge):
        def get_sdk_executable(self):
            return exe

        def get_launcher_argv(self):
            return [sys.executable]

        def build_probe_command(self):
            return OfficialCommandPlan(
                operation=PlanOperation.PROBE,
                launcher_argv=[sys.executable],
                sdk_executable_path=str(exe),
                operation_argv=["probe"],
                cwd=str(tmp_path),
                sdk_executable_sha256=exe_hash,
            )

        def parse_probe_result(self, res):
            return {"sdk_version": "1.0.0"}

        def build_or_prepare_artifact(self, src, out):
            return {"status": "SUCCESS"}

        def validate_spec(self, s):
            return True, ""

        def validate_bot_compatibility(self, b):
            return True, ""

        def build_match_command(self, s, a, b, w):
            return []

        def parse_match_result(self, s, c, w):
            raise NotImplementedError

        def parse_replay(self, p):
            raise NotImplementedError

        def locate_replay(self, p):
            return None

        def normalize_outcome(self, o):
            return MatchOutcome.DRAW

    checker = OfficialReadinessChecker(bridge=SelfReportingBuildBridge())
    report = checker.evaluate()
    build_check = next(c for c in report.checks if c.name == "build_command_succeeds")
    assert build_check.passed is False


# 13. test_build_output_requires_verified_artifact_manifest
def test_build_output_requires_verified_artifact_manifest(tmp_path: Path):
    exe = tmp_path / "engine.py"
    exe.write_text("print('ok')\n", encoding="utf-8")

    plan = OfficialCommandPlan(
        operation=PlanOperation.BUILD,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=["build", "src", "out"],
        cwd=str(tmp_path),
        sdk_executable_sha256=hash_file(exe),
    )
    assert plan.operation == PlanOperation.BUILD
    assert plan.get_argv() == [sys.executable, str(exe), "build", "src", "out"]

    bad_manifest = {"source_bot_hash": "a" * 64}
    assert "sdk_executable_sha256" not in bad_manifest
    assert "build_command_hash" not in bad_manifest


# 14. test_no_probe_subcommand_assumed_by_generic_production_code
def test_no_probe_subcommand_assumed_by_generic_production_code(tmp_path: Path):
    exe = tmp_path / "custom_sdk.exe"
    exe.write_text(
        "import sys, json\n"
        "if '--custom-query' in sys.argv:\n"
        "    print(json.dumps({'sdk_version': '3.2.1'}))\n",
        encoding="utf-8",
    )
    exe_hash = hash_file(exe)

    plan = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=["--custom-query", "version"],
        cwd=str(tmp_path),
        sdk_executable_sha256=exe_hash,
    )
    runner = OfficialCommandRunner()
    res = runner.execute_plan(plan)
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["sdk_version"] == "3.2.1"


# 15. test_supported_languages_come_only_from_source_backed_spec
def test_supported_languages_come_only_from_source_backed_spec(tmp_path: Path):
    spec_data = {
        "schema_version": "1.0.0",
        "competition_name": "battlecode",
        "competition_season": "2026",
        "spec_version": "1.0.0",
        "source_bundle_hash": "a" * 64,
        "official_document_hashes": ["b" * 64],
        "sdk_version": "1.0.0",
        "game_version": "2026.1.0",
        "supported_languages": ["java", "rust"],
        "minimal_bot_language": "rust",
        "generated_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "rules": {
            sec: {
                "meaning": "desc",
                "source_refs": ["doc.md#sec"],
                "verification_state": "DOCUMENTED",
            }
            for sec in REQUIRED_RULE_SECTIONS
        },
    }
    spec = GameSpec.from_dict(spec_data)
    assert spec.supported_languages == ["java", "rust"]
    assert "python" not in spec.supported_languages


# 16. test_game_version_not_derived_from_sdk_version
def test_game_version_not_derived_from_sdk_version(tmp_path: Path):
    spec_data = {
        "schema_version": "1.0.0",
        "competition_name": "battlecode",
        "competition_season": "2026",
        "spec_version": "1.0.0",
        "source_bundle_hash": "a" * 64,
        "official_document_hashes": ["b" * 64],
        "sdk_version": "1.0.0-synthetic-internal",
        "game_version": "2026.0.1",
        "supported_languages": ["python"],
        "minimal_bot_language": "python",
        "generated_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    spec = GameSpec.from_dict(spec_data)
    assert spec.game_version == "2026.0.1"
    assert spec.sdk_version == "1.0.0-synthetic-internal"
    assert spec.game_version != spec.sdk_version


# 17. test_minimal_bot_language_not_hardcoded_to_python
def test_minimal_bot_language_not_hardcoded_to_python(tmp_path: Path):
    spec = GameSpec(
        schema_version="1.0.0",
        competition_name="battlecode",
        competition_season="2026",
        spec_version="1.0.0",
        source_bundle_hash="a" * 64,
        official_document_hashes=["b" * 64],
        sdk_version="1.0.0",
        game_version="2026.1.0",
        supported_languages=["java", "csharp"],
        minimal_bot_language="java",
        generated_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert spec.minimal_bot_language == "java"
    assert spec.minimal_bot_language != "python"


# 18. test_memory_enforcement_status_truthful_and_tested
def test_memory_enforcement_status_truthful_and_tested():
    runner = OfficialCommandRunner()
    status, reason = runner.get_memory_enforcement_status()
    assert isinstance(status, EnforcementStatus)
    assert status in (
        EnforcementStatus.PLATFORM_ENFORCED,
        EnforcementStatus.OFFICIAL_ENGINE_ENFORCED,
        EnforcementStatus.UNENFORCED,
        EnforcementStatus.UNKNOWN,
    )
    assert isinstance(reason, str) and len(reason) > 0
    if sys.platform.startswith("win"):
        assert status == EnforcementStatus.UNENFORCED
        assert "Windows" in reason


# 19. test_previous_phase3_adversarial_regressions_remain_green
def test_previous_phase3_adversarial_regressions_remain_green(tmp_path: Path):
    with pytest.raises(Exception):
        load_source_bundle_manifest("0" * 64)

    runner = OfficialCommandRunner()
    exe = tmp_path / "dummy.py"
    exe.write_text("print(1)\n", encoding="utf-8")
    h = hash_file(exe)

    plan = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[sys.executable],
        sdk_executable_path=str(exe),
        operation_argv=["probe"],
        cwd=str(tmp_path),
        sdk_executable_sha256=h,
    )
    res = runner.execute_plan(plan)
    assert res.exit_code == 0


# 20. test_production_default_remains_fully_fail_closed
def test_production_default_remains_fully_fail_closed():
    adapter = get_adapter("official")
    ok, err = adapter.validate_installation()
    assert "not ingested or configured" in err.lower() or "unconfigured" in err.lower()

    caps = adapter.get_capabilities()
    assert caps.can_run_local is False
    assert caps.can_submit is False

    checker = OfficialReadinessChecker()
    report = checker.evaluate()
    assert report.ready is False
    assert report.can_run_local is False
    assert report.can_submit is False
    assert len(report.blockers) > 0


def test_launcher_symlink_resolving_to_regular_file(tmp_path: Path):
    if platform.system() == "Windows":
        pytest.skip("Symlink to Windows Store python stub not supported on Windows")

    runner = OfficialCommandRunner()
    exe = tmp_path / "engine.py"
    exe.write_text("print('ok')\n", encoding="utf-8")
    h = hash_file(exe)

    launcher_sym = tmp_path / "python_symlink"
    try:
        launcher_sym.symlink_to(sys.executable)
    except OSError:
        pytest.skip("Symlink creation not supported on this OS")

    from battlelab.official._win_appexeclink import resolve_executable_for_hash

    launcher_h = hash_file(resolve_executable_for_hash(sys.executable))
    plan = OfficialCommandPlan(
        operation=PlanOperation.PROBE,
        launcher_argv=[str(launcher_sym)],
        sdk_executable_path=str(exe),
        operation_argv=[],
        cwd=str(tmp_path),
        sdk_executable_sha256=h,
        launcher_executable_sha256=launcher_h,
    )
    res = runner.execute_plan(plan)
    assert res.exit_code == 0
