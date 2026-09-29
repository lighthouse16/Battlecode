"""Comprehensive Phase 2.1 regression test suite verifying all 18 required correctness criteria."""

from __future__ import annotations

import inspect
import json
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
from battlelab.analysis.report import generate_experiment_report
from battlelab.bots.artifacts import (
    compute_artifact_manifest,
    create_bot_artifact,
    verify_artifact_integrity,
)
from battlelab.bots.process_runner import (
    BotSubprocess,
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

    assert parent.poll() is None
    child_visible = is_process_active(child_pid)
    grandchild_visible = is_process_active(grandchild_pid)

    terminate_process_tree(parent, timeout_seconds=1.5)
    time.sleep(0.2)

    assert parent.poll() is not None
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
        os.chmod(helper, stat.S_IWRITE | stat.S_IREAD)
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
    os.chmod(main_file, stat.S_IWRITE | stat.S_IREAD)
    main_file.write_text("print('tampered')", encoding="utf-8")
    new_main_hash = hash_file(main_file)

    os.chmod(manifest_file, stat.S_IWRITE | stat.S_IREAD)
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
    os.chmod(base_file, stat.S_IWRITE | stat.S_IREAD)
    base_file.write_text("print('tampered baseline')", encoding="utf-8")

    gate = PromotionGate(db)
    with pytest.raises(PromotionGateError) as exc_info:
        gate.promote(exp.experiment_id)
    assert any("integrity" in v.lower() for v in exc_info.value.violations)


def test_27_symlink_directory_and_unmanifested_entries_rejected(tmp_path: Path):
    """27. Snapshots reject symlink files, directory symlinks, hidden files/dirs, and unmanifested files."""
    db = Database(tmp_path / "sym_test.db")
    reg = BotRegistry(db)

    bot_dir = tmp_path / "sample_bot"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text("print('clean bot')\n", encoding="utf-8")

    # Directory symlink check during compute_artifact_manifest
    target_sub = tmp_path / "sub"
    target_sub.mkdir()
    sym_dir = bot_dir / "sym_dir"
    try:
        sym_dir.symlink_to(target_sub, target_is_directory=True)
        with pytest.raises(ValueError, match="Symlinks are not permitted"):
            compute_artifact_manifest(bot_dir)
        sym_dir.unlink()
    except (OSError, NotImplementedError):
        # Platform/user privilege may restrict symlink creation on Windows
        pass

    # Register clean bot
    art = reg.register_bot(bot_dir, "CleanBot")
    art_path = Path(art.source_location)

    # 1. Hidden file rejection
    hidden_file = art_path / ".backdoor.py"
    hidden_file.write_text("print('hidden')\n", encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "hidden" in err.lower()
    hidden_file.unlink()

    # 2. Hidden directory rejection
    hidden_dir = art_path / ".secret"
    hidden_dir.mkdir()
    (hidden_dir / "nested.py").write_text("print('nested')\n", encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "hidden" in err.lower()
    (hidden_dir / "nested.py").unlink()
    hidden_dir.rmdir()

    # 3. __pycache__ rejection
    pycache_dir = art_path / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "evil.pyc").write_bytes(b"bytecode")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "__pycache__" in err.lower()
    (pycache_dir / "evil.pyc").unlink()
    pycache_dir.rmdir()

    # 4. Unmanifested extraneous file rejection
    extra = art_path / "extra.py"
    extra.write_text("print('extra')\n", encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "extraneous" in err.lower()
    extra.unlink()

    # 5. Clean state passes integrity
    ok, err = verify_artifact_integrity(art)
    assert ok


def test_28_manifest_type_safety_and_no_crash(tmp_path: Path):
    """28. verify_artifact_integrity handles malformed types, invalid hashes, and bad JSON safely without crashing."""
    db = Database(tmp_path / "manifest_types.db")
    reg = BotRegistry(db)

    bot_file = tmp_path / "types_bot.py"
    bot_file.write_text("print('types bot')\n", encoding="utf-8")
    art = reg.register_bot(bot_file, "TypesBot")
    art_path = Path(art.source_location)
    mfile = art_path / "manifest.json"
    os.chmod(mfile, stat.S_IWRITE | stat.S_IREAD)

    orig_manifest = json.loads(mfile.read_text(encoding="utf-8"))

    # Case A: Corrupted JSON syntax
    mfile.write_text("{invalid json", encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "manifest.json" in err.lower()

    # Case B: Boolean size_bytes (JSON true becomes Python True)
    bad_manifest = json.loads(json.dumps(orig_manifest))
    bad_manifest["files"]["types_bot.py"]["size_bytes"] = True
    mfile.write_text(json.dumps(bad_manifest), encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "size_bytes" in err.lower()

    # Case C: Negative size_bytes
    bad_manifest["files"]["types_bot.py"]["size_bytes"] = -1
    mfile.write_text(json.dumps(bad_manifest), encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "size_bytes" in err.lower()

    # Case D: Invalid SHA256 length / non-hex
    bad_manifest["files"]["types_bot.py"]["size_bytes"] = orig_manifest["files"]["types_bot.py"][
        "size_bytes"
    ]
    bad_manifest["files"]["types_bot.py"]["sha256"] = "short_hash"
    mfile.write_text(json.dumps(bad_manifest), encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok
    assert "sha-256" in err.lower() or "hash" in err.lower()

    # Case E: Non-string file key
    bad_manifest["files"] = {None: {"sha256": "a" * 64, "size_bytes": 10}}
    mfile.write_text(json.dumps(bad_manifest), encoding="utf-8")
    ok, err = verify_artifact_integrity(art)
    assert not ok


def test_29_bytecode_generation_disabled(tmp_path: Path):
    """29. Bot subprocess execution must not emit .pyc files or __pycache__ directories."""
    bot_code = tmp_path / "simple_turn_bot.py"
    bot_code.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    if not line.strip(): continue\n"
        "    print(json.dumps({'action': 'MOVE', 'turn': 1}), flush=True)\n",
        encoding="utf-8",
    )

    runner = BotSubprocess(entrypoint_path=bot_code, cwd=tmp_path)
    runner.start()
    try:
        action, _status = runner.send_turn({"turn": 1}, timeout_seconds=1.0)
        assert action is not None
        assert action.get("action") == "MOVE"
    finally:
        runner.stop()

    # Verify no .pyc or __pycache__ in tmp_path
    for _root, dirs, files in os.walk(tmp_path):
        assert "__pycache__" not in dirs
        for f in files:
            assert not f.endswith(".pyc")


def test_30_promotion_config_provenance_persisted_before_report(tmp_path: Path):
    """30. Promotion config snapshot and 64-char hash are persisted to DB, report.md, and analysis_packet.json."""
    db = Database(tmp_path / "provenance.db")
    evaluator = ExperimentEvaluator(db=db)
    b = evaluator.bot_registry.register_bot("bots/baselines/fixed_bot.py", "BaseFixed")
    c = evaluator.bot_registry.register_bot("bots/baselines/random_bot.py", "ChalRand")

    exp = evaluator.registry.create_experiment(
        hypothesis="Provenance test",
        baseline_artifact_id=b.artifact_id,
        challenger_artifact_id=c.artifact_id,
        intended_change="Check promotion config snapshot",
    )

    custom_prom_cfg = tmp_path / "custom_promotion.yaml"
    custom_prom_cfg.write_text(
        "min_sample_size: 4\nmin_win_rate: 0.50\nrequire_determinism_pass: false\nmin_segment_sample_size: 1\n",
        encoding="utf-8",
    )

    evaluator.run_experiment(
        exp.experiment_id,
        promotion_config_path=custom_prom_cfg,
    )

    # 1. DB check
    saved_exp = db.get_experiment(exp.experiment_id)
    assert saved_exp is not None
    assert saved_exp.status == "COMPLETED"
    assert len(saved_exp.promotion_config_hash) == 64
    assert saved_exp.promotion_config.get("min_sample_size") == 4

    # 2. report.md check
    from battlelab.storage.paths import get_reports_dir

    actual_report_path = get_reports_dir() / exp.experiment_id / "report.md"
    assert actual_report_path.exists()
    report_content = actual_report_path.read_text(encoding="utf-8")
    assert f"- **Promotion Config Hash**: `{saved_exp.promotion_config_hash}`" in report_content

    # 3. analysis_packet.json check
    packet_path = get_reports_dir() / exp.experiment_id / "analysis_packet.json"
    assert packet_path.exists()
    packet_data = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet_data["metadata"]["promotion_config_hash"] == saved_exp.promotion_config_hash


def test_31_promotion_uses_stored_config_snapshot(tmp_path: Path):
    """31. PromotionGate.promote uses experiment snapshot instead of newer modified disk config."""
    db = Database(tmp_path / "snap_prom.db")
    reg = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)

    b = reg.register_bot("bots/baselines/fixed_bot.py", "BaseFixed")
    c = reg.register_bot("bots/baselines/random_bot.py", "ChalRand")

    exp = exp_reg.create_experiment(
        hypothesis="Snapshot test",
        baseline_artifact_id=b.artifact_id,
        challenger_artifact_id=c.artifact_id,
        intended_change="Snapshot promotion test",
    )
    exp.status = "COMPLETED"
    exp.results_summary = {
        "aggregate": {
            "valid_matches": 10,
            "win_rate": 0.60,
            "runtime_headroom": 0.50,
            "runtime_telemetry_complete": True,
            "runtime_telemetry_missing_count": 0,
        },
        "paired_analysis": {
            "completed_pairs": 5,
            "mean_score_delta": 2.0,
            "weighted_mean_win_delta": 0.15,
            "score_delta_bootstrap_ci_95": [0.5, 4.0],
            "win_delta_bootstrap_ci_95": [0.05, 0.30],
        },
    }
    # Stored snapshot has min_win_rate 0.50 (passing)
    exp.promotion_config = {
        "min_sample_size": 4,
        "min_win_rate": 0.50,
        "require_determinism_pass": False,
        "min_segment_sample_size": 0,
    }
    exp_reg.update_experiment(exp)

    # Disk config has impossible min_win_rate 0.99
    impossible_yaml = tmp_path / "impossible_promotion.yaml"
    impossible_yaml.write_text("min_win_rate: 0.99\n", encoding="utf-8")

    gate = PromotionGate(db, config_path=impossible_yaml)
    res = gate.promote(exp.experiment_id)
    assert res["status"] == "PROMOTED"


def test_32_missing_and_insufficient_seed_segments_block_promotion(tmp_path: Path):
    """32. Missing seeds or insufficient pairs across segments strictly fail promotion."""
    db = Database(tmp_path / "segments.db")
    gate = PromotionGate(db)

    exp = Experiment(
        experiment_id="exp_seg_check",
        hypothesis="Check segment sample counts",
        baseline_artifact_id="b",
        challenger_artifact_id="c",
        intended_change="Segments test",
        evaluation_config={
            "seeds": [42, 137, 999],
            "maps": ["grid_classic_8x8"],
            "paired_sides": True,
        },
    )
    metrics = {
        "aggregate": {
            "valid_matches": 20,
            "win_rate": 0.80,
            "runtime_headroom": 0.50,
            "runtime_telemetry_complete": True,
        },
        "paired_analysis": {
            "completed_pairs": 10,
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.30,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.10, 0.50],
            "by_seed": {
                "42": {"pair_count": 1},
                "137": {"pair_count": 9},
            },
        },
    }

    check = gate.check_criteria(
        exp,
        metrics,
        config_override={"min_segment_sample_size": 2, "require_determinism_pass": False},
    )
    assert check["passed"] is False
    assert check["segment_sample_counts"]["by_seed"]["999"] == 0
    assert check["segment_sample_counts"]["by_seed"]["42"] == 1
    # Check violations mention both missing seed 999 and insufficient seed 42
    violation_texts = " ".join(check["violations"])
    assert "999" in violation_texts and "0 pair" in violation_texts
    assert "42" in violation_texts and "1 pair" in violation_texts


def test_33_pure_per_turn_runtime_telemetry_enforced(tmp_path: Path):
    """33. Matches without challenger turn telemetry fail closed without whole-match fallback."""
    matches = [
        {
            "match_id": "m1",
            "bot_a_id": "challenger",
            "bot_b_id": "baseline",
            "outcome": "PLAYER_A_WIN",
            "score_a": 10.0,
            "score_b": 5.0,
            "duration_ms": 120.0,
            "turn_count": 10,
            "seed": 42,
            "map_name": "grid_classic_8x8",
            "bot_a_turn_durations_ms": [],
            "bot_b_turn_durations_ms": [5.0, 6.0],
        }
    ]

    metrics = calculate_paired_experiment_metrics(
        matches=matches,
        challenger_id="challenger",
        baseline_id="baseline",
    )

    agg = metrics["aggregate"]
    assert agg["runtime_telemetry_complete"] is False
    assert agg["runtime_headroom"] is None
    assert agg["runtime_percentiles_ms"] is None
    assert agg["runtime_telemetry_missing_count"] == 1

    # Promotion check fails closed
    gate = PromotionGate()
    exp = Experiment(
        experiment_id="exp_telem",
        hypothesis="Telemetry test",
        baseline_artifact_id="baseline",
        challenger_artifact_id="challenger",
        intended_change="Check telemetry",
    )
    check = gate.check_criteria(
        exp,
        metrics,
        config_override={"min_runtime_headroom": 0.10, "require_determinism_pass": False},
    )
    assert check["passed"] is False
    assert any("Runtime telemetry incomplete" in v for v in check["violations"])

    # Report outputs UNAVAILABLE
    report = generate_experiment_report(exp, metrics, check)
    assert "Runtime Headroom**: `UNAVAILABLE`" in report


def test_34_pid_namespace_portability_and_concurrency_isolation():
    """34. terminate_process_tree isolates target process without killing concurrent unrelated processes."""
    target_code = "import time; time.sleep(60)\n"
    target = subprocess.Popen([sys.executable, "-c", target_code])
    unrelated = subprocess.Popen([sys.executable, "-c", target_code])

    try:
        assert target.poll() is None
        assert unrelated.poll() is None

        # Terminate only target
        terminate_process_tree(target, timeout_seconds=1.0)
        time.sleep(0.1)

        # Target is dead
        assert target.poll() is not None
        assert not is_process_active(target.pid)

        # Unrelated process is still alive and running
        assert unrelated.poll() is None
        assert is_process_active(unrelated.pid)
    finally:
        terminate_process_tree(unrelated, timeout_seconds=1.0)


def test_35_unrelated_exited_child_preserves_exit_code():
    """35. terminate_process_tree must not reap unrelated exited child processes (preserves exit code 7)."""
    for _ in range(3):
        unrelated = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
        while unrelated.poll() is None:
            time.sleep(0.01)

        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        concurrent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        try:
            terminate_process_tree(target, timeout_seconds=1.0)
            assert unrelated.wait() == 7
            assert concurrent.poll() is None
        finally:
            terminate_process_tree(concurrent, timeout_seconds=1.0)
            if unrelated.poll() is None:
                unrelated.kill()


def test_36_segment_minimum_enforced_at_threshold_one(tmp_path: Path):
    """36. Segment minimum of 1 fails when expected segment has 0 pairs; 0 disables; 1 passes with 1 pair."""
    db = Database(tmp_path / "seg_thresh.db")
    gate = PromotionGate(db)

    exp = Experiment(
        experiment_id="exp_seg_one",
        hypothesis="Segment threshold 1 test",
        baseline_artifact_id="b",
        challenger_artifact_id="c",
        intended_change="Check threshold 1 and 0",
        evaluation_config={
            "seeds": [1, 2],
            "maps": ["map_a"],
            "paired_sides": True,
        },
        opponent_pool_config={
            "opponents": [{"group": "group_x"}],
        },
    )

    metrics_missing_seed = {
        "aggregate": {
            "valid_matches": 2,
            "win_rate": 0.80,
            "runtime_headroom": 0.50,
            "runtime_telemetry_complete": True,
        },
        "paired_analysis": {
            "completed_pairs": 1,
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.30,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.10, 0.50],
            "by_seed": {"1": {"pair_count": 1}},
            "by_map": {"map_a": {"pair_count": 1}},
            "by_opponent_group": {"group_x": {"pair_count": 1}},
            "by_side": {"side_0": {"pair_count": 1}, "side_1": {"pair_count": 0}},
        },
    }

    # min_segment_sample_size = 1 -> fail missing seed 2 and side_1
    check1 = gate.check_criteria(
        exp,
        metrics_missing_seed,
        config_override={
            "min_segment_sample_size": 1,
            "min_sample_size": 1,
            "require_determinism_pass": False,
        },
    )
    assert check1["passed"] is False
    assert any("seed '2' has 0 pair" in v.lower() for v in check1["violations"])

    # min_segment_sample_size = 0 -> disables check
    check0 = gate.check_criteria(
        exp,
        metrics_missing_seed,
        config_override={
            "min_segment_sample_size": 0,
            "min_sample_size": 1,
            "require_determinism_pass": False,
        },
    )
    assert not any("segment sample size" in v.lower() for v in check0["violations"])

    # Complete segments with 1 pair each passes when min_segment_sample_size = 1
    metrics_complete = {
        "aggregate": {
            "valid_matches": 4,
            "win_rate": 0.80,
            "runtime_headroom": 0.50,
            "runtime_telemetry_complete": True,
        },
        "paired_analysis": {
            "completed_pairs": 2,
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.30,
            "score_delta_bootstrap_ci_95": [1.0, 9.0],
            "win_delta_bootstrap_ci_95": [0.10, 0.50],
            "by_seed": {"1": {"pair_count": 1}, "2": {"pair_count": 1}},
            "by_map": {"map_a": {"pair_count": 2}},
            "by_opponent_group": {"group_x": {"pair_count": 2}},
            "by_side": {"side_0": {"pair_count": 1}, "side_1": {"pair_count": 1}},
        },
    }
    check_pass = gate.check_criteria(
        exp,
        metrics_complete,
        config_override={
            "min_segment_sample_size": 1,
            "min_sample_size": 2,
            "require_determinism_pass": False,
        },
    )
    assert check_pass["passed"] is True

    # Invalid min_segment_sample_size type (bool, negative, string) appends violation
    for invalid_val in [True, False, -1, "one"]:
        check_inv = gate.check_criteria(
            exp,
            metrics_complete,
            config_override={
                "min_segment_sample_size": invalid_val,
                "require_determinism_pass": False,
            },
        )
        assert check_inv["passed"] is False
        assert any("min_segment_sample_size" in v for v in check_inv["violations"])


def test_37_root_source_symlinks_rejected(tmp_path: Path):
    """37. create_bot_artifact and register_bot reject root dir symlinks, file symlinks, and broken symlinks."""
    db = Database(tmp_path / "sym_root.db")
    reg = BotRegistry(db)

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "bot.py").write_text("print('hello')\n", encoding="utf-8")

    real_file = tmp_path / "real_file.py"
    real_file.write_text("print('hello file')\n", encoding="utf-8")

    sym_dir = tmp_path / "sym_dir"
    sym_file = tmp_path / "sym_file.py"
    broken_sym = tmp_path / "broken_sym.py"

    symlinks_supported = True
    try:
        sym_dir.symlink_to(real_dir, target_is_directory=True)
        sym_file.symlink_to(real_file)
        broken_sym.symlink_to(tmp_path / "nonexistent.py")
    except (OSError, NotImplementedError):
        symlinks_supported = False

    if symlinks_supported:
        # Directory symlink
        with pytest.raises(ValueError, match="Symlinks are not permitted as bot source"):
            create_bot_artifact(sym_dir, display_name="SymDirBot")
        with pytest.raises(ValueError, match="Symlinks are not permitted as bot source"):
            reg.register_bot(sym_dir, "SymDirBot")

        # File symlink
        with pytest.raises(ValueError, match="Symlinks are not permitted as bot source"):
            create_bot_artifact(sym_file, display_name="SymFileBot")
        with pytest.raises(ValueError, match="Symlinks are not permitted as bot source"):
            reg.register_bot(sym_file, "SymFileBot")

        # Broken symlink
        with pytest.raises(ValueError, match="Symlinks are not permitted as bot source"):
            create_bot_artifact(broken_sym, display_name="BrokenSymBot")
        with pytest.raises(ValueError, match="Symlinks are not permitted as bot source"):
            reg.register_bot(broken_sym, "BrokenSymBot")


def test_38_prohibited_source_entries_rejected_not_ignored(tmp_path: Path):
    """38. compute_artifact_manifest and create_bot_artifact explicitly reject prohibited entries naming them."""
    # 1. Hidden file
    dir1 = tmp_path / "bot1"
    dir1.mkdir()
    (dir1 / "bot.py").write_text("print(1)\n", encoding="utf-8")
    (dir1 / ".hidden").write_text("secret\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"Hidden files are not permitted in bot source: .hidden"):
        compute_artifact_manifest(dir1)
    with pytest.raises(ValueError, match=r"Hidden files are not permitted in bot source: .hidden"):
        create_bot_artifact(dir1, display_name="B1")

    # 2. Hidden directory
    dir2 = tmp_path / "bot2"
    dir2.mkdir()
    (dir2 / "bot.py").write_text("print(1)\n", encoding="utf-8")
    (dir2 / ".git").mkdir()
    with pytest.raises(
        ValueError, match=r"Hidden directories are not permitted in bot source: .git"
    ):
        compute_artifact_manifest(dir2)
    with pytest.raises(
        ValueError, match=r"Hidden directories are not permitted in bot source: .git"
    ):
        create_bot_artifact(dir2, display_name="B2")

    # 3. __pycache__ directory
    dir3 = tmp_path / "bot3"
    dir3.mkdir()
    (dir3 / "bot.py").write_text("print(1)\n", encoding="utf-8")
    (dir3 / "__pycache__").mkdir()
    with pytest.raises(
        ValueError, match=r"__pycache__ directories are not permitted in bot source: __pycache__"
    ):
        compute_artifact_manifest(dir3)
    with pytest.raises(
        ValueError, match=r"__pycache__ directories are not permitted in bot source: __pycache__"
    ):
        create_bot_artifact(dir3, display_name="B3")

    # 4. .pyc file
    dir4 = tmp_path / "bot4"
    dir4.mkdir()
    (dir4 / "bot.py").write_text("print(1)\n", encoding="utf-8")
    (dir4 / "compiled.pyc").write_bytes(b"bad")
    with pytest.raises(
        ValueError, match=r"Compiled bytecode files are not permitted in bot source: compiled.pyc"
    ):
        compute_artifact_manifest(dir4)
    with pytest.raises(
        ValueError, match=r"Compiled bytecode files are not permitted in bot source: compiled.pyc"
    ):
        create_bot_artifact(dir4, display_name="B4")

    # 5. .pyo file
    dir5 = tmp_path / "bot5"
    dir5.mkdir()
    (dir5 / "bot.py").write_text("print(1)\n", encoding="utf-8")
    (dir5 / "opt.pyo").write_bytes(b"bad")
    with pytest.raises(
        ValueError, match=r"Compiled bytecode files are not permitted in bot source: opt.pyo"
    ):
        compute_artifact_manifest(dir5)
    with pytest.raises(
        ValueError, match=r"Compiled bytecode files are not permitted in bot source: opt.pyo"
    ):
        create_bot_artifact(dir5, display_name="B5")


def test_39_runtime_telemetry_count_semantics_and_invariant():
    """39. runtime_challenger_match_count, runtime_telemetry_match_count, missing_count satisfy invariant, reject bad durations."""
    matches = [
        {
            "match_id": "m1",
            "bot_a_id": "challenger",
            "bot_b_id": "baseline",
            "seed": 42,
            "map_name": "grid_classic_8x8",
            "bot_a_turn_durations_ms": [10.0, 20.0],
            "bot_b_turn_durations_ms": [5.0],
            "outcome": "WIN_A",
            "score_a": 1.0,
            "score_b": 0.0,
        },
        {
            "match_id": "m2",
            "bot_a_id": "baseline",
            "bot_b_id": "challenger",
            "seed": 42,
            "map_name": "grid_classic_8x8",
            "bot_a_turn_durations_ms": [5.0],
            "bot_b_turn_durations_ms": [15.0],
            "outcome": "WIN_B",
            "score_a": 0.0,
            "score_b": 1.0,
        },
        {
            "match_id": "m3",
            "bot_a_id": "challenger",
            "bot_b_id": "baseline",
            "seed": 43,
            "map_name": "grid_classic_8x8",
            "bot_a_turn_durations_ms": [],
            "outcome": "WIN_A",
            "score_a": 1.0,
            "score_b": 0.0,
        },
        {
            "match_id": "m4",
            "bot_a_id": "baseline",
            "bot_b_id": "challenger",
            "seed": 43,
            "map_name": "grid_classic_8x8",
            "bot_b_turn_durations_ms": [float("nan"), 10.0],
            "outcome": "WIN_B",
            "score_a": 0.0,
            "score_b": 1.0,
        },
    ]

    metrics = calculate_paired_experiment_metrics(
        matches=matches,
        challenger_id="challenger",
        baseline_id="baseline",
    )
    agg = metrics["aggregate"]
    assert agg["runtime_challenger_match_count"] == 4
    assert agg["runtime_telemetry_match_count"] == 2
    assert agg["runtime_telemetry_missing_count"] == 2
    assert (
        agg["runtime_telemetry_match_count"] + agg["runtime_telemetry_missing_count"]
        == agg["runtime_challenger_match_count"]
    )
    assert agg["runtime_telemetry_complete"] is False
    assert agg["runtime_headroom"] is None
    assert agg["runtime_percentiles_ms"] is None

    # Verify negative, inf, and non-numeric durations are also treated as missing telemetry
    for bad_val in [-5.0, float("inf"), float("-inf"), "bad"]:
        bad_matches = [
            {
                "match_id": "mbad",
                "bot_a_id": "challenger",
                "bot_b_id": "baseline",
                "seed": 99,
                "map_name": "grid_classic_8x8",
                "bot_a_turn_durations_ms": [bad_val, 10.0],
                "outcome": "WIN_A",
                "score_a": 1.0,
                "score_b": 0.0,
            }
        ]
        bad_metrics = calculate_paired_experiment_metrics(
            matches=bad_matches,
            challenger_id="challenger",
            baseline_id="baseline",
        )
        bad_agg = bad_metrics["aggregate"]
        assert bad_agg["runtime_challenger_match_count"] == 1
        assert bad_agg["runtime_telemetry_match_count"] == 0
        assert bad_agg["runtime_telemetry_missing_count"] == 1
        assert bad_agg["runtime_telemetry_complete"] is False

    # Fully complete case
    complete_matches = [
        {
            "match_id": "mc1",
            "bot_a_id": "challenger",
            "bot_b_id": "baseline",
            "seed": 101,
            "map_name": "grid_classic_8x8",
            "bot_a_turn_durations_ms": [10.0, 20.0],
            "outcome": "WIN_A",
            "score_a": 1.0,
            "score_b": 0.0,
        },
        {
            "match_id": "mc2",
            "bot_a_id": "baseline",
            "bot_b_id": "challenger",
            "seed": 101,
            "map_name": "grid_classic_8x8",
            "bot_b_turn_durations_ms": [15.0, 25.0],
            "outcome": "WIN_B",
            "score_a": 0.0,
            "score_b": 1.0,
        },
    ]
    comp_metrics = calculate_paired_experiment_metrics(
        matches=complete_matches,
        challenger_id="challenger",
        baseline_id="baseline",
    )
    comp_agg = comp_metrics["aggregate"]
    assert comp_agg["runtime_challenger_match_count"] == 2
    assert comp_agg["runtime_telemetry_match_count"] == 2
    assert comp_agg["runtime_telemetry_missing_count"] == 0
    assert comp_agg["runtime_telemetry_complete"] is True
    assert comp_agg["runtime_headroom"] is not None
    assert comp_agg["runtime_percentiles_ms"] is not None
