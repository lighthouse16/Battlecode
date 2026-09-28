"""Integration tests for experiment lifecycle, evaluation, promotion gates, and rollback."""

from pathlib import Path
import pytest
from battlelab.bots.registry import BotRegistry
from battlelab.core.errors import PromotionGateError
from battlelab.experiments.evaluator import ExperimentEvaluator
from battlelab.experiments.promotion import PromotionGate
from battlelab.experiments.registry import ExperimentRegistry
from battlelab.storage.database import Database

def test_experiment_eval_and_promotion(tmp_path: Path):
    db_file = tmp_path / "exp_test.db"
    db = Database(db_file)
    registry = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)

    # 1. Register baseline (random) and winning challenger (fixed)
    baseline = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="BaselineRandom",
        tags=["policy:random"],
    )
    challenger = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="ChallengerFixed",
        tags=["policy:fixed"],
    )

    exp = exp_reg.create_experiment(
        hypothesis="Fixed deterministic movements will outperform random exploration on small grids",
        baseline_artifact_id=baseline.artifact_id,
        challenger_artifact_id=challenger.artifact_id,
        intended_change="Use cyclic pattern policy instead of random walk",
    )

    evaluator = ExperimentEvaluator(db)
    res = evaluator.run_experiment(exp.experiment_id, max_workers=2)
    assert res["status"] == "COMPLETED"
    assert Path(res["report_path"]).exists()
    assert Path(res["packet_path"]).exists()

    # Read generated report
    report_text = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "Executive Summary" in report_text
    assert exp.experiment_id in report_text

    # Test Promotion Gate
    gate = PromotionGate(db)
    # Check criteria
    loaded_exp = exp_reg.get_experiment(exp.experiment_id)
    assert loaded_exp.results_summary is not None

    criteria_res = gate.check_criteria(loaded_exp, loaded_exp.results_summary)
    assert "metrics_evaluated" in criteria_res

    # Test Dry Run
    dry = gate.promote(exp.experiment_id, dry_run=True, force=True)
    assert dry["dry_run"] is True
    assert dry["would_promote"] == challenger.artifact_id

    # Test Actual Promotion
    prom_res = gate.promote(exp.experiment_id, dry_run=False, force=True)
    assert prom_res["status"] == "PROMOTED"

    # Verify champion manifest
    champ = registry.get_champion_artifact()
    assert champ is not None
    assert champ.artifact_id == challenger.artifact_id

    # Test Rollback
    rollback_res = gate.rollback(baseline.artifact_id, reason="Manual safety rollback")
    assert rollback_res["status"] == "ROLLED_BACK"
    champ_after = registry.get_champion_artifact()
    assert champ_after is not None
    assert champ_after.artifact_id == baseline.artifact_id

def test_promotion_rejection_on_failure(tmp_path: Path):
    db_file = tmp_path / "exp_fail_test.db"
    db = Database(db_file)
    registry = BotRegistry(db)
    exp_reg = ExperimentRegistry(db)

    baseline = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="BaselineFixed",
        tags=["policy:fixed"],
    )
    # Buggy challenger with crashes
    buggy_challenger = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="BuggyCrashChallenger",
        tags=["fail:crash"],
    )

    exp = exp_reg.create_experiment(
        hypothesis="Should fail promotion due to crashes",
        baseline_artifact_id=baseline.artifact_id,
        challenger_artifact_id=buggy_challenger.artifact_id,
        intended_change="Injected crash fixture",
    )

    evaluator = ExperimentEvaluator(db)
    evaluator.run_experiment(exp.experiment_id, max_workers=2)

    gate = PromotionGate(db)
    with pytest.raises(PromotionGateError) as exc_info:
        gate.promote(exp.experiment_id, dry_run=False, force=False)
    
    assert len(exc_info.value.violations) > 0
    # Confirm champion did NOT become the buggy challenger
    champ = registry.get_champion_artifact()
    assert champ is None or champ.artifact_id != buggy_challenger.artifact_id
