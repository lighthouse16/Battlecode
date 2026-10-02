"""Phase 2 tests: Paired opponent evaluation, determinism verification, hardened promotion gates, and rollback."""

from pathlib import Path

import pytest

from battlelab.bots.registry import BotRegistry
from battlelab.core.errors import PromotionGateError
from battlelab.core.models import Experiment
from battlelab.experiments.evaluator import ExperimentEvaluator
from battlelab.experiments.promotion import REQUIRED_OVERRIDE_ACKNOWLEDGEMENT, PromotionGate
from battlelab.experiments.registry import ExperimentRegistry
from battlelab.storage.database import Database


def test_paired_opponent_evaluation_and_metrics(tmp_path: Path):
    """Verify paired evaluation joins by pair_id, respects opponent pool, and computes bootstrap difference."""
    db_file = tmp_path / "paired_eval.db"
    db = Database(db_file)
    registry = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)

    baseline = registry.register_bot(
        source_path="bots/baselines/random_bot.py",
        display_name="BaselineRandom",
        tags=["policy:random"],
    )
    challenger = registry.register_bot(
        source_path="bots/challengers/challenger_v1.py",
        display_name="ChallengerGreedy",
        tags=["policy:resource"],
    )

    exp = exp_reg.create_experiment(
        hypothesis="Greedy tile harvesting outperforms random baseline",
        baseline_artifact_id=baseline.artifact_id,
        challenger_artifact_id=challenger.artifact_id,
        intended_change="Navigate to adjacent unclaimed tiles",
    )

    evaluator = ExperimentEvaluator(db)
    res = evaluator.run_experiment(exp.experiment_id, max_workers=2)

    assert res["status"] == "COMPLETED"
    exp_completed = exp_reg.get_experiment(exp.experiment_id)
    assert exp_completed.results_summary is not None

    summary = exp_completed.results_summary
    paired = summary.get("paired_analysis", {})
    assert "mean_score_delta" in paired or "mean_score_diff" in paired
    assert "paired_bootstrap_ci_95" in paired
    assert "by_opponent_group" in paired

    # Confirm all matches in experiment have stable pair_ids
    matches = db.list_matches_by_experiment(exp.experiment_id)
    assert len(matches) > 0
    assert all(m.get("pair_id") is not None for m in matches)


def test_multi_seed_determinism_check(tmp_path: Path):
    """Verify multi-seed determinism passes for deterministic bot and fails for nondeterministic bot."""
    db = Database(tmp_path / "det.db")
    registry = BotRegistry(db)
    gate = PromotionGate(db)

    # 1. Deterministic bot passes
    det_bot = registry.register_bot(
        source_path="bots/baselines/fixed_bot.py",
        display_name="DeterministicBot",
        tags=["policy:fixed"],
    )
    ok_det, err_det = gate._verify_multi_seed_determinism(det_bot.artifact_id)
    assert ok_det is True
    assert "determinism verified" in err_det.lower()

    # 2. Nondeterministic bot fails
    nondet_bot = registry.register_bot(
        source_path="bots/adversaries/nondeterministic_bot.py",
        display_name="NonDeterministicBot",
        tags=["fail:nondeterministic"],
    )
    ok_nondet, err_nondet = gate._verify_multi_seed_determinism(nondet_bot.artifact_id)
    assert ok_nondet is False
    assert "discrepancy" in err_nondet.lower() or "divergence" in err_nondet.lower()


def test_hardened_promotion_gates_and_override_audit(tmp_path: Path):
    """Verify failing candidate is rejected, override requires explicit flags, and rollback is audited."""
    db_file = tmp_path / "promo_test.db"
    db = Database(db_file)
    registry = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)
    gate = PromotionGate(db)

    baseline = registry.register_bot(
        source_path="bots/baselines/random_bot.py",
        display_name="BaselineRandom",
        tags=["policy:random"],
    )
    adversary_crash = registry.register_bot(
        source_path="bots/adversaries/crash_bot.py",
        display_name="AdversaryCrash",
        tags=["fail:crash"],
    )

    exp = exp_reg.create_experiment(
        hypothesis="Crash bot must fail promotion gates",
        baseline_artifact_id=baseline.artifact_id,
        challenger_artifact_id=adversary_crash.artifact_id,
        intended_change="Injected crash",
    )

    evaluator = ExperimentEvaluator(db)
    evaluator.run_experiment(exp.experiment_id, max_workers=2)

    # 1. Standard promotion fails
    with pytest.raises(PromotionGateError) as exc_info:
        gate.promote(exp.experiment_id, dry_run=False)
    assert len(exc_info.value.violations) > 0

    # 2. Override fails without full explicit acknowledgement and reason
    with pytest.raises(PromotionGateError):
        gate.promote(
            exp.experiment_id,
            dry_run=False,
            actor="researcher_alice",
            override_reason="",  # Missing reason
            acknowledge_risk=REQUIRED_OVERRIDE_ACKNOWLEDGEMENT,
        )

    with pytest.raises(PromotionGateError):
        gate.promote(
            exp.experiment_id,
            dry_run=False,
            actor="researcher_alice",
            override_reason="Known crash in rare edgecase",
            acknowledge_risk="WRONG_STRING",  # Wrong string
        )

    # 3. Deliberate, explicit override succeeds and writes MANUAL_OVERRIDE audit record
    prom_res = gate.promote(
        exp.experiment_id,
        dry_run=False,
        actor="researcher_alice",
        override_reason="Benchmarking crash adversary in test champion slot",
        acknowledge_risk=REQUIRED_OVERRIDE_ACKNOWLEDGEMENT,
    )

    assert prom_res["status"] == "PROMOTED"
    assert prom_res["mode"] == "MANUAL_OVERRIDE"
    assert prom_res["promoted_by"] == "researcher_alice"

    # Confirm champion pointer updated
    current_champ = registry.get_champion_artifact()
    assert current_champ is not None
    assert current_champ.artifact_id == adversary_crash.artifact_id

    # 4. Rollback to baseline artifact
    rollback_res = gate.rollback(
        historical_artifact_id=baseline.artifact_id,
        reason="Reverting test crash bot back to stable random baseline",
        actor="lead_engineer_bob",
    )
    assert rollback_res["status"] == "ROLLED_BACK"
    assert rollback_res["mode"] == "ROLLBACK"
    assert rollback_res["promoted_by"] == "lead_engineer_bob"

    # Verify champion manifest now points back to baseline
    restored_champ = registry.get_champion_artifact()
    assert restored_champ is not None
    assert restored_champ.artifact_id == baseline.artifact_id

    # Verify promotions audit trail in DB
    with db.connect() as conn:
        promotions = conn.execute("SELECT * FROM promotions ORDER BY promoted_at ASC").fetchall()
        assert len(promotions) == 2
        # First was override
        assert promotions[0]["mode"] == "MANUAL_OVERRIDE"
        assert promotions[0]["override_acknowledgement"] == REQUIRED_OVERRIDE_ACKNOWLEDGEMENT
        assert promotions[0]["promoted_by"] == "researcher_alice"
        # Second was rollback
        assert promotions[1]["mode"] == "ROLLBACK"
        assert promotions[1]["promoted_by"] == "lead_engineer_bob"


def test_individual_opponent_and_seed_regression_gates():
    """Verify promotion gate independently rejects individual opponent and seed regressions."""
    gate = PromotionGate()
    exp = Experiment(
        experiment_id="exp_test_regression_gates",
        hypothesis="Testing opponent and seed regression rejection",
        baseline_artifact_id="art_base",
        challenger_artifact_id="art_challenger",
        intended_change="Tuned parameters",
        opponent_pool_config={
            "opponents": [
                {"config_id": "opp_alpha", "group": "g1"},
                {"config_id": "opp_beta", "group": "g1"},
            ]
        },
        evaluation_config={"seeds": [101, 102], "maps": ["grid_tiny_4x4"]},
    )

    base_metrics = {
        "aggregate": {
            "total_scheduled": 20,
            "valid_matches": 20,
            "infrastructure_failures": 0,
            "win_rate": 0.65,
            "crash_rate": 0.0,
            "timeout_rate": 0.0,
            "invalid_action_rate": 0.0,
            "protocol_violation_rate": 0.0,
            "runtime_headroom": 0.30,
        },
        "paired_analysis": {
            "completed_pairs": 10,
            "mean_score_delta": 5.0,
            "weighted_mean_win_delta": 0.15,
            "score_delta_bootstrap_ci_95": [2.0, 8.0],
            "win_delta_bootstrap_ci_95": [0.05, 0.25],
            "by_opponent_group": {"g1": {"mean_win_diff": 0.15, "pair_count": 10}},
            "by_opponent": {
                "opp_alpha": {"mean_win_diff": 0.40, "pair_count": 5},
                "opp_beta": {"mean_win_diff": -0.25, "pair_count": 5},  # Exceeds -0.15 limit
            },
            "by_map": {"grid_tiny_4x4": {"mean_win_diff": 0.15, "pair_count": 10}},
            "by_side": {
                "side_0": {"mean_win_diff": 0.15, "pair_count": 5},
                "side_1": {"mean_win_diff": 0.15, "pair_count": 5},
            },
            "by_seed": {
                "101": {"mean_win_diff": 0.30, "pair_count": 5},
                "102": {"mean_win_diff": 0.0, "pair_count": 5},
            },
        },
    }

    # 1. Individual opponent regression triggers rejection
    res_opp = gate.check_criteria(exp, base_metrics)
    assert not res_opp["passed"]
    assert any(
        "Opponent 'opp_beta'" in v and "regressed beyond tolerance" in v
        for v in res_opp["violations"]
    )

    # 2. Fix opponent, but introduce severe seed regression
    metrics_seed_reg = dict(base_metrics)
    metrics_seed_reg["paired_analysis"] = dict(base_metrics["paired_analysis"])
    metrics_seed_reg["paired_analysis"]["by_opponent"] = {
        "opp_alpha": {"mean_win_diff": 0.20, "pair_count": 5},
        "opp_beta": {"mean_win_diff": 0.10, "pair_count": 5},
    }
    metrics_seed_reg["paired_analysis"]["by_seed"] = {
        "101": {"mean_win_diff": 0.40, "pair_count": 5},
        "102": {"mean_win_diff": -0.30, "pair_count": 5},  # Exceeds -0.20 limit
    }
    res_seed = gate.check_criteria(exp, metrics_seed_reg)
    assert not res_seed["passed"]
    assert any(
        "Seed '102'" in v and "regressed beyond tolerance" in v for v in res_seed["violations"]
    )

    # 3. Protocol violation rate triggers rejection
    metrics_proto = dict(base_metrics)
    metrics_proto["aggregate"] = dict(base_metrics["aggregate"])
    metrics_proto["aggregate"]["protocol_violation_rate"] = 0.05
    res_proto = gate.check_criteria(exp, metrics_proto)
    assert not res_proto["passed"]
    assert any(
        "Protocol violation rate" in v and "exceeds maximum allowable" in v
        for v in res_proto["violations"]
    )

    # 4. Insufficient critical opponent sample size triggers rejection
    metrics_insufficient = dict(base_metrics)
    metrics_insufficient["paired_analysis"] = dict(base_metrics["paired_analysis"])
    metrics_insufficient["paired_analysis"]["by_opponent"] = {
        "opp_alpha": {"mean_win_diff": 0.20, "pair_count": 5},
        # opp_beta is missing entirely (0 pairs)
    }
    res_insuf = gate.check_criteria(exp, metrics_insufficient)
    assert not res_insuf["passed"]
    assert any("Insufficient evidence: Opponent 'opp_beta'" in v for v in res_insuf["violations"])


def test_duplicate_pair_detection():
    """Verify calculate_paired_experiment_metrics detects and rejects duplicate pairs."""
    from battlelab.analysis.metrics import calculate_paired_experiment_metrics

    matches = [
        {"pair_id": "pair_001", "bot_a_id": "challenger", "score_a": 10.0, "winner": "A"},
        {"pair_id": "pair_001", "bot_a_id": "baseline", "score_a": 5.0, "winner": "A"},
        # Duplicate challenger match for pair_001
        {"pair_id": "pair_001", "bot_a_id": "challenger", "score_a": 12.0, "winner": "A"},
    ]

    import pytest

    with pytest.raises(ValueError) as exc:
        calculate_paired_experiment_metrics(
            matches=matches,
            challenger_id="challenger",
            baseline_id="baseline",
        )
    assert "Duplicate challenger match found for pair_id 'pair_001'" in str(exc.value)
