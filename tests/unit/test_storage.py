"""Unit tests for storage, migrations, and database operations."""

from pathlib import Path

from battlelab.core.models import (
    BotArtifact,
    Experiment,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)
from battlelab.storage.database import Database


def test_database_crud(tmp_path: Path):
    db_file = tmp_path / "test.db"
    db = Database(db_file)

    # 1. Artifacts
    art = BotArtifact(
        artifact_id="art_test1",
        display_name="TestBot",
        source_location="/tmp/test",
        language="python",
        git_commit="abcdef",
        dirty_worktree=False,
        source_hash="sha123",
        created_at="2026-09-28T00:00:00Z",
    )
    db.save_artifact(art)
    loaded_art = db.get_artifact("art_test1")
    assert loaded_art is not None
    assert loaded_art.display_name == "TestBot"
    assert loaded_art.git_commit == "abcdef"
    assert len(db.list_artifacts()) == 1

    # 2. Tournaments
    db.save_tournament(
        tournament_id="trn_1",
        name="Test Tournament",
        config_hash="conf_hash",
        created_at="2026-09-28T00:00:00Z",
        config={"workers": 2},
    )
    trn = db.get_tournament("trn_1")
    assert trn is not None
    assert trn["name"] == "Test Tournament"

    # 3. Matches
    art2 = BotArtifact(
        artifact_id="art_test2",
        display_name="TestBot2",
        source_location="/tmp/test2",
        language="python",
        git_commit="abcdef",
        dirty_worktree=False,
        source_hash="sha456",
        created_at="2026-09-28T00:00:00Z",
    )
    db.save_artifact(art2)

    spec = MatchSpec(
        match_id="m_100",
        adapter_name="mock",
        adapter_version="0.1.0",
        bot_a_id="art_test1",
        bot_b_id="art_test2",
        map_name="map_a",
        seed=42,
        tournament_id="trn_1",
    )
    db.save_match_spec(spec, "2026-09-28T00:00:00Z")
    m = db.get_match("m_100")
    assert m is not None
    assert m["status"] == "PENDING"

    res = MatchResult(
        match_id="m_100",
        outcome=MatchOutcome.WIN_A,
        winner="A",
        score_a=100.0,
        score_b=50.0,
        duration_ms=45.0,
        completed_at="2026-09-28T00:01:00Z",
    )
    db.update_match_result(res)
    m_updated = db.get_match("m_100")
    assert m_updated is not None
    assert m_updated["status"] == "COMPLETED"
    assert m_updated["outcome"] == "WIN_A"
    assert m_updated["score_a"] == 100.0

    # 4. Experiments
    exp = Experiment(
        experiment_id="exp_1",
        hypothesis="Testing speedup",
        baseline_artifact_id="art_test1",
        challenger_artifact_id="art_test2",
        intended_change="Tweak heuristic",
        status="RUNNING",
        created_at="2026-09-28T00:00:00Z",
    )
    db.save_experiment(exp)
    loaded_exp = db.get_experiment("exp_1")
    assert loaded_exp is not None
    assert loaded_exp.hypothesis == "Testing speedup"

    # 5. Promotions
    db.save_promotion(
        promotion_id="p_1",
        experiment_id="exp_1",
        artifact_id="art_test2",
        promoted_at="2026-09-28T00:02:00Z",
        manifest_snapshot={"champion_artifact_id": "art_test2"},
        reason="Beat baseline decisively",
    )
    promotions = db.list_promotions()
    assert len(promotions) == 1
    assert promotions[0]["artifact_id"] == "art_test2"
