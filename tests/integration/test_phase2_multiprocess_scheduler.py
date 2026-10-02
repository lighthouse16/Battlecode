"""Integration tests for multi-process scheduler contention and process-death lease recovery."""

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from battlelab.bots.registry import BotRegistry
from battlelab.core.models import MatchSpec
from battlelab.matches.matrix import generate_match_matrix
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.storage.database import Database

RUNNER_SCRIPT = """
import sys
from pathlib import Path
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.storage.database import Database

if __name__ == '__main__':
    db_path = sys.argv[1]
    t_id = sys.argv[2]
    worker_prefix = sys.argv[3]
    lease_sec = float(sys.argv[4]) if len(sys.argv) > 4 else 10.0
    db = Database(Path(db_path))
    sched = TournamentScheduler(db=db, max_workers=2, lease_duration_seconds=lease_sec)
    sched.scheduler_id = worker_prefix
    sched.run_tournament(t_id)
"""


def test_independent_scheduler_processes_contention(tmp_path: Path):
    """Verify two independent Python interpreter processes contend for leases and execute exactly once."""
    db_file = tmp_path / "multiprocess_sched.db"
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

    # 10 matches: 5 seeds * 2 paired sides
    specs = generate_match_matrix(
        bot_a_id=bot_a.artifact_id,
        opponents=[bot_b.artifact_id],
        maps=["grid_tiny_4x4"],
        seeds=[101, 102, 103, 104, 105],
        paired_sides=True,
    )
    assert len(specs) == 10

    t_id = "trn_multiprocess_contention"
    scheduler = TournamentScheduler(db=db, max_workers=2, lease_duration_seconds=15.0)
    scheduler.create_tournament(tournament_id=t_id, name="Multiprocess Contention", specs=specs)

    # Launch two independent OS processes running python
    p1 = subprocess.Popen(
        [sys.executable, "-c", RUNNER_SCRIPT, str(db_file), t_id, "proc_alpha", "15.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p2 = subprocess.Popen(
        [sys.executable, "-c", RUNNER_SCRIPT, str(db_file), t_id, "proc_beta", "15.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    pid_a = p1.pid
    pid_b = p2.pid

    p1.wait(timeout=60)
    p2.wait(timeout=60)

    assert p1.returncode == 0, (
        f"Process 1 failed with stderr: {p1.stderr.read() if p1.stderr else ''}"
    )
    assert p2.returncode == 0, (
        f"Process 2 failed with stderr: {p2.stderr.read() if p2.stderr else ''}"
    )

    # Verify exactly 10 matches executed
    matches = db.list_matches_by_tournament(t_id)
    assert len(matches) == 10
    assert all(m["status"] == "COMPLETED" for m in matches)
    assert all(m["attempt_count"] == 1 for m in matches)

    # Verify both processes acquired work
    worker_ids = {m["worker_id"] for m in matches if m.get("worker_id")}
    matches_alpha = [m["match_id"] for m in matches if "proc_alpha" in (m.get("worker_id") or "")]
    matches_beta = [m["match_id"] for m in matches if "proc_beta" in (m.get("worker_id") or "")]

    print(
        f"Contention Evidence: Scheduler PID A={pid_a} leased {len(matches_alpha)} matches; "
        f"Scheduler PID B={pid_b} leased {len(matches_beta)} matches. Workers={worker_ids}. Total={len(matches)} COMPLETED."
    )

    assert len(matches_alpha) > 0, "proc_alpha acquired no matches"
    assert len(matches_beta) > 0, "proc_beta acquired no matches"
    assert len(matches_alpha) + len(matches_beta) == 10


def test_real_process_death_and_natural_lease_recovery(tmp_path: Path):
    """Verify that killing a running scheduler process leads to natural lease expiry and recovery by process B."""
    from battlelab.core.models import MatchOutcome, MatchResult

    db_file = tmp_path / "proc_death_rec.db"
    db = Database(db_file)
    registry = BotRegistry(db)

    # Create a bot that takes 2 seconds per turn so the lease stays RUNNING while process is alive
    slow_script = tmp_path / "slow_bot.py"
    slow_script.write_text(
        """import sys, json, time
for line in sys.stdin:
    if not line.strip(): continue
    time.sleep(2.0)
    state = json.loads(line)
    act = {"type": "CLAIM"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    bot_slow = registry.register_bot(source_path=slow_script, display_name="SlowDeathBot")
    bot_fixed = registry.register_bot(
        source_path="bots/baselines/fixed_bot.py", display_name="Fixed"
    )

    spec = MatchSpec(
        match_id="m_death_recovery_1",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=bot_slow.artifact_id,
        bot_b_id=bot_fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=42,
    )

    t_id = "trn_death_recovery"
    scheduler = TournamentScheduler(db=db, max_workers=1, lease_duration_seconds=1.5)
    scheduler.create_tournament(tournament_id=t_id, name="Death Recovery Tournament", specs=[spec])

    matches = db.list_matches_by_tournament(t_id)
    assert len(matches) == 1
    target_match_id = matches[0]["match_id"]

    # 1. Start Process A with 1.5s lease duration
    proc_a = subprocess.Popen(
        [sys.executable, "-c", RUNNER_SCRIPT, str(db_file), t_id, "proc_doomed_A", "1.5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid_a = proc_a.pid

    # 2. Wait until match row reaches RUNNING with Process A ownership
    running_worker_id = None
    for _ in range(50):
        m = db.get_match(target_match_id)
        if m and m.get("status") == "RUNNING" and m.get("worker_id"):
            running_worker_id = m["worker_id"]
            break
        time.sleep(0.1)

    assert running_worker_id is not None, "Match failed to reach RUNNING status"
    assert "proc_doomed_A" in running_worker_id
    m_before = db.get_match(target_match_id)
    assert m_before is not None
    assert m_before["attempt_count"] == 1
    initial_lease_token = m_before["lease_token"]
    assert initial_lease_token is not None

    # 3. Kill Process A abruptly without normal cleanup
    proc_a.kill()
    proc_a.wait()

    # 4. Allow the lease to expire naturally (lease duration is 1.5s)
    time.sleep(2.0)

    # Verify row has expired naturally without manual modification
    m_expired = db.get_match(target_match_id)
    assert m_expired is not None
    assert m_expired["status"] == "RUNNING"
    now_iso = datetime.now(timezone.utc).isoformat()
    assert m_expired["lease_expires_at"] < now_iso, "Lease must have expired naturally"

    # 5. Start Process B with 15.0s lease duration to recover and complete tournament
    proc_b = subprocess.Popen(
        [sys.executable, "-c", RUNNER_SCRIPT, str(db_file), t_id, "proc_rescuer_B", "15.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid_b = proc_b.pid

    # Wait until Process B acquires the expired lease
    recovery_lease_token = None
    for _ in range(50):
        m = db.get_match(target_match_id)
        if m and m.get("worker_id") and "proc_rescuer_B" in m["worker_id"]:
            recovery_lease_token = m.get("lease_token")
            if recovery_lease_token and recovery_lease_token != initial_lease_token:
                break
        time.sleep(0.1)

    # 6. Attempt stale commit with doomed Process A's old lease token - must be rejected
    stale_dummy_result = MatchResult(
        match_id=target_match_id,
        outcome=MatchOutcome.WIN_A,
        winner="A",
        score_a=999.0,
        score_b=0.0,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    stale_rejected = not db.update_match_result(stale_dummy_result, lease_token=initial_lease_token)
    assert stale_rejected, (
        "Database must reject stale commit attempt using expired/superseded lease token"
    )

    proc_b.wait(timeout=30)
    assert proc_b.returncode == 0

    # 7. Verify Process B recovered the job, incremented attempt_count, and completed it authoritatively
    m_final = db.get_match(target_match_id)
    assert m_final is not None
    assert m_final["status"] == "COMPLETED"
    assert m_final["attempt_count"] == 2
    final_worker_id = m_final["worker_id"] or ""
    assert "proc_rescuer_B" in final_worker_id

    print(
        f"Lease Recovery Evidence: "
        f"RUNNING owned by A (PID {pid_a}, worker={running_worker_id}, token={initial_lease_token[:8]}..., attempt=1) "
        f"-> A killed -> lease naturally expired -> B acquired new token (PID {pid_b}, worker={final_worker_id}, "
        f"token={(m_final['lease_token'] or '')[:8]}..., attempt=2) -> stale token commit rejected -> B authoritative COMPLETED."
    )


def test_leased_match_heartbeat_database_invariants_and_tamper_detection(
    tmp_path: Path, monkeypatch
):
    """Verify that match lease heartbeats legitimately write to SQLite without triggering false tampering,
    while unauthorized database mutations (e.g. modifying other matches) are detected and fail the match.
    """
    db_file = tmp_path / "heartbeat_tamper_test.db"
    monkeypatch.setenv("BATTLELAB_DATABASE_PATH", str(db_file))
    db = Database(db_file)
    registry = BotRegistry(db)

    # 1. Setup bot that pauses 0.25s per turn for 3 turns (total ~0.75s)
    heartbeat_bot_script = tmp_path / "hb_bot.py"
    heartbeat_bot_script.write_text(
        """import sys, json, time
for line in sys.stdin:
    if not line.strip(): continue
    time.sleep(0.25)
    state = json.loads(line)
    act = {"type": "CLAIM"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art_hb = registry.register_bot(heartbeat_bot_script, display_name="HeartbeatBot")
    art_fixed = registry.register_bot(Path("bots/baselines/fixed_bot.py"), display_name="FixedBot")

    spec_valid = MatchSpec(
        match_id="m_hb_valid",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=art_hb.artifact_id,
        bot_b_id=art_fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )

    t_id = "trn_heartbeat_invariants"
    # Use short lease duration 0.4s -> heartbeat interval max(0.01, 0.4/3) = ~0.133s
    # During ~0.75s execution, at least 2-3 heartbeats will occur
    scheduler = TournamentScheduler(db=db, max_workers=1, lease_duration_seconds=0.4)
    scheduler.create_tournament(tournament_id=t_id, name="Heartbeat Invariants", specs=[spec_valid])

    # Run tournament
    summary = scheduler.run_tournament(t_id)
    assert summary["status"] == "COMPLETED"

    matches = db.list_matches_by_tournament(t_id)
    assert len(matches) == 1
    target_match_id = matches[0]["match_id"]
    match_row = db.get_match(target_match_id)
    assert match_row is not None
    assert match_row["status"] == "COMPLETED"
    assert match_row["last_heartbeat"] is not None
    # Heartbeat writes succeeded and match finished without STORAGE_FAILURE

    # 2. Vector B: Tamper with database from inside match execution
    # Bot modifies another match record in SQLite
    victim_spec = MatchSpec(
        match_id="m_other_victim",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=art_fixed.artifact_id,
        bot_b_id=art_fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=999,
    )
    db.save_match_spec(victim_spec, datetime.now(timezone.utc).isoformat())

    tamper_bot_script = tmp_path / "tamper_db_bot.py"
    db_escaped = db_file.resolve().as_posix()
    tamper_bot_script.write_text(
        f"""import sys, json, sqlite3
conn = sqlite3.connect("{db_escaped}")
conn.execute("UPDATE matches SET outcome = 'FORGED_WIN' WHERE match_id = 'm_other_victim'")
conn.commit()
conn.close()
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    act = {{"type": "CLAIM"}}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art_tamper = registry.register_bot(tamper_bot_script, display_name="DbTamperBot")
    spec_tamper = MatchSpec(
        match_id="m_hb_tamper",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=art_tamper.artifact_id,
        bot_b_id=art_fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=2,
        max_attempts=1,
    )
    t_id_tamper = "trn_tamper_check"
    scheduler_tamper = TournamentScheduler(db=db, max_workers=1, lease_duration_seconds=0.4)
    scheduler_tamper.create_tournament(
        tournament_id=t_id_tamper, name="Tamper Check", specs=[spec_tamper]
    )
    matches_tamper = db.list_matches_by_tournament(t_id_tamper)
    assert len(matches_tamper) == 1
    target_tamper_id = matches_tamper[0]["match_id"]

    scheduler_tamper.run_tournament(t_id_tamper)

    tamper_match_row = db.get_match(target_tamper_id)
    assert tamper_match_row is not None
    assert tamper_match_row["outcome"] == "INFRASTRUCTURE_FAILURE"
    assert tamper_match_row["failure_category"] == "STORAGE_FAILURE"
    evidence = (tamper_match_row.get("last_infrastructure_error") or "").lower()
    assert "tampering detected" in evidence or "unauthorized mutation" in evidence


def test_worker_death_child_containment_and_recovery(tmp_path: Path):
    """Verify Windows Job Object child containment and lease recovery after hard worker death.

    1. launch real scheduler worker;
    2. launch long-running bot;
    3. record worker PID;
    4. record bot PID;
    5. bot spawns a descendant process;
    6. record descendant PID;
    7. hard-kill worker without graceful cleanup;
    8. verify bot PID is dead;
    9. verify descendant PID is dead;
    10. lease expires;
    11. replacement worker recovers;
    12. authoritative match completes.
    """
    import psutil

    from battlelab.bots.process_runner import get_containment_capabilities

    caps = get_containment_capabilities()
    assert "worker_death_child_containment" in caps
    if sys.platform.startswith("win"):
        assert caps["worker_death_child_containment"]["status"] == "ENFORCED_AND_TESTED"
    else:
        assert caps["worker_death_child_containment"]["status"] == "PLATFORM_DEPENDENT"

    db_file = tmp_path / "orphan_death.db"
    db = Database(db_file)
    registry = BotRegistry(db)

    bot_pid_file = tmp_path / "bot_pid.txt"
    descendant_pid_file = tmp_path / "descendant_pid.txt"
    bot_pid_posix = bot_pid_file.resolve().as_posix()
    desc_pid_posix = descendant_pid_file.resolve().as_posix()

    descendant_bot_script = tmp_path / "descendant_bot.py"
    descendant_bot_script.write_text(
        f"""import sys, json, os, time, subprocess
# Record bot PID
with open("{bot_pid_posix}", "w") as f:
    f.write(str(os.getpid()))

# Spawn a descendant process
p_desc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open("{desc_pid_posix}", "w") as f:
    f.write(str(p_desc.pid))

for line in sys.stdin:
    if not line.strip(): continue
    time.sleep(30.0)
    state = json.loads(line)
    act = {{"type": "CLAIM"}}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    bot_desc = registry.register_bot(
        source_path=descendant_bot_script, display_name="DescendantSpawnerBot"
    )
    bot_fixed = registry.register_bot(
        source_path="bots/baselines/fixed_bot.py", display_name="Fixed"
    )

    spec = MatchSpec(
        match_id="m_containment_test",
        adapter_name="mock",
        adapter_version="0.2.0",
        bot_a_id=bot_desc.artifact_id,
        bot_b_id=bot_fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=10,
    )

    t_id = "trn_containment_test"
    scheduler = TournamentScheduler(db=db, max_workers=1, lease_duration_seconds=1.5)
    scheduler.create_tournament(tournament_id=t_id, name="Containment Tournament", specs=[spec])
    matches = db.list_matches_by_tournament(t_id)
    assert len(matches) == 1
    target_match_id = matches[0]["match_id"]

    # 1. Start worker process A
    proc_a = subprocess.Popen(
        [sys.executable, "-c", RUNNER_SCRIPT, str(db_file), t_id, "proc_worker_A", "1.5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker_pid = proc_a.pid
    assert psutil.pid_exists(worker_pid), "Worker A process must be alive"

    # 2. Wait until bot starts and both PIDs are recorded
    bot_pid: int | None = None
    descendant_pid: int | None = None
    for _ in range(60):
        if bot_pid is None and bot_pid_file.exists():
            c1 = bot_pid_file.read_text().strip()
            if c1.isdigit():
                bot_pid = int(c1)
        if descendant_pid is None and descendant_pid_file.exists():
            c2 = descendant_pid_file.read_text().strip()
            if c2.isdigit():
                descendant_pid = int(c2)
        if bot_pid is not None and descendant_pid is not None:
            break
        time.sleep(0.1)

    assert bot_pid is not None, "Bot process failed to record PID"
    assert descendant_pid is not None, "Descendant process failed to record PID"
    assert psutil.pid_exists(bot_pid), f"Bot PID {bot_pid} should be active"
    assert psutil.pid_exists(descendant_pid), f"Descendant PID {descendant_pid} should be active"

    # 3. Hard-kill worker process A without graceful cleanup
    proc_a.kill()
    proc_a.wait()
    assert not psutil.pid_exists(worker_pid), "Worker A must be terminated"

    # 4. Wait brief interval for OS kernel Job Object cleanup
    time.sleep(0.5)

    # 5. On Windows with Job Object KILL_ON_JOB_CLOSE, both bot and descendant are terminated
    if sys.platform.startswith("win"):
        bot_alive = psutil.pid_exists(bot_pid)
        desc_alive = psutil.pid_exists(descendant_pid)
        assert not bot_alive, f"Bot PID {bot_pid} must be dead after worker hard kill"
        assert not desc_alive, (
            f"Descendant PID {descendant_pid} must be dead after worker hard kill"
        )
    else:
        # On POSIX without external daemons, clean up if still alive
        for p in (bot_pid, descendant_pid):
            if psutil.pid_exists(p):
                try:
                    psutil.Process(p).kill()
                except Exception:
                    pass

    # 6. Wait for lease to expire naturally
    time.sleep(1.8)

    # 7. Start worker process B to recover expired lease
    proc_b = subprocess.Popen(
        [sys.executable, "-c", RUNNER_SCRIPT, str(db_file), t_id, "proc_worker_B", "15.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc_b.wait(timeout=30)
    assert proc_b.returncode == 0

    # 8. Verify authoritative match completed with attempt_count=2
    m_recovered = db.get_match(target_match_id)
    assert m_recovered is not None
    assert m_recovered["status"] == "COMPLETED"
    assert m_recovered["attempt_count"] == 2
    assert "proc_worker_B" in (m_recovered.get("worker_id") or "")
