"""Unit tests for official source ingestion, game spec, command runner, and readiness."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

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
    is_valid, errors, spec_obj, is_ready = validate_game_spec(spec.to_dict())
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
