"""Unit tests for official source ingestion, game spec, command runner, and readiness."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from battlelab.adapters import get_adapter
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
from battlelab.official.models import (
    NormalizedReplay,
    RuleItem,
    RuleVerificationState,
    compare_replay_determinism,
)
from battlelab.official.readiness import OfficialReadinessChecker
from battlelab.official.sources import ingest_sources, load_source_bundle_manifest
from battlelab.official.spec import init_game_spec, validate_game_spec


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
