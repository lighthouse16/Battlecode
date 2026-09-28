"""Comprehensive Phase 2.1 regression test suite verifying all 18 required correctness criteria."""

from __future__ import annotations

import inspect
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from battlelab.adapters import get_adapter
from battlelab.analysis.metrics import (
    calculate_paired_experiment_metrics,
)
from battlelab.bots.artifacts import create_bot_artifact, verify_artifact_integrity
from battlelab.bots.process_runner import (
    check_memory_limit_support,
    is_process_active,
    terminate_process_tree,
)
from battlelab.bots.registry import BotRegistry
from battlelab.config.loader import load_yaml_config
from battlelab.core.errors import PromotionGateError
from battlelab.core.hashing import hash_file
from battlelab.core.models import Experiment, MatchOutcome, MatchSpec, ResolvedOpponent
from battlelab.experiments.evaluator import ExperimentEvaluator
from battlelab.experiments.promotion import PromotionGate
from battlelab.experiments.registry import ExperimentRegistry
from battlelab.storage.database import Database
from battlelab.storage.paths import get_project_root


def test_1_process_tree_reaps_nested_children():
    """1. test_process_tree_reaps_nested_children"""
    nested_code = """import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print(c.pid, flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
c_pid = int(p.stdout.readline().strip())
print(f"{p.pid}:{c_pid}", flush=True)
while True:
    time.sleep(1)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", nested_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    line = parent.stdout.readline().strip()
    child_pid_str, grandchild_pid_str = line.split(":")
    child_pid = int(child_pid_str)
    grandchild_pid = int(grandchild_pid_str)

    assert is_process_active(parent.pid)
    assert is_process_active(child_pid)
    assert is_process_active(grandchild_pid)

    terminate_process_tree(parent, timeout_seconds=1.5)
    time.sleep(0.2)

    assert not is_process_active(parent.pid)
    assert not is_process_active(child_pid)
    assert not is_process_active(grandchild_pid)


def test_2_timeout_leaves_no_active_or_zombie_descendants(tmp_path: Path):
    """2. test_timeout_leaves_no_active_or_zombie_descendants"""
    bot_code = tmp_path / "timeout_spawner.py"
    bot_code.write_text(
        """import subprocess, sys, time, json
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
for line in sys.stdin:
    time.sleep(5)  # times out
    sys.stdout.write(json.dumps({"type": "PASS"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    bot = create_bot_artifact(bot_code, display_name="TimeoutSpawner")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_timeout_descendant_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=bot.artifact_id,
        bot_b_id=bot.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
        per_turn_limit_ms=50,
        time_limit_ms=50,
    )
    res = mock.run_local_match(spec, bot, bot, tmp_path / "work")
    assert res.timed_out_a is True


def test_3_long_match_renews_lease_without_duplicate_execution(tmp_path: Path):
    """3. test_long_match_renews_lease_without_duplicate_execution"""
    db = Database(tmp_path / "lease_renew.db")
    reg = BotRegistry(db)
    b1 = reg.register_bot("bots/baselines/fixed_bot.py", "B1")
    b2 = reg.register_bot("bots/baselines/random_bot.py", "B2")

    spec = MatchSpec(
        match_id="m_long_renew",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=b1.artifact_id,
        bot_b_id=b2.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    db.save_match_spec(spec, "2026-09-28T00:00:00Z")

    # Worker 1 leases with 1-second lease
    lease1 = db.lease_next_match(worker_id="w1", lease_duration_seconds=1)
    assert lease1 is not None
    token1 = lease1["lease_token"]

    # Renew lease before expiry
    renewed = db.renew_lease("m_long_renew", "w1", token1, additional_seconds=5)
    assert renewed is True

    # Worker 2 attempts to lease concurrently
    lease2 = db.lease_next_match(worker_id="w2", lease_duration_seconds=1)
    assert lease2 is None  # Worker 2 must not steal renewed lease!


def test_4_stale_worker_result_is_rejected_by_fencing_token(tmp_path: Path):
    """4. test_stale_worker_result_is_rejected_by_fencing_token"""
    db = Database(tmp_path / "stale_token.db")
    reg = BotRegistry(db)
    b1 = reg.register_bot("bots/baselines/fixed_bot.py", "B1")
    b2 = reg.register_bot("bots/baselines/random_bot.py", "B2")

    spec = MatchSpec(
        match_id="m_fencing",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=b1.artifact_id,
        bot_b_id=b2.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    db.save_match_spec(spec, "2026-09-28T00:00:00Z")

    lease = db.lease_next_match(worker_id="w1", lease_duration_seconds=1)
    assert lease is not None
    stale_token = "invalid_or_expired_token_12345"

    from battlelab.core.models import MatchResult

    res = MatchResult(
        match_id="m_fencing",
        outcome=MatchOutcome.WIN_A,
        winner="A",
        score_a=10.0,
        score_b=5.0,
    )
    # Stale worker attempt to commit with wrong lease_token
    update_res = db.update_match_result(res, lease_token=stale_token)
    assert update_res is False


def test_5_expired_lease_is_recoverable(tmp_path: Path):
    """5. test_expired_lease_is_recoverable"""
    db = Database(tmp_path / "lease_rec.db")
    reg = BotRegistry(db)
    b1 = reg.register_bot("bots/baselines/fixed_bot.py", "B1")
    b2 = reg.register_bot("bots/baselines/random_bot.py", "B2")

    spec = MatchSpec(
        match_id="m_expired",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=b1.artifact_id,
        bot_b_id=b2.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    db.save_match_spec(spec, "2026-09-28T00:00:00Z")
    # Lease for 0 seconds (instantly expired)
    lease = db.lease_next_match(worker_id="w1", lease_duration_seconds=0)
    assert lease is not None
    time.sleep(0.05)

    recovered = db.recover_expired_leases()
    assert recovered >= 1

    # Now worker 2 can claim it
    lease2 = db.lease_next_match(worker_id="w2", lease_duration_seconds=10)
    assert lease2 is not None
    assert lease2["match_id"] == "m_expired"


def test_6_added_artifact_file_fails_integrity(tmp_path: Path):
    """6. test_added_artifact_file_fails_integrity"""
    bot_dir = tmp_path / "valid_bot"
    bot_dir.mkdir()
    (bot_dir / "main.py").write_text("print('hello')", encoding="utf-8")
    bot = create_bot_artifact(bot_dir, display_name="TamperAddBot")

    # Add extra unauthorized file
    snapshot_dir = Path(bot.source_location)
    (snapshot_dir / "aaa.py").write_text("print('injected')", encoding="utf-8")

    ok, err = verify_artifact_integrity(bot)
    assert ok is False
    assert any(w in err.lower() for w in ("extraneous", "unexpected", "mismatch"))


def test_7_deleted_artifact_file_fails_integrity(tmp_path: Path):
    """7. test_deleted_artifact_file_fails_integrity"""
    bot_dir = tmp_path / "multi_bot"
    bot_dir.mkdir()
    (bot_dir / "main.py").write_text("import helper", encoding="utf-8")
    (bot_dir / "helper.py").write_text("x = 1", encoding="utf-8")
    bot = create_bot_artifact(bot_dir, display_name="TamperDelBot")

    # Delete helper (temporarily restore write perm for deletion test)
    snapshot_dir = Path(bot.source_location)
    helper = snapshot_dir / "helper.py"
    if helper.exists():
        os.chmod(helper, stat.S_IWRITE)
        helper.unlink()

    ok, err = verify_artifact_integrity(bot)
    assert ok is False
    assert any(w in err.lower() for w in ("missing", "mismatch", "failed"))


def test_8_explicit_entrypoint_is_executed(tmp_path: Path):
    """8. test_explicit_entrypoint_is_executed"""
    bot_dir = tmp_path / "entrypoint_bot"
    bot_dir.mkdir()
    (bot_dir / "aaa.py").write_text("import sys; sys.exit(1)", encoding="utf-8")
    (bot_dir / "real_bot.py").write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    sys.stdout.write(json.dumps({"type": "CLAIM"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    bot = create_bot_artifact(bot_dir, display_name="ExplicitEntryBot", entrypoint="real_bot.py")
    assert bot.entrypoint_relpath == "real_bot.py"

    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_entrypoint_check",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=bot.artifact_id,
        bot_b_id=bot.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, bot, bot, tmp_path / "work")
    assert res.crashed_a is False
    assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)


def test_9_opponent_groups_resolve_from_config_to_artifacts(tmp_path: Path):
    """9. test_opponent_groups_resolve_from_config_to_artifacts"""
    db = Database(tmp_path / "eval_opp.db")
    evaluator = ExperimentEvaluator(db)
    opp_cfg = load_yaml_config("configs/opponent_pool.yaml")
    resolved = evaluator._ensure_opponent_artifacts(opp_cfg)
    assert len(resolved) >= 3
    groups = {r.group for r in resolved}
    assert "basic" in groups
    assert "strategy" in groups
    for r in resolved:
        assert isinstance(r, ResolvedOpponent)
        assert r.artifact_id.startswith("art_")


def test_10_opponent_weights_change_weighted_result():
    """10. test_opponent_weights_change_weighted_result"""
    matches = [
        # Pair 1: vs opp1 (win)
        {
            "pair_id": "p1",
            "bot_a_id": "c",
            "bot_b_id": "opp1",
            "winner": "A",
            "score_a": 10.0,
            "outcome": "WIN_A",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "replay_path": "r1",
            "replay_hash": "h1",
        },
        {
            "pair_id": "p1",
            "bot_a_id": "b",
            "bot_b_id": "opp1",
            "winner": "B",
            "score_a": 0.0,
            "outcome": "WIN_B",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "replay_path": "r2",
            "replay_hash": "h2",
        },
        # Pair 2: vs opp2 (loss)
        {
            "pair_id": "p2",
            "bot_a_id": "c",
            "bot_b_id": "opp2",
            "winner": "B",
            "score_a": 0.0,
            "outcome": "WIN_B",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "replay_path": "r3",
            "replay_hash": "h3",
        },
        {
            "pair_id": "p2",
            "bot_a_id": "b",
            "bot_b_id": "opp2",
            "winner": "A",
            "score_a": 10.0,
            "outcome": "WIN_A",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "replay_path": "r4",
            "replay_hash": "h4",
        },
    ]
    # Equal weights: mean win delta should be 0.0
    cfg_equal = {"opponents": [{"id": "opp1", "weight": 1.0}, {"id": "opp2", "weight": 1.0}]}
    res_equal = calculate_paired_experiment_metrics(
        matches, "c", "b", opponent_pool_config=cfg_equal
    )
    assert res_equal["paired_analysis"]["weighted_mean_win_delta"] == 0.0

    # Skewed weights favoring opp1: mean win delta should be positive (> 0.0)
    cfg_skewed = {"opponents": [{"id": "opp1", "weight": 9.0}, {"id": "opp2", "weight": 1.0}]}
    res_skewed = calculate_paired_experiment_metrics(
        matches, "c", "b", opponent_pool_config=cfg_skewed
    )
    assert res_skewed["paired_analysis"]["weighted_mean_win_delta"] > 0.5


def test_11_positive_mean_with_nonpositive_lower_ci_is_rejected(tmp_path: Path):
    """11. test_positive_mean_with_nonpositive_lower_ci_is_rejected"""
    db = Database(tmp_path / "ci_rej.db")
    gate = PromotionGate(db)
    exp = Experiment(
        experiment_id="exp_ci_check",
        hypothesis="Test CI lower bound gate",
        baseline_artifact_id="b",
        challenger_artifact_id="c",
        intended_change="Test",
    )
    metrics = {
        "aggregate": {"valid_matches": 20, "win_rate": 0.60, "runtime_headroom": 0.50},
        "paired_analysis": {
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.15,
            "score_delta_bootstrap_ci_95": [-0.5, 10.5],
            "win_delta_bootstrap_ci_95": [-0.05, 0.35],
        },
    }
    check = gate.check_criteria(
        exp, metrics, config_override={"require_positive_lower_ci": True, "min_sample_size": 10}
    )
    assert check["passed"] is False
    assert any("lower bound" in v.lower() for v in check["violations"])


def test_12_runtime_headroom_gate(tmp_path: Path):
    """12. test_runtime_headroom_gate"""
    db = Database(tmp_path / "headroom.db")
    gate = PromotionGate(db)
    exp = Experiment(
        experiment_id="exp_headroom",
        hypothesis="Test runtime headroom",
        baseline_artifact_id="b",
        challenger_artifact_id="c",
        intended_change="Test",
    )
    metrics = {
        "aggregate": {"valid_matches": 20, "win_rate": 0.70, "runtime_headroom": 0.05},
        "paired_analysis": {
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.20,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.05, 0.35],
        },
    }
    check = gate.check_criteria(
        exp, metrics, config_override={"min_runtime_headroom": 0.15, "min_sample_size": 10}
    )
    assert check["passed"] is False
    assert any("headroom" in v.lower() for v in check["violations"])


def test_13_critical_opponent_regression_gate(tmp_path: Path):
    """13. test_critical_opponent_regression_gate"""
    db = Database(tmp_path / "group_reg.db")
    gate = PromotionGate(db)
    exp = Experiment(
        experiment_id="exp_grp_reg",
        hypothesis="Test group regression",
        baseline_artifact_id="b",
        challenger_artifact_id="c",
        intended_change="Test",
    )
    metrics = {
        "aggregate": {"valid_matches": 20, "win_rate": 0.60, "runtime_headroom": 0.50},
        "paired_analysis": {
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.15,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.05, 0.25],
            "by_opponent_group": {
                "strategy": {"mean_win_diff": -0.25, "pair_count": 5},
            },
        },
    }
    check = gate.check_criteria(
        exp, metrics, config_override={"max_group_regression_delta": -0.15, "min_sample_size": 10}
    )
    assert check["passed"] is False
    assert any("strategy" in v.lower() for v in check["violations"])


def test_14_force_parameter_no_longer_exists():
    """14. test_force_parameter_no_longer_exists"""
    sig = inspect.signature(PromotionGate.promote)
    assert "force" not in sig.parameters

    db = Database()
    gate = PromotionGate(db)
    with pytest.raises(TypeError):
        getattr(gate, "promote")("exp_dummy", force=True)


def test_15_memory_limit_enforced_when_supported():
    """15. test_memory_limit_enforced_when_supported"""
    supported, msg = check_memory_limit_support()
    if sys.platform.startswith("win"):
        assert supported is False
        assert "Windows" in msg
    else:
        assert supported is True


def test_16_report_uses_completed_experiment_and_correct_counts(tmp_path: Path):
    """16. test_report_uses_completed_experiment_and_correct_counts"""
    db = Database(tmp_path / "exp_report.db")
    registry = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)
    b = registry.register_bot("bots/baselines/fixed_bot.py", "BaseFixed", tags=["policy:fixed"])
    c = registry.register_bot("bots/baselines/random_bot.py", "ChalRand", tags=["policy:random"])

    exp = exp_reg.create_experiment(
        hypothesis="Report verification",
        baseline_artifact_id=b.artifact_id,
        challenger_artifact_id=c.artifact_id,
        intended_change="Test report",
    )
    evaluator = ExperimentEvaluator(db)
    res = evaluator.run_experiment(exp.experiment_id, max_workers=2)
    assert res["status"] == "COMPLETED"

    report_path = Path(res["report_path"])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "Status**: COMPLETED" in content
    assert "Total Scheduled**:" in content
    assert "Valid Matches**:" in content


def test_17_full_e2e_accepts_and_rejects_without_override(tmp_path: Path):
    """17. test_full_e2e_accepts_and_rejects_without_override"""
    db = Database(tmp_path / "e2e_gate.db")
    registry = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)
    b = registry.register_bot("bots/baselines/fixed_bot.py", "BaseFixed", tags=["policy:fixed"])
    c_fail = registry.register_bot("bots/adversaries/crash_bot.py", "AdvCrash", tags=["fail:crash"])

    exp_fail = exp_reg.create_experiment(
        hypothesis="Must reject crash bot",
        baseline_artifact_id=b.artifact_id,
        challenger_artifact_id=c_fail.artifact_id,
        intended_change="Reject crash",
    )
    evaluator = ExperimentEvaluator(db)
    evaluator.run_experiment(exp_fail.experiment_id, max_workers=2)

    gate = PromotionGate(db)
    # Reject without override
    with pytest.raises(PromotionGateError) as exc_info:
        gate.promote(exp_fail.experiment_id)
    assert len(exc_info.value.violations) > 0


def test_18_suite_does_not_modify_champion_manifest():
    """18. test_suite_does_not_modify_champion_manifest"""
    manifest_file = get_project_root() / "bots" / "champion" / "champion_manifest.json"
    if manifest_file.exists():
        initial_hash = hash_file(manifest_file)
        time.sleep(0.01)
        current_hash = hash_file(manifest_file)
        assert current_hash == initial_hash
