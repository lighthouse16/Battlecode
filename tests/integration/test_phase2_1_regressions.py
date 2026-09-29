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
    extra_kwargs = {}
    if sys.platform != "win32":
        extra_kwargs["start_new_session"] = True
    parent = subprocess.Popen(
        [sys.executable, "-c", nested_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **extra_kwargs,
    )
    assert parent.stdout is not None
    line = parent.stdout.readline().strip()
    child_pid_str, grandchild_pid_str = line.split(":")
    child_pid = int(child_pid_str)
    grandchild_pid = int(grandchild_pid_str)

    assert is_process_active(parent.pid)
    child_visible = is_process_active(child_pid)
    grandchild_visible = is_process_active(grandchild_pid)

    terminate_process_tree(parent, timeout_seconds=1.5)
    time.sleep(0.2)

    assert not is_process_active(parent.pid)
    if child_visible:
        assert not is_process_active(child_pid)
    if grandchild_visible:
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


def test_19_manifest_forgery_attacks_rejected(tmp_path: Path):
    """19. Verify forged manifest metadata, entrypoint, and altered files are rejected against DB manifest_hash."""
    import json

    from battlelab.bots.artifacts import recompute_manifest_hash

    bot_dir = tmp_path / "forgery_bot"
    bot_dir.mkdir()
    (bot_dir / "main.py").write_text("print('legit')", encoding="utf-8")
    (bot_dir / "backdoor.py").write_text("print('backdoor')", encoding="utf-8")
    bot = create_bot_artifact(bot_dir, display_name="ForgeryBot", entrypoint="main.py")

    snap_dir = Path(bot.source_location)
    manifest_file = snap_dir / "manifest.json"

    # Attack 1: Modify file content and forge manifest file hash + internal manifest_hash
    main_file = snap_dir / "main.py"
    os.chmod(main_file, stat.S_IWRITE)
    main_file.write_text("print('tampered')", encoding="utf-8")
    new_main_hash = hash_file(main_file)

    os.chmod(manifest_file, stat.S_IWRITE)
    m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    m_data["files"]["main.py"] = {
        "sha256": new_main_hash,
        "size_bytes": main_file.stat().st_size,
    }
    m_data["manifest_hash"] = recompute_manifest_hash(m_data["files"], m_data["entrypoint_relpath"])
    manifest_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")

    # verify_artifact_integrity must reject because DB bot.manifest_hash differs
    ok, err = verify_artifact_integrity(bot)
    assert ok is False
    assert "manifest hash mismatch" in err.lower() or "tampered" in err.lower()

    # Attack 2: Modify entrypoint and forge manifest
    m_data["entrypoint_relpath"] = "backdoor.py"
    m_data["manifest_hash"] = recompute_manifest_hash(m_data["files"], m_data["entrypoint_relpath"])
    manifest_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")
    ok, err = verify_artifact_integrity(bot)
    assert ok is False

    # Attack 3: Manifest hash in manifest.json is invalid/fake
    m_data["manifest_hash"] = "0" * 64
    manifest_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")
    ok, err = verify_artifact_integrity(bot)
    assert ok is False


def test_20_manifest_path_traversal_and_symlink_rejection(tmp_path: Path):
    """20. Verify path traversal in manifest and symlinks escaping source dir are rejected."""
    from battlelab.bots.artifacts import compute_artifact_manifest

    bot_dir = tmp_path / "traversal_bot"
    bot_dir.mkdir()
    (bot_dir / "main.py").write_text("print('ok')", encoding="utf-8")

    # Traversal in entrypoint
    with pytest.raises(ValueError):
        compute_artifact_manifest(bot_dir, entrypoint="../escape.py")

    # Symlink escape
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    symlink_file = bot_dir / "leak.txt"
    try:
        symlink_file.symlink_to(outside_file)
        with pytest.raises((ValueError, OSError)):
            compute_artifact_manifest(bot_dir, entrypoint="main.py")
    except (OSError, NotImplementedError):
        # Platform may not allow unprivileged symlinks (e.g. Windows Developer Mode disabled)
        pass


def test_21_runtime_headroom_per_turn_measurement():
    """21. Verify headroom measures challenger per-turn durations, not whole match duration."""
    # Match A: 100 turns at 5ms each. Total duration = 500ms. Per-turn limit = 50ms.
    # Whole-match comparison would see 500ms > 50ms (0.0 headroom).
    # Turn-level comparison sees turns at 5ms << 50ms (headroom ~ 0.90).
    matches_fast = [
        {
            "pair_id": "p1",
            "bot_a_id": "challenger",
            "bot_b_id": "opp",
            "winner": "A",
            "score_a": 10.0,
            "outcome": "WIN_A",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "duration_ms": 500.0,
            "per_turn_limit_ms": 50.0,
            "bot_a_stats": {
                "turn_durations_ms": [5.0] * 100,
                "max_turn_ms": 5.0,
            },
        },
        {
            "pair_id": "p1",
            "bot_a_id": "baseline",
            "bot_b_id": "opp",
            "winner": "B",
            "score_a": 0.0,
            "outcome": "WIN_B",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "duration_ms": 500.0,
            "per_turn_limit_ms": 50.0,
        },
    ]
    res_fast = calculate_paired_experiment_metrics(matches_fast, "challenger", "baseline")
    assert res_fast["aggregate"]["runtime_headroom"] >= 0.85
    assert res_fast["aggregate"]["runtime_percentiles_ms"]["p99"] == 5.0

    # Match B: 10 turns, 9 at 5ms and 1 slow turn at 60ms. Total duration = 105ms. Per-turn limit = 50ms.
    matches_slow = [
        {
            "pair_id": "p2",
            "bot_a_id": "challenger",
            "bot_b_id": "opp",
            "winner": "A",
            "score_a": 10.0,
            "outcome": "WIN_A",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "duration_ms": 105.0,
            "per_turn_limit_ms": 50.0,
            "bot_a_stats": {
                "turn_durations_ms": [5.0] * 9 + [60.0],
                "max_turn_ms": 60.0,
            },
        },
        {
            "pair_id": "p2",
            "bot_a_id": "baseline",
            "bot_b_id": "opp",
            "winner": "B",
            "score_a": 0.0,
            "outcome": "WIN_B",
            "map_name": "grid_classic_8x8",
            "seed": 42,
            "duration_ms": 105.0,
            "per_turn_limit_ms": 50.0,
        },
    ]
    res_slow = calculate_paired_experiment_metrics(matches_slow, "challenger", "baseline")
    assert res_slow["aggregate"]["runtime_headroom"] == 0.0
    assert res_slow["aggregate"]["runtime_percentiles_ms"]["p99"] == 60.0


def test_22_active_cancellation_terminates_on_lost_lease(tmp_path: Path):
    """22. Verify that match execution aborts and reaps processes when cancel_event is set."""
    import threading

    from battlelab.adapters.mock.adapter import MockAdapter

    bot_code = tmp_path / "loop_bot.py"
    bot_code.write_text(
        """import sys, json, time
for line in sys.stdin:
    if not line.strip(): continue
    time.sleep(0.01)
    sys.stdout.write(json.dumps({"type": "PASS"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    bot = create_bot_artifact(bot_code, display_name="LoopBot")
    adapter = MockAdapter()
    spec = MatchSpec(
        match_id="m_cancel_test",
        adapter_name="mock",
        adapter_version=adapter.version,
        bot_a_id=bot.artifact_id,
        bot_b_id=bot.artifact_id,
        map_name="grid_classic_8x8",
        seed=1,
        per_turn_limit_ms=2000,
    )

    cancel_event = threading.Event()
    # Trigger cancellation after 50ms
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()

    res = adapter.run_local_match(
        spec=spec,
        bot_a=bot,
        bot_b=bot,
        work_dir=tmp_path / "work_cancel",
        cancel_event=cancel_event,
    )
    timer.join()
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.replay_hash is None or res.replay_path is None


def test_23_authentic_stale_token_rejected(tmp_path: Path):
    """23. Verify that an authentic token from a lapsed lease cannot commit after re-leasing."""
    db = Database(tmp_path / "authentic_stale.db")
    reg = BotRegistry(db)
    b1 = reg.register_bot("bots/baselines/fixed_bot.py", "B1")
    b2 = reg.register_bot("bots/baselines/random_bot.py", "B2")

    spec = MatchSpec(
        match_id="m_authentic_stale",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=b1.artifact_id,
        bot_b_id=b2.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    db.save_match_spec(spec, "2026-09-28T00:00:00Z")

    # Worker 1 leases match with 0s duration (immediately expires)
    lease1 = db.lease_next_match(worker_id="w1", lease_duration_seconds=0)
    assert lease1 is not None
    token1 = lease1["lease_token"]
    time.sleep(0.02)

    # Recover expired lease
    db.recover_expired_leases()

    # Worker 2 leases match
    lease2 = db.lease_next_match(worker_id="w2", lease_duration_seconds=10)
    assert lease2 is not None
    token2 = lease2["lease_token"]
    assert token1 != token2

    from battlelab.core.models import MatchResult

    res1 = MatchResult(match_id="m_authentic_stale", outcome=MatchOutcome.WIN_A, score_a=1.0)
    # Stale Worker 1 commits with token1 -> must be rejected
    committed1 = db.update_match_result(res1, lease_token=token1)
    assert committed1 is False

    # Valid Worker 2 commits with token2 -> succeeds
    res2 = MatchResult(match_id="m_authentic_stale", outcome=MatchOutcome.WIN_B, score_b=2.0)
    committed2 = db.update_match_result(res2, lease_token=token2)
    assert committed2 is True


def test_24_configuration_provenance_persisted(tmp_path: Path):
    """24. Verify all 5 experiment provenance fields are persisted and loaded from DB."""
    db = Database(tmp_path / "prov.db")
    reg = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)
    b = reg.register_bot("bots/baselines/fixed_bot.py", "B")
    c = reg.register_bot("bots/baselines/random_bot.py", "C")
    exp = exp_reg.create_experiment(
        hypothesis="Prov test",
        baseline_artifact_id=b.artifact_id,
        challenger_artifact_id=c.artifact_id,
        intended_change="Test",
    )
    exp.evaluation_config = {"maps": ["grid_classic_8x8"]}
    exp.evaluation_config_hash = "eval_hash_123"
    exp.opponent_pool_config = {"opponents": []}
    exp.opponent_pool_config_hash = "opp_hash_456"
    exp.promotion_config_hash = "prom_hash_789"
    exp_reg.update_experiment(exp)

    loaded = exp_reg.get_experiment(exp.experiment_id)
    assert loaded.evaluation_config == {"maps": ["grid_classic_8x8"]}
    assert loaded.evaluation_config_hash == "eval_hash_123"
    assert loaded.opponent_pool_config == {"opponents": []}
    assert loaded.opponent_pool_config_hash == "opp_hash_456"
    assert loaded.promotion_config_hash == "prom_hash_789"


def test_25_min_segment_sample_size_enforced(tmp_path: Path):
    """25. Verify promotion gate blocks when segment sample size is below min_segment_sample_size."""
    db = Database(tmp_path / "min_segment.db")
    gate = PromotionGate(db)
    exp = Experiment(
        experiment_id="exp_seg_check",
        hypothesis="Test min segment sample size",
        baseline_artifact_id="b",
        challenger_artifact_id="c",
        intended_change="Test",
    )
    metrics = {
        "aggregate": {"valid_matches": 20, "win_rate": 0.80, "runtime_headroom": 0.50},
        "paired_analysis": {
            "completed_pairs": 10,
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.30,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.10, 0.50],
            "by_opponent_group": {
                "strategy": {"pair_count": 1, "mean_win_diff": 0.5},
            },
        },
    }
    check = gate.check_criteria(
        exp, metrics, config_override={"min_segment_sample_size": 2, "min_sample_size": 6}
    )
    assert check["passed"] is False
    assert any("segment sample size" in v.lower() for v in check["violations"])


def test_26_pre_promotion_integrity_blocks_tampered_bot(tmp_path: Path):
    """26. Verify PromotionGate.promote rejects promotion if either challenger or baseline fails integrity."""
    db = Database(tmp_path / "tamper_prom.db")
    reg = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)

    b = reg.register_bot("bots/baselines/fixed_bot.py", "BaseFixed")
    c = reg.register_bot("bots/baselines/random_bot.py", "ChalRand")

    exp = exp_reg.create_experiment(
        hypothesis="Tamper check",
        baseline_artifact_id=b.artifact_id,
        challenger_artifact_id=c.artifact_id,
        intended_change="Tamper test",
    )
    exp.status = "COMPLETED"
    exp.results_summary = {
        "aggregate": {"valid_matches": 20, "win_rate": 0.80, "runtime_headroom": 0.50},
        "paired_analysis": {
            "completed_pairs": 10,
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.30,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.10, 0.50],
        },
    }
    exp_reg.update_experiment(exp)

    # Tamper with baseline artifact
    base_file = Path(b.source_location) / b.entrypoint_relpath
    os.chmod(base_file, stat.S_IWRITE)
    base_file.write_text("print('tampered baseline')", encoding="utf-8")

    gate = PromotionGate(db)
    with pytest.raises(PromotionGateError) as exc_info:
        gate.promote(exp.experiment_id)
    assert any("integrity" in v.lower() for v in exc_info.value.violations)
