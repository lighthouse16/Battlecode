"""Phase 2 tests: Atomic SQLite job-leasing, multi-scheduler safety, and lease recovery."""

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from battlelab.bots.registry import BotRegistry
from battlelab.core.models import MatchSpec
from battlelab.matches.matrix import generate_match_matrix
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.storage.database import Database


def test_multi_scheduler_concurrency_exactly_once(tmp_path: Path):
    """Verify that multiple concurrent schedulers leasing from same DB execute every match exactly once."""
    db_file = tmp_path / "multi_sched.db"
    db = Database(db_file)
    registry = BotRegistry(db)

    bot_a = registry.register_bot(
        source_path="bots/baselines/fixed_bot.py",
        display_name="FixedBot",
        tags=["policy:fixed"],
    )
    bot_b = registry.register_bot(
        source_path="bots/baselines/random_bot.py",
        display_name="RandomBot",
        tags=["policy:random"],
    )

    # 8 matches: 4 seeds * 2 paired sides
    specs = generate_match_matrix(
        bot_a_id=bot_a.artifact_id,
        opponents=[bot_b.artifact_id],
        maps=["grid_tiny_4x4"],
        seeds=[101, 102, 103, 104],
        paired_sides=True,
    )
    assert len(specs) == 8

    t_id = "trn_multi_sched_1"
    scheduler1 = TournamentScheduler(db=db, max_workers=2, lease_duration_seconds=30.0)
    scheduler1.create_tournament(tournament_id=t_id, name="Multi-Scheduler Tournament", specs=specs)

    # Create second scheduler on same DB
    scheduler2 = TournamentScheduler(
        db=Database(db_file), max_workers=2, lease_duration_seconds=30.0
    )

    # Run both schedulers concurrently in separate threads
    def run_s1():
        scheduler1.run_tournament(t_id)

    def run_s2():
        scheduler2.run_tournament(t_id)

    t1 = threading.Thread(target=run_s1)
    t2 = threading.Thread(target=run_s2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Verify exactly 8 matches were completed
    matches = db.list_matches_by_tournament(t_id)
    assert len(matches) == 8
    assert all(m["status"] == "COMPLETED" for m in matches)
    # Check that each match has attempt_count == 1
    assert all(m["attempt_count"] == 1 for m in matches)


def test_expired_lease_recovery(tmp_path: Path):
    """Verify that expired RUNNING match leases are safely recovered by another worker."""
    db_file = tmp_path / "lease_rec.db"
    db = Database(db_file)
    registry = BotRegistry(db)

    bot_a = registry.register_bot(
        source_path="bots/baselines/fixed_bot.py",
        display_name="FixedA",
    )
    bot_b = registry.register_bot(
        source_path="bots/baselines/random_bot.py",
        display_name="RandomB",
    )

    spec = MatchSpec(
        match_id="m_expired_lease_1",
        adapter_name="mock",
        adapter_version="0.1.0",
        bot_a_id=bot_a.artifact_id,
        bot_b_id=bot_b.artifact_id,
        map_name="grid_tiny_4x4",
        seed=42,
    )
    db.save_match_spec(spec, created_at="2026-09-28T00:00:00Z")

    # Manually simulate crashed worker with expired lease
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with db.connect() as conn:
        with conn:
            conn.execute(
                """
                UPDATE matches
                SET status = 'RUNNING',
                    worker_id = 'crashed_worker_dead',
                    lease_timestamp = ?,
                    lease_expires_at = ?,
                    attempt_count = 1
                WHERE match_id = 'm_expired_lease_1'
                """,
                (past_time, past_time),
            )

    # Now a new worker calls lease_next_match
    leased_job = db.lease_next_match(
        tournament_id=None,
        worker_id="new_healthy_worker",
        lease_duration_seconds=30.0,
    )

    assert leased_job is not None
    assert leased_job["match_id"] == "m_expired_lease_1"
    # Attempt count incremented to 2
    assert leased_job["attempt_count"] == 2
    assert leased_job["worker_id"] == "new_healthy_worker"
