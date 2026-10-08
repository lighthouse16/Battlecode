"""Unit tests for Competition Launch Kit.

Verifies canonical delegation to battlelab.official, single bundle hash concept,
GameSpec conformance, authoritative status reporting, and fail-closed edge cases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from battlelab.cli import main as cli_main
from battlelab.official.models import GameSpec
from battlelab.official.sources import ingest_sources, load_source_bundle_manifest
from battlelab.official.spec import REQUIRED_RULE_SECTIONS, init_game_spec, load_and_validate_spec
from battlelab.storage.paths import get_project_root
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
    assert "Workflow Phase:" in captured
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

    with patch("battlelab.storage.paths.get_project_root", return_value=tmp_path):
        # Even with tampered YAML, the authoritative subsystem must report NOT READY
        cli_main(["competition", "status"])
        out = capsys.readouterr().out
        assert "Official Integration:   NOT READY" in out
        assert "Official Adapter:       NOT READY" in out


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
