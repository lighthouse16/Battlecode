"""Phase 2 tests: Subprocess execution, process isolation, hard timeouts, and tree termination."""

import subprocess
import sys
import time
from pathlib import Path

import psutil

from battlelab.adapters import get_adapter
from battlelab.bots.artifacts import create_bot_artifact
from battlelab.bots.process_runner import (
    check_memory_limit_support,
    is_process_active,
    terminate_process_tree,
)
from battlelab.core.models import FailureCategory, MatchOutcome, MatchSpec


def test_real_artifact_execution_immutability(tmp_path: Path):
    """Verify that execution runs strictly from snapshot directory and is immune to external edits."""
    script_path = tmp_path / "original_bot.py"
    script_path.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    turn = state.get("turn", 0)
    sys.stdout.write(json.dumps({"type": "CLAIM" if turn == 0 else "PASS"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    bot_art = create_bot_artifact(script_path, display_name="ImmutabilityTestBot")
    snapshot_file = Path(bot_art.source_location) / script_path.name
    assert snapshot_file.exists()

    # Tamper with the original external script
    script_path.write_text("SYNTAX_ERROR_CRASH = True\\n", encoding="utf-8")

    # Run match using snapshot artifact: should execute clean CLAIM logic, not syntax error
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_immutability_1",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=bot_art.artifact_id,
        bot_b_id=bot_art.artifact_id,
        map_name="grid_tiny_4x4",
        seed=42,
    )
    res = mock.run_local_match(spec, bot_art, bot_art, tmp_path / "work")
    assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)
    assert res.failure_classification is None


def test_hard_10ms_timeout_sub_second_return(tmp_path: Path):
    """Verify hard 10ms per-turn limit returns in well under 1 second without hanging."""
    timeout_script = tmp_path / "slow_bot.py"
    timeout_script.write_text(
        """import sys, json, time
for line in sys.stdin:
    if not line.strip(): continue
    time.sleep(2.0)  # Exceeds 10ms limit
    sys.stdout.write(json.dumps({"type": "PASS"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    slow_bot = create_bot_artifact(timeout_script, display_name="SlowBot")

    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_timeout_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=slow_bot.artifact_id,
        bot_b_id=slow_bot.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
        time_limit_ms=10,  # 10ms deadline
    )

    t0 = time.perf_counter()
    res = mock.run_local_match(spec, slow_bot, slow_bot, tmp_path / "work_timeout")
    elapsed = time.perf_counter() - t0

    # Match terminates on first turn timeout; must be substantially sub-second
    assert elapsed < 0.8
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.TIMEOUT


def test_process_tree_termination_cleans_children():
    """Verify terminate_process_tree kills both parent and any background children."""
    code = """import subprocess, sys, time
# Spawn background sleep child
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
while True:
    time.sleep(1)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    child_pid_line = proc.stdout.readline().strip()
    child_pid = int(child_pid_line)

    assert psutil.pid_exists(proc.pid)
    child_visible = psutil.pid_exists(child_pid)

    # Terminate process tree
    terminate_process_tree(proc, timeout_seconds=1.0)
    time.sleep(0.1)

    assert not is_process_active(proc.pid)
    if child_visible:
        assert not is_process_active(child_pid)


def test_memory_limit_support_check():
    """Verify check_memory_limit_support returns accurate platform capability."""
    supported, msg = check_memory_limit_support()
    if sys.platform.startswith("win"):
        assert supported is False
        assert "Windows" in msg
    else:
        assert supported is True
