"""Regression tests for the solo-first competition operations control plane."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from battlelab.bots.registry import BotRegistry
from battlelab.cli import main
from battlelab.competition.config import load_competition_plan, validate_competition_config
from battlelab.competition.state import (
    append_event,
    initial_state,
    save_state,
    validate_audit_chain,
)
from battlelab.competition.workflow import CompetitionControlPlane, verify_github_ci_run
from battlelab.storage.database import Database
from battlelab.storage.paths import (
    get_competition_releases_dir,
    get_competition_state_path,
)


def _minimal_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_name": "Test Event",
        "timezone": "Asia/Hong_Kong",
        "objective": "Reach finals",
        "operator": "Dang",
        "stages": [
            {
                "id": "prelaunch",
                "name": "Pre-launch",
                "starts_on": "2026-09-30",
                "deadline_on": "2026-10-11",
                "deadline_time_confirmed": False,
                "promotion_profile": "prelaunch",
                "freeze_changes_hours_before_deadline": 0,
                "steps": [
                    {
                        "id": "ci_green",
                        "title": "CI green",
                        "kind": "command",
                        "command": "python -m pytest",
                        "evidence_required": True,
                    },
                    {
                        "id": "policy_recorded",
                        "title": "Policy recorded",
                        "kind": "manual",
                        "evidence_required": True,
                    },
                ],
            },
            {
                "id": "qualifier",
                "name": "Qualifier",
                "starts_on": "2026-11-04",
                "deadline_on": "2026-11-04",
                "deadline_time_confirmed": False,
                "promotion_profile": "qualification",
                "freeze_changes_hours_before_deadline": 0,
                "steps": [
                    {
                        "id": "submit",
                        "title": "Submit manually",
                        "kind": "manual",
                        "evidence_required": True,
                    }
                ],
            },
        ],
        "operating_policy": {
            "maximum_active_challengers": 1,
            "maximum_strategy_archetypes": 3,
            "require_github_ci_for_release": True,
            "require_clean_worktree_for_release": True,
            "require_synced_upstream_for_release": True,
            "require_official_readiness_for_release": True,
            "require_champion_match_for_release": True,
            "require_promoted_experiment_for_release": False,
            "exact_release_acknowledgement": "I_ACKNOWLEDGE_RELEASE_FREEZE",
            "exact_plan_change_acknowledgement": "I_ACKNOWLEDGE_COMPETITION_PLAN_CHANGE",
            "submission_is_manual": True,
        },
    }


def _write_config(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "competition.yaml"
    path.write_text(yaml.safe_dump(config or _minimal_config(), sort_keys=False), encoding="utf-8")
    return path


def test_repository_competition_plan_is_valid_and_day_precision_is_explicit() -> None:
    plan = load_competition_plan("configs/competition.yaml")
    assert plan.objective == "Qualify as one of the eight International finalists"
    assert plan.timezone == "Asia/Hong_Kong"
    assert [stage.stage_id for stage in plan.stages] == [
        "prelaunch",
        "sprint_1",
        "sprint_2",
        "sprint_3",
        "qualifier_freeze",
        "international_qualifier",
        "finals_prep",
        "finals",
    ]
    assert all(not stage.deadline_time_confirmed for stage in plan.stages)
    assert plan.operating_policy["submission_is_manual"] is True
    assert plan.operating_policy["require_promoted_experiment_for_release"] is True


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda c: c.update(schema_version=True), "schema_version"),
        (lambda c: c.update(timezone="Not/AZone"), "timezone"),
        (lambda c: c["stages"][0].update(id="Bad ID"), "must match"),
        (
            lambda c: c["stages"][0].update(deadline_on="2026-09-01"),
            "cannot be before",
        ),
        (
            lambda c: c["stages"][0]["steps"][0].update(command=None),
            "command is required",
        ),
    ],
)
def test_competition_config_rejects_ambiguous_or_malformed_values(
    mutation: Any, expected: str
) -> None:
    config = _minimal_config()
    mutation(config)
    assert any(expected in error for error in validate_competition_config(config))


def test_current_stage_and_next_action_are_date_driven(tmp_path: Path) -> None:
    control = CompetitionControlPlane(_write_config(tmp_path))
    status = control.status(local_date=date(2026, 10, 1))
    assert status["stage"]["id"] == "prelaunch"
    assert status["deadline_time_confirmed"] is False
    assert status["progress"]["next_step"]["id"] == "ci_green"

    qualifier = control.status(local_date=date(2026, 11, 4))
    assert qualifier["stage"]["id"] == "qualifier"


def test_record_step_requires_real_actor_and_evidence(tmp_path: Path) -> None:
    control = CompetitionControlPlane(_write_config(tmp_path))
    with pytest.raises(ValueError, match="real human"):
        control.record_step(
            stage_id="prelaunch", step_id="ci_green", actor="human", evidence="ci/1"
        )
    with pytest.raises(ValueError, match="evidence is required"):
        control.record_step(stage_id="prelaunch", step_id="ci_green", actor="Dang", evidence="")


def test_record_step_is_atomic_audited_and_not_repeatable(tmp_path: Path) -> None:
    control = CompetitionControlPlane(_write_config(tmp_path))
    result = control.record_step(
        stage_id="prelaunch",
        step_id="ci_green",
        actor="Dang",
        evidence="https://github.com/example/repo/actions/runs/1",
    )
    assert len(result["event_hash"]) == 64
    state = json.loads(get_competition_state_path().read_text(encoding="utf-8"))
    valid, _ = validate_audit_chain(state["audit_chain"])
    assert valid
    assert control.next_action()["next_step"]["id"] == "policy_recorded"
    with pytest.raises(ValueError, match="already completed"):
        control.record_step(
            stage_id="prelaunch",
            step_id="ci_green",
            actor="Dang",
            evidence="duplicate",
        )


def test_audit_chain_detects_tampering() -> None:
    state = initial_state("a" * 64)
    append_event(state, event_type="STEP_COMPLETED", actor="Dang", details={"step": "one"})
    assert validate_audit_chain(state["audit_chain"])[0]
    state["audit_chain"][0]["details"]["step"] = "tampered"
    valid, reason = validate_audit_chain(state["audit_chain"])
    assert not valid
    assert "hash mismatch" in reason


def test_config_drift_is_visible_and_blocks_new_checkpoints(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    control = CompetitionControlPlane(config_path)
    save_state(initial_state("f" * 64))
    assert control.status()["config_drift"] is True
    with pytest.raises(ValueError, match="config changed"):
        control.record_step(
            stage_id="prelaunch",
            step_id="ci_green",
            actor="Dang",
            evidence="ci",
        )


def test_config_drift_can_only_be_reconciled_by_reviewed_clean_synced_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = CompetitionControlPlane(_write_config(tmp_path))
    save_state(initial_state("f" * 64))
    monkeypatch.setattr(
        "battlelab.competition.workflow.get_git_snapshot",
        lambda: {
            "head": "a" * 40,
            "branch": "main",
            "dirty": False,
            "upstream": "a" * 40,
            "synced_upstream": True,
        },
    )
    result = control.reconcile_config(
        actor="Dang",
        reason="Organizer published a corrected official stage date",
        reviewed_commit="a" * 40,
        acknowledgement="I_ACKNOWLEDGE_COMPETITION_PLAN_CHANGE",
    )
    assert result["old_config_hash"] == "f" * 64
    assert control.status()["config_drift"] is False
    state = json.loads(get_competition_state_path().read_text(encoding="utf-8"))
    assert state["audit_chain"][-1]["event_type"] == "PLAN_RECONCILED"


def _register_champion(tmp_path: Path) -> Any:
    source = tmp_path / "bot.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    registry = BotRegistry(Database())
    artifact = registry.register_bot(source, display_name="Release Candidate")
    registry.update_champion_manifest(
        artifact.artifact_id,
        experiment_id=None,
        updated_at=datetime.now(timezone.utc).isoformat(),
        reason="test champion",
    )
    return artifact


def _patch_release_dependencies(monkeypatch: pytest.MonkeyPatch, *, ready: bool = True) -> None:
    monkeypatch.setattr(
        "battlelab.competition.workflow.get_git_snapshot",
        lambda: {
            "head": "a" * 40,
            "branch": "main",
            "dirty": False,
            "upstream": "a" * 40,
            "synced_upstream": True,
        },
    )

    class FakeReport:
        blockers = [] if ready else ["sdk_missing"]

        def __init__(self) -> None:
            self.ready = ready

        def to_dict(self) -> dict[str, Any]:
            return {
                "ready": ready,
                "blockers": self.blockers,
                "spec_hash": "b" * 64 if ready else None,
                "source_bundle_hash": "c" * 64 if ready else None,
                "sdk_version": "test-sdk" if ready else None,
            }

    monkeypatch.setattr(
        "battlelab.official.readiness.OfficialReadinessChecker.evaluate",
        lambda _self: FakeReport(),
    )
    monkeypatch.setattr(
        "battlelab.competition.workflow.verify_github_ci_run",
        lambda url, commit: (
            url.startswith("https://github.com/"),
            f"successful run for {commit}" if url.startswith("https://github.com/") else "invalid",
        ),
    )


def test_release_freeze_requires_exact_acknowledgement(tmp_path: Path) -> None:
    artifact = _register_champion(tmp_path)
    control = CompetitionControlPlane(_write_config(tmp_path))
    with pytest.raises(ValueError, match="exactly equal"):
        control.freeze_release(
            artifact_id=artifact.artifact_id,
            experiment_id=None,
            actor="Dang",
            reviewed_commit="a" * 40,
            ci_run_url="https://github.com/example/repo/actions/runs/1",
            acknowledgement="yes",
        )


@pytest.mark.parametrize(
    ("payload", "expected", "message"),
    [
        (
            {"headSha": "a" * 40, "status": "completed", "conclusion": "success", "url": "x"},
            True,
            "successful run",
        ),
        (
            {"headSha": "b" * 40, "status": "completed", "conclusion": "success", "url": "x"},
            False,
            "does not match",
        ),
        (
            {"headSha": "a" * 40, "status": "completed", "conclusion": "failure", "url": "x"},
            False,
            "not successful",
        ),
    ],
)
def test_github_ci_evidence_is_queried_and_bound_to_exact_commit(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
    expected: bool,
    message: str,
) -> None:
    class Result:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(
        "battlelab.competition.workflow.subprocess.run", lambda *args, **kwargs: Result()
    )
    valid, detail = verify_github_ci_run(
        "https://github.com/lighthouse16/Battlecode/actions/runs/123", "a" * 40
    )
    assert valid is expected
    assert message in detail


def test_release_freeze_fails_closed_when_official_readiness_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _register_champion(tmp_path)
    _patch_release_dependencies(monkeypatch, ready=False)
    control = CompetitionControlPlane(_write_config(tmp_path))
    with pytest.raises(ValueError, match="official_readiness"):
        control.freeze_release(
            artifact_id=artifact.artifact_id,
            experiment_id=None,
            actor="Dang",
            reviewed_commit="a" * 40,
            ci_run_url="https://github.com/example/repo/actions/runs/1",
            acknowledgement="I_ACKNOWLEDGE_RELEASE_FREEZE",
        )


def test_production_policy_requires_a_promoted_experiment_for_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _minimal_config()
    config["operating_policy"]["require_promoted_experiment_for_release"] = True
    artifact = _register_champion(tmp_path)
    _patch_release_dependencies(monkeypatch)
    control = CompetitionControlPlane(_write_config(tmp_path, config))
    with pytest.raises(ValueError, match="promoted_experiment_matches"):
        control.freeze_release(
            artifact_id=artifact.artifact_id,
            experiment_id=None,
            actor="Dang",
            reviewed_commit="a" * 40,
            ci_run_url="https://github.com/example/repo/actions/runs/1",
            acknowledgement="I_ACKNOWLEDGE_RELEASE_FREEZE",
        )


def test_release_freeze_and_verify_bind_artifact_config_git_and_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _register_champion(tmp_path)
    _patch_release_dependencies(monkeypatch)
    control = CompetitionControlPlane(_write_config(tmp_path))
    result = control.freeze_release(
        artifact_id=artifact.artifact_id,
        experiment_id=None,
        actor="Dang",
        reviewed_commit="a" * 40,
        ci_run_url="https://github.com/example/repo/actions/runs/123",
        acknowledgement="I_ACKNOWLEDGE_RELEASE_FREEZE",
        note="qualification candidate",
    )
    assert result["release"]["artifact_manifest_hash"] == artifact.manifest_hash
    assert result["release"]["competition_config_hash"] == control.plan.canonical_hash
    assert result["release"]["git"]["head"] == "a" * 40
    assert control.verify_release("latest")["valid"] is True


def test_release_manifest_tampering_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _register_champion(tmp_path)
    _patch_release_dependencies(monkeypatch)
    control = CompetitionControlPlane(_write_config(tmp_path))
    result = control.freeze_release(
        artifact_id=artifact.artifact_id,
        experiment_id=None,
        actor="Dang",
        reviewed_commit="a" * 40,
        ci_run_url="https://github.com/example/repo/actions/runs/123",
        acknowledgement="I_ACKNOWLEDGE_RELEASE_FREEZE",
    )
    path = Path(result["release_path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["actor"] = "attacker"
    path.write_text(json.dumps(data), encoding="utf-8")
    verification = control.verify_release("latest")
    assert verification["valid"] is False
    assert verification["checks"][0] == {"name": "release_hash", "passed": False}


def test_release_reverification_fails_if_official_readiness_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _register_champion(tmp_path)
    _patch_release_dependencies(monkeypatch, ready=True)
    control = CompetitionControlPlane(_write_config(tmp_path))
    control.freeze_release(
        artifact_id=artifact.artifact_id,
        experiment_id=None,
        actor="Dang",
        reviewed_commit="a" * 40,
        ci_run_url="https://github.com/example/repo/actions/runs/123",
        acknowledgement="I_ACKNOWLEDGE_RELEASE_FREEZE",
    )
    _patch_release_dependencies(monkeypatch, ready=False)
    verification = control.verify_release("latest")
    assert verification["valid"] is False
    failed = {check["name"] for check in verification["checks"] if not check["passed"]}
    assert "official_readiness" in failed
    assert "official_provenance_unchanged" in failed


def test_release_freeze_rejects_non_github_ci_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _register_champion(tmp_path)
    _patch_release_dependencies(monkeypatch)
    control = CompetitionControlPlane(_write_config(tmp_path))
    with pytest.raises(ValueError, match="github_ci_evidence"):
        control.freeze_release(
            artifact_id=artifact.artifact_id,
            experiment_id=None,
            actor="Dang",
            reviewed_commit="a" * 40,
            ci_run_url="https://example.com/green",
            acknowledgement="I_ACKNOWLEDGE_RELEASE_FREEZE",
        )


def test_cli_status_next_and_complete_have_machine_readable_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)
    assert main(["competition", "--config", str(config_path), "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["submission_is_manual"] is True

    assert main(["competition", "--config", str(config_path), "next", "--json"]) == 0
    next_action = json.loads(capsys.readouterr().out)
    assert next_action["next_step"]["id"] == "ci_green"

    assert (
        main(
            [
                "competition",
                "--config",
                str(config_path),
                "complete",
                "--stage",
                "prelaunch",
                "--step",
                "ci_green",
                "--actor",
                "Dang",
                "--evidence",
                "ci-run-1",
                "--json",
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert len(completed["event_hash"]) == 64


def test_release_directory_is_inside_isolated_data_dir() -> None:
    assert get_competition_releases_dir().parent.name == "competition"
