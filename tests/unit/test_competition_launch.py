"""Unit tests for Competition Launch Kit.

Verifies canonical delegation to battlelab.official, single bundle hash concept,
GameSpec conformance, authoritative status reporting, and fail-closed edge cases.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from battlelab.bots.registry import BotRegistry
from battlelab.cli import main as cli_main
from battlelab.official.models import GameSpec, ReadinessReport
from battlelab.official.sources import ingest_sources, load_source_bundle_manifest
from battlelab.official.spec import (
    REQUIRED_RULE_SECTIONS,
    init_game_spec,
    load_and_validate_spec,
    validate_game_spec,
)
from battlelab.storage.database import Database
from battlelab.storage.paths import get_champion_manifest_path, get_project_root
from scripts.bootstrap_competition import bootstrap_competition
from scripts.bootstrap_competition import main as bootstrap_main


def test_bootstrap_delegates_to_official_ingest(tmp_path: Path):
    """Verify bootstrap_competition delegates to official ingest and produces identical hash and manifest."""
    src_dir = tmp_path / "materials"
    src_dir.mkdir()
    (src_dir / "rules.md").write_text(
        "# Official Autumn 2026 Rules\nWin by score.", encoding="utf-8"
    )
    (src_dir / "starter.py").write_text("print('turn')", encoding="utf-8")

    bundles_dir = tmp_path / "bundles"

    # Call through bootstrap wrapper
    bootstrap_result = bootstrap_competition(src_dir, copy_files=True, bundles_dir=bundles_dir)

    # Call canonical ingest directly
    canonical_manifest = ingest_sources(src_dir, copy_files=True, bundles_dir=bundles_dir)

    # Must produce the exact same single canonical bundle hash
    assert bootstrap_result["bundle_hash"] == canonical_manifest.bundle_hash
    assert bootstrap_result["file_count"] == canonical_manifest.file_count == 2
    assert bootstrap_result["total_size_bytes"] == canonical_manifest.total_size_bytes

    # Load from disk to verify canonical layout
    loaded = load_source_bundle_manifest(bootstrap_result["bundle_hash"], bundles_dir=bundles_dir)
    assert loaded.bundle_hash == bootstrap_result["bundle_hash"]


def test_bootstrap_no_source_mutation(tmp_path: Path):
    """Verify that bootstrap does NOT mutate original source files."""
    src_dir = tmp_path / "materials"
    src_dir.mkdir()
    test_file = src_dir / "rules.txt"
    content = b"RULESET_AUTUMN_2026_ORIGINAL"
    test_file.write_bytes(content)
    stat_before = test_file.stat()

    bundles_dir = tmp_path / "bundles"
    bootstrap_competition(src_dir, copy_files=True, bundles_dir=bundles_dir)

    assert test_file.read_bytes() == content
    assert test_file.stat().st_size == stat_before.st_size
    assert test_file.stat().st_mtime == stat_before.st_mtime


def test_bootstrap_fails_on_missing_path(tmp_path: Path):
    """Verify missing materials path fails closed with FileNotFoundError and exits nonzero."""
    missing = tmp_path / "nonexistent"
    with pytest.raises(FileNotFoundError):
        bootstrap_competition(missing, bundles_dir=tmp_path / "bundles")

    ret = bootstrap_main([str(missing)])
    assert ret == 1


def test_bootstrap_fails_on_empty_directory(tmp_path: Path):
    """Verify empty materials directory fails closed with ValueError and exits nonzero."""
    empty_dir = tmp_path / "empty_materials"
    empty_dir.mkdir()

    with pytest.raises(ValueError, match="contains no regular files"):
        bootstrap_competition(empty_dir, bundles_dir=tmp_path / "bundles")

    ret = bootstrap_main([str(empty_dir)])
    assert ret == 1


def test_bootstrap_rejects_symlinks(tmp_path: Path):
    """Verify symlinks in official materials are rejected per official security policy."""
    src_dir = tmp_path / "materials"
    src_dir.mkdir()
    target_file = tmp_path / "external_target.txt"
    target_file.write_text("target", encoding="utf-8")

    link_path = src_dir / "symlink_doc.txt"
    try:
        os.symlink(target_file, link_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    bundles_dir = tmp_path / "bundles"
    with pytest.raises(ValueError, match="Symlinks are rejected"):
        bootstrap_competition(src_dir, bundles_dir=bundles_dir)


def test_spec_initialization_conforms_to_gamespec(tmp_path: Path):
    """Verify initializing a spec creates a valid 23-rule GameSpec referencing the bundle."""
    src_dir = tmp_path / "materials"
    src_dir.mkdir()
    (src_dir / "rules.md").write_text("# Rules", encoding="utf-8")

    bundles_dir = tmp_path / "bundles"
    manifest = bootstrap_competition(src_dir, bundles_dir=bundles_dir)
    bundle_hash = manifest["bundle_hash"]

    out_spec = tmp_path / "configs" / "game_spec.yaml"
    spec = init_game_spec(
        source_bundle_hash=bundle_hash, output_path=out_spec, bundles_dir=bundles_dir
    )

    assert isinstance(spec, GameSpec)
    assert spec.schema_version == "1.0.0"
    assert spec.source_bundle_hash == bundle_hash
    assert len(spec.rules) == 23
    for sec in REQUIRED_RULE_SECTIONS:
        assert sec in spec.rules
        assert spec.rules[sec].verification_state == "MISSING"

    # Verify file written to disk can be loaded
    is_valid, errors, loaded, is_ready = load_and_validate_spec(out_spec, bundles_dir=bundles_dir)
    assert is_valid is True
    assert errors == []
    assert loaded is not None
    assert loaded.schema_version == "1.0.0"
    assert loaded.source_bundle_hash == bundle_hash
    assert len(loaded.rules) == 23


def test_cli_competition_status_authoritative_vs_tampered_state(capsys, tmp_path: Path):
    """Verify battlelab competition status reports authoritative unreadiness even if state YAML is tampered."""
    # Normal status check
    ret = cli_main(["competition", "status"])
    assert ret == 0
    captured = capsys.readouterr().out
    assert "OPERATIONAL WORKFLOW PROGRESS" in captured
    assert "Official Integration:   NOT READY" in captured
    assert "Official Adapter:       NOT READY" in captured
    assert "Active Blockers" in captured

    # JSON mode
    ret_json = cli_main(["competition", "status", "--json"])
    assert ret_json == 0
    data = json.loads(capsys.readouterr().out)
    assert "workflow" in data
    assert "authoritative" in data
    assert data["authoritative"]["official_readiness"]["ready"] is False
    assert data["authoritative"]["adapter"]["can_run_local"] is False

    # Simulate tampered COMPETITION_STATE.yaml that claims READY
    tampered_state = {
        "workflow": {
            "current_phase": "PHASE_E_SUBMISSION",
            "status": "READY_FOR_SUBMISSION",
            "next_action": "Submit",
        },
        "official": {"ready": True, "status": "VERIFIED"},
        "adapter": {"status": "READY", "can_run_local": True},
        "submission_controls": {
            "operator_approval_required": True,
            "manual_submission_authorized": False,
        },
    }

    tampered_file = tmp_path / "competition" / "COMPETITION_STATE.yaml"
    tampered_file.parent.mkdir(parents=True, exist_ok=True)
    tampered_file.write_text(yaml.safe_dump(tampered_state), encoding="utf-8")

    # Patch battlelab.cli.get_project_root to ensure CLI loads the tampered file
    with patch("battlelab.cli.get_project_root", return_value=tmp_path):
        ret_tampered = cli_main(["competition", "status"])
        assert ret_tampered == 0
        out = capsys.readouterr().out
        # Verify untrusted operator progress text was indeed loaded from tampered YAML
        assert "PHASE_E_SUBMISSION" in out
        assert "READY_FOR_SUBMISSION" in out
        # Crucially: authoritative subsystem remains strictly NOT READY
        assert "Official Integration:   NOT READY" in out
        assert "Official Adapter:       NOT READY" in out

        # JSON mode confirms tampered workflow alongside authoritative NOT READY
        ret_tampered_json = cli_main(["competition", "status", "--json"])
        assert ret_tampered_json == 0
        t_data = json.loads(capsys.readouterr().out)
        assert t_data["workflow"]["current_phase"] == "PHASE_E_SUBMISSION"
        assert t_data["workflow"]["status"] == "READY_FOR_SUBMISSION"
        assert t_data["authoritative"]["official_readiness"]["ready"] is False
        assert t_data["authoritative"]["adapter"]["can_run_local"] is False


def test_champion_init_governance(tmp_path: Path, monkeypatch, capsys):
    """Verify safe Champion v0 initialization flow via CLI."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    get_champion_manifest_path().unlink(missing_ok=True)

    # Register candidate bot
    ret_reg = cli_main(
        [
            "bot",
            "register",
            "bots/baselines/fixed_bot.py",
            "--name",
            "Champion_v0",
            "--tags",
            "policy:baseline,version:v0",
        ]
    )
    assert ret_reg == 0
    reg_out = capsys.readouterr().out
    import re

    match = re.search(r"Artifact ID:\s+(art_[a-f0-9]+)", reg_out)
    assert match is not None
    art_id = match.group(1)

    # Reject missing/invalid artifact ID
    ret_no_art = cli_main(
        [
            "champion",
            "init",
            "art_missing",
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
        ]
    )
    assert ret_no_art == 1
    assert "Artifact not found" in capsys.readouterr().err

    # Reject generic actor
    for generic in ["human", "default", "unknown", "system", "root"]:
        ret_gen = cli_main(
            [
                "champion",
                "init",
                art_id,
                "--reason",
                "Initial baseline Champion v0",
                "--actor",
                generic,
            ]
        )
        assert ret_gen == 1
        assert "explicit, named non-generic actor" in capsys.readouterr().err

    # Reject short reason (< 10 chars)
    ret_short = cli_main(
        ["champion", "init", art_id, "--reason", "short", "--actor", "operator_alice"]
    )
    assert ret_short == 1
    assert "explicit reason" in capsys.readouterr().err

    # Ordinary initialization without verified official adapter must fail closed
    ret_unverified = cli_main(
        [
            "champion",
            "init",
            art_id,
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
        ]
    )
    assert ret_unverified == 1
    assert "Official adapter is not verified for local execution" in capsys.readouterr().err

    # Explicit override allows initialization during preliminary development
    ret_init = cli_main(
        [
            "champion",
            "init",
            art_id,
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
            "--allow-unverified-adapter",
        ]
    )
    assert ret_init == 0
    init_out = capsys.readouterr().out
    assert "Successfully INITIALIZED Champion v0" in init_out
    assert art_id in init_out
    assert "INITIAL_CHAMPION_V0_UNVERIFIED_OVERRIDE" in init_out
    assert "NO SUBMISSION READINESS IMPLIED" in init_out

    # Verify champion is now active
    ret_stat = cli_main(["champion", "status"])
    assert ret_stat == 0
    stat_out = capsys.readouterr().out
    assert art_id in stat_out

    # Second champion init must fail closed (already initialized)
    ret_reinit = cli_main(
        [
            "champion",
            "init",
            art_id,
            "--reason",
            "Attempt duplicate init",
            "--actor",
            "operator_alice",
            "--allow-unverified-adapter",
        ]
    )
    assert ret_reinit == 1
    assert "Champion is already initialized" in capsys.readouterr().err


def test_prompt01_rule_verification_states_against_validator(tmp_path: Path):
    """Verify Prompt 01 rule specification conforms to canonical validator and rejects invalid states."""
    src_dir = tmp_path / "materials"
    src_dir.mkdir()
    (src_dir / "rules.md").write_text("# Official Rules\nScoring and turns.", encoding="utf-8")
    bundles_dir = tmp_path / "bundles"
    manifest = bootstrap_competition(src_dir, bundles_dir=bundles_dir)
    bundle_hash = manifest["bundle_hash"]
    doc_hash = manifest["files"][0]["sha256"]

    # Valid spec matching Prompt 01 instructions
    rules: dict[str, Any] = {}
    for sec in REQUIRED_RULE_SECTIONS:
        rules[sec] = {
            "meaning": f"Rule specification for {sec}",
            "source_refs": [],
            "verification_state": "MISSING",
            "implementation_impacts": [],
            "test_coverage": [],
            "notes": "",
        }

    # Document one rule with valid citation
    sec_name = "victory_loss_draw_tiebreak"
    rules[sec_name]["verification_state"] = "DOCUMENTED"
    rules[sec_name]["meaning"] = "Win by destroying opponent base"
    rules[sec_name]["source_refs"] = ["rules.md#scoring"]

    valid_spec = {
        "schema_version": "1.0.0",
        "competition_name": "Battlecode",
        "competition_season": "Autumn2026",
        "spec_version": "1.0.0",
        "source_bundle_hash": bundle_hash,
        "official_document_hashes": [doc_hash],
        "sdk_version": "1.0.0",
        "rules": rules,
    }

    is_valid, errors, spec_obj, _ = validate_game_spec(
        valid_spec, bundles_dir=bundles_dir, project_root=get_project_root()
    )
    assert is_valid is True
    assert errors == []
    assert spec_obj is not None

    # Verify invalid non-canonical states (e.g. from old Prompt 01) are strictly rejected
    for invalid_state in ["VERIFIED", "UNKNOWN", "CONFLICTING"]:
        invalid_spec = dict(valid_spec)
        invalid_rules = dict(rules)
        invalid_rules[sec_name] = dict(rules[sec_name])
        invalid_rules[sec_name]["verification_state"] = invalid_state
        invalid_spec["rules"] = invalid_rules

        inv_valid, inv_errors, _, _ = validate_game_spec(
            invalid_spec, bundles_dir=bundles_dir, project_root=get_project_root()
        )
        assert inv_valid is False
        assert any("invalid state" in e and invalid_state in e for e in inv_errors)


def test_competition_state_and_templates_integrity():
    """Verify launch kit files exist, template is docs/game_spec.template.yaml, and no duplicate template."""
    root = get_project_root()
    comp_dir = root / "competition"

    assert (comp_dir / "START_COMPETITION.md").exists()
    state_file = comp_dir / "COMPETITION_STATE.yaml"
    assert state_file.exists()

    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert "workflow" in state
    assert "operator_decisions" in state
    assert "submission_controls" in state
    assert state["submission_controls"]["operator_approval_required"] is True

    # Duplicate template must NOT exist
    assert not (comp_dir / "templates" / "game_spec.yaml").exists()

    # Canonical template exists in docs
    assert (root / "docs" / "game_spec.template.yaml").exists()

    # Templates for research & report exist
    assert (comp_dir / "templates" / "experiment.yaml").exists()
    assert (comp_dir / "templates" / "ladder_report.md").exists()

    # Prompts exist and are non-trivial
    prompts_dir = comp_dir / "prompts"
    for p_name in [
        "01_ingest_official_materials.md",
        "02_configure_official_adapter.md",
        "03_build_champion_v0.md",
        "04_strategy_research.md",
        "05_ladder_replay_analysis.md",
    ]:
        p = prompts_dir / p_name
        assert p.exists()
        assert len(p.read_text(encoding="utf-8").strip()) > 100


def test_champion_init_with_verified_official_adapter_succeeds_without_override(
    tmp_path: Path, monkeypatch, capsys
):
    """Verify champion init succeeds without override flags when official adapter is local-ready."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    get_champion_manifest_path().unlink(missing_ok=True)

    ret_reg = cli_main(
        [
            "bot",
            "register",
            "bots/baselines/fixed_bot.py",
            "--name",
            "Official_Champ_v0",
            "--tags",
            "policy:baseline,version:v0",
        ]
    )
    assert ret_reg == 0
    import re

    match = re.search(r"Artifact ID:\s+(art_[a-f0-9]+)", capsys.readouterr().out)
    assert match is not None
    art_id = match.group(1)

    mock_report = ReadinessReport(
        ready=True,
        can_run_local=True,
        can_submit=False,
        blockers=[],
        checks=[],
    )

    with patch(
        "battlelab.official.readiness.OfficialReadinessChecker.evaluate",
        return_value=mock_report,
    ):
        ret_init = cli_main(
            [
                "champion",
                "init",
                art_id,
                "--reason",
                "Official baseline Champion v0",
                "--actor",
                "operator_bob",
            ]
        )
        assert ret_init == 0
        out = capsys.readouterr().out
        assert "Successfully INITIALIZED Champion v0" in out
        assert "Mode:        INITIAL_CHAMPION_V0" in out
        assert "Notice" not in out

    # Verify DB promotion record mode is strictly INITIAL_CHAMPION_V0 and no override acknowledgement
    db = Database()
    proms = db.list_promotions()
    assert len(proms) == 1
    assert proms[0]["mode"] == "INITIAL_CHAMPION_V0"
    assert proms[0]["override_acknowledgement"] is None
    assert proms[0]["gate_violations"] == []


def test_champion_init_blocks_on_corrupt_or_orphaned_manifest(tmp_path: Path, monkeypatch, capsys):
    """Verify champion init fails closed and never overwrites a corrupted or orphaned manifest."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    champ_manifest_path = get_champion_manifest_path()
    champ_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Corrupt JSON manifest
    corrupt_content = '{"champion_artifact_id": "malformed_json_without_closing'
    champ_manifest_path.write_text(corrupt_content, encoding="utf-8")

    ret_corrupt = cli_main(
        [
            "champion",
            "init",
            "art_dummy",
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
            "--allow-unverified-adapter",
        ]
    )
    assert ret_corrupt == 1
    err_corrupt = capsys.readouterr().err
    assert "Existing champion manifest found" in err_corrupt
    assert "invalid or corrupted" in err_corrupt
    # Assert corrupt manifest was NOT overwritten
    assert champ_manifest_path.read_text(encoding="utf-8") == corrupt_content

    # 2. Valid JSON pointing to unregistered/orphaned artifact
    orphaned_content = json.dumps({"champion_artifact_id": "art_nonexistent_999"})
    champ_manifest_path.write_text(orphaned_content, encoding="utf-8")

    ret_orphan = cli_main(
        [
            "champion",
            "init",
            "art_dummy",
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
            "--allow-unverified-adapter",
        ]
    )
    assert ret_orphan == 1
    err_orphan = capsys.readouterr().err
    assert "Existing champion manifest found" in err_orphan
    assert "invalid or corrupted" in err_orphan
    assert champ_manifest_path.read_text(encoding="utf-8") == orphaned_content


def test_champion_init_blocks_on_existing_promotions_without_manifest(
    tmp_path: Path, monkeypatch, capsys
):
    """Verify champion init fails closed if promotion history exists but manifest is absent."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    get_champion_manifest_path().unlink(missing_ok=True)
    db = Database()
    registry = BotRegistry(db)
    art = registry.register_bot(
        source_path="bots/baselines/fixed_bot.py",
        display_name="Historical_Bot",
    )
    db.save_promotion(
        promotion_id="prom_test_existing",
        experiment_id=None,
        artifact_id=art.artifact_id,
        promoted_at="2026-10-08T00:00:00Z",
        manifest_snapshot={},
        reason="Historical promotion",
    )

    champ_manifest_path = get_champion_manifest_path()
    assert not champ_manifest_path.exists()

    ret = cli_main(
        [
            "champion",
            "init",
            art.artifact_id,
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
            "--allow-unverified-adapter",
        ]
    )
    assert ret == 1
    err = capsys.readouterr().err
    assert "Existing promotion history found in database" in err
    assert not champ_manifest_path.exists()


def test_champion_init_audit_first_fails_closed_on_db_save_failure(
    tmp_path: Path, monkeypatch, capsys
):
    """Verify audit-first fail-closed semantics: DB failure prevents manifest creation."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    get_champion_manifest_path().unlink(missing_ok=True)

    ret_reg = cli_main(
        [
            "bot",
            "register",
            "bots/baselines/fixed_bot.py",
            "--name",
            "Candidate_Fail_DB",
            "--tags",
            "policy:baseline",
        ]
    )
    assert ret_reg == 0
    import re

    match = re.search(r"Artifact ID:\s+(art_[a-f0-9]+)", capsys.readouterr().out)
    assert match is not None
    art_id = match.group(1)

    champ_manifest_path = get_champion_manifest_path()
    assert not champ_manifest_path.exists()

    with patch.object(
        Database,
        "save_promotion",
        side_effect=sqlite3.DatabaseError("Injected disk I/O error"),
    ):
        ret = cli_main(
            [
                "champion",
                "init",
                art_id,
                "--reason",
                "Initial baseline Champion v0",
                "--actor",
                "operator_alice",
                "--allow-unverified-adapter",
            ]
        )
        assert ret == 1
        err = capsys.readouterr().err
        assert "Failed to record promotion audit in database" in err

    # Active champion manifest was never created
    assert not champ_manifest_path.exists()
    registry = BotRegistry()
    assert registry.get_champion_artifact() is None


def test_champion_init_compensating_rollback_on_manifest_write_failure(
    tmp_path: Path, monkeypatch, capsys
):
    """Verify compensating rollback: manifest failure deletes newly inserted DB audit row."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    get_champion_manifest_path().unlink(missing_ok=True)

    ret_reg = cli_main(
        [
            "bot",
            "register",
            "bots/baselines/fixed_bot.py",
            "--name",
            "Candidate_Fail_Manifest",
            "--tags",
            "policy:baseline",
        ]
    )
    assert ret_reg == 0
    import re

    match = re.search(r"Artifact ID:\s+(art_[a-f0-9]+)", capsys.readouterr().out)
    assert match is not None
    art_id = match.group(1)

    champ_manifest_path = get_champion_manifest_path()
    assert not champ_manifest_path.exists()

    with patch.object(
        BotRegistry,
        "update_champion_manifest",
        side_effect=OSError("Injected permission denied writing manifest"),
    ):
        ret = cli_main(
            [
                "champion",
                "init",
                art_id,
                "--reason",
                "Initial baseline Champion v0",
                "--actor",
                "operator_alice",
                "--allow-unverified-adapter",
            ]
        )
        assert ret == 1
        err = capsys.readouterr().err
        assert "Failed to write champion manifest" in err

    # Compensating rollback cleanly purged the promotion row from DB
    db = Database()
    assert len(db.list_promotions()) == 0
    assert not champ_manifest_path.exists()


def test_champion_init_single_writer_lock_prevents_race(tmp_path: Path, monkeypatch, capsys):
    """Verify single-writer mutual exclusion lock blocks concurrent first-initialization."""
    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(tmp_path / "data"))
    get_champion_manifest_path().unlink(missing_ok=True)

    ret_reg = cli_main(
        [
            "bot",
            "register",
            "bots/baselines/fixed_bot.py",
            "--name",
            "Candidate_Race",
            "--tags",
            "policy:baseline",
        ]
    )
    assert ret_reg == 0
    import re

    match = re.search(r"Artifact ID:\s+(art_[a-f0-9]+)", capsys.readouterr().out)
    assert match is not None
    art_id = match.group(1)

    champ_manifest_path = get_champion_manifest_path()
    lock_path = champ_manifest_path.with_suffix(".init.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("99999\n", encoding="utf-8")

    ret_race = cli_main(
        [
            "champion",
            "init",
            art_id,
            "--reason",
            "Initial baseline Champion v0",
            "--actor",
            "operator_alice",
            "--allow-unverified-adapter",
        ]
    )
    assert ret_race == 1
    err = capsys.readouterr().err
    assert "Concurrent champion initialization detected" in err
    assert not champ_manifest_path.exists()

    # Lock file is preserved as evidence and not forcibly deleted by failing caller
    assert lock_path.exists()
