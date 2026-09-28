"""Integration tests for TournamentScheduler, resumability, and matrix generation."""

from pathlib import Path

from battlelab.bots.registry import BotRegistry
from battlelab.matches.matrix import generate_match_matrix
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.storage.database import Database


def test_tournament_execution_and_resumability(tmp_path: Path):
    db_file = tmp_path / "tourn_test.db"
    db = Database(db_file)
    registry = BotRegistry(db)

    # Register two mock bots
    bot_a = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="FixedChallenger",
        tags=["policy:fixed"],
    )
    bot_b = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="RandomBaseline",
        tags=["policy:random"],
    )

    # Generate matrix of 4 matches (2 seeds * 2 paired sides)
    specs = generate_match_matrix(
        bot_a_id=bot_a.artifact_id,
        opponents=[bot_b.artifact_id],
        maps=["grid_classic_8x8"],
        seeds=[101, 102],
        paired_sides=True,
    )
    assert len(specs) == 4

    scheduler = TournamentScheduler(db=db, max_workers=2)
    t_id = "test_trn_1"
    scheduler.create_tournament(
        tournament_id=t_id,
        name="Integration Tournament",
        specs=specs,
    )

    # 1. Run only 2 matches and stop
    res1 = scheduler.run_tournament(t_id, stop_after=2)
    assert res1["status"] == "INTERRUPTED"
    assert res1["completed_matches"] == 2

    matches_after_interruption = db.list_matches_by_tournament(t_id)
    completed_matches = [m for m in matches_after_interruption if m["status"] == "COMPLETED"]
    assert len(completed_matches) == 2

    # 2. Resume tournament
    res2 = scheduler.run_tournament(t_id)
    assert res2["status"] == "COMPLETED"
    assert res2["completed_matches"] == 4

    matches_final = db.list_matches_by_tournament(t_id)
    assert all(m["status"] == "COMPLETED" for m in matches_final)
    # Check that all matches recorded scores and outcomes
    assert all(m["outcome"] in ("WIN_A", "WIN_B", "DRAW") for m in matches_final)
