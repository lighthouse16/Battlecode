"""Integration tests for deterministic match reproduction."""

from pathlib import Path

from battlelab.adapters import get_adapter
from battlelab.bots.registry import BotRegistry
from battlelab.core.models import MatchSpec
from battlelab.matches.worker import execute_match_job
from battlelab.storage.database import Database


def test_match_exact_reproduction(tmp_path: Path):
    db_file = tmp_path / "reproduce.db"
    db = Database(db_file)
    registry = BotRegistry(db)

    bot_a = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="ReproduceA",
        tags=["policy:fixed"],
    )
    bot_b = registry.register_bot(
        source_path=Path("src/battlelab/adapters/mock/bots.py"),
        display_name="ReproduceB",
        tags=["policy:resource"],
    )

    spec = MatchSpec(
        match_id="m_repro_1",
        adapter_name="mock",
        adapter_version="0.1.0",
        bot_a_id=bot_a.artifact_id,
        bot_b_id=bot_b.artifact_id,
        map_name="grid_classic_8x8",
        seed=8888,
    )
    db.save_match_spec(spec, "2026-09-28T00:00:00Z")

    # Run match 1
    res1 = execute_match_job(spec.to_dict(), str(db_file))

    # Re-run identical match from stored spec
    stored_match = db.get_match("m_repro_1")
    assert stored_match is not None

    adapter = get_adapter(stored_match["adapter_name"])
    res2 = adapter.run_local_match(
        spec=spec,
        bot_a=bot_a,
        bot_b=bot_b,
        work_dir=tmp_path / "repro_verify",
    )

    assert res1["outcome"] == res2.outcome.value
    assert res1["score_a"] == res2.score_a
    assert res1["score_b"] == res2.score_b
    assert res1["turns_played"] == res2.turns_played
    assert res1["replay_hash"] == res2.replay_hash
