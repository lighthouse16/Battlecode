"""Phase 2 tests: Subprocess execution, process isolation, hard timeouts, and tree termination."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from battlelab.adapters import get_adapter
from battlelab.bots.artifacts import create_bot_artifact
from battlelab.bots.process_runner import (
    BotSubprocess,
    check_memory_limit_support,
    is_process_active,
    terminate_process_tree,
)
from battlelab.core.hashing import ProtectedFileState
from battlelab.core.models import BotArtifact, FailureCategory, MatchOutcome, MatchSpec


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
    act = {"type": "CLAIM" if turn == 0 else "PASS"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
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

    # Registering modified source creates a new artifact with a different hash
    new_art = create_bot_artifact(script_path, display_name="ImmutabilityTestBot")
    assert new_art.artifact_id != bot_art.artifact_id
    assert new_art.manifest_hash != bot_art.manifest_hash


def test_artifact_behavior_independence(tmp_path: Path):
    """Register two bot source trees with same display name and tags, but different logic; verify different behavior."""
    dir1 = tmp_path / "bot_source_1"
    dir1.mkdir()
    (dir1 / "bot.py").write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    act = {"type": "CLAIM"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    dir2 = tmp_path / "bot_source_2"
    dir2.mkdir()
    (dir2 / "bot.py").write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    act = {"type": "MOVE", "direction": "RIGHT"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    # Identical display names and tags
    bot1 = create_bot_artifact(
        dir1 / "bot.py", display_name="IdenticalIdentityBot", tags=["policy:fixed", "test:tag"]
    )
    bot2 = create_bot_artifact(
        dir2 / "bot.py", display_name="IdenticalIdentityBot", tags=["policy:fixed", "test:tag"]
    )

    # Artifact IDs and hashes must be different
    assert bot1.artifact_id != bot2.artifact_id
    assert bot1.manifest_hash != bot2.manifest_hash

    mock = get_adapter("mock")
    # Fixed bot to play against
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")

    spec1 = MatchSpec(
        match_id="m_indep_1",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=bot1.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_classic_8x8",
        seed=42,
    )
    res1 = mock.run_local_match(spec1, bot1, fixed, tmp_path / "work_indep_1")

    spec2 = MatchSpec(
        match_id="m_indep_2",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=bot2.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_classic_8x8",
        seed=42,
    )
    res2 = mock.run_local_match(spec2, bot2, fixed, tmp_path / "work_indep_2")

    # Observable behavior difference in score or turns
    assert (res1.score_a, res1.turns_played) != (res2.score_a, res2.turns_played)


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
    extra_kwargs = {}
    if sys.platform != "win32":
        extra_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **extra_kwargs,
    )
    assert proc.stdout is not None
    child_pid_line = proc.stdout.readline().strip()
    child_pid = int(child_pid_line)

    assert proc.poll() is None
    child_visible = is_process_active(child_pid)

    # Terminate process tree
    terminate_process_tree(proc, timeout_seconds=1.0)
    time.sleep(0.1)

    assert proc.poll() is not None
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


def test_hard_10ms_infinite_loop_timeout_sub_second_return(tmp_path: Path):
    """Verify hard 10ms per-turn limit terminates infinite CPU loop in well under 1 second."""
    loop_script = Path("bots/adversaries/infinite_loop_bot.py")
    loop_bot = create_bot_artifact(loop_script, display_name="InfiniteLoopBot")

    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_loop_timeout_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=loop_bot.artifact_id,
        bot_b_id=loop_bot.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
        time_limit_ms=10,  # 10ms deadline
    )

    t0 = time.perf_counter()
    res = mock.run_local_match(spec, loop_bot, loop_bot, tmp_path / "work_loop_timeout")
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.8
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.TIMEOUT


def test_invalid_action_and_malformed_protocol_distinction(tmp_path: Path):
    """Verify distinct classification of malformed output vs invalid action."""
    mock = get_adapter("mock")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")

    # Malformed output bot -> PROTOCOL_ERROR
    malformed_bot = create_bot_artifact(
        Path("bots/adversaries/malformed_bot.py"), display_name="MalformedBot"
    )
    spec_mal = MatchSpec(
        match_id="m_malformed_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=malformed_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res_mal = mock.run_local_match(spec_mal, malformed_bot, fixed, tmp_path / "work_mal")
    assert res_mal.outcome == MatchOutcome.WIN_B
    assert res_mal.failure_classification is not None
    assert res_mal.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION

    # Invalid action bot -> INVALID_ACTION
    invalid_bot = create_bot_artifact(
        Path("bots/adversaries/invalid_action_bot.py"), display_name="InvalidActionBot"
    )
    spec_inv = MatchSpec(
        match_id="m_invalid_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=invalid_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res_inv = mock.run_local_match(spec_inv, invalid_bot, fixed, tmp_path / "work_inv")
    assert res_inv.outcome == MatchOutcome.WIN_B
    assert res_inv.failure_classification is not None
    assert res_inv.failure_classification.category == FailureCategory.INVALID_ACTION


def test_environment_secret_non_inheritance(tmp_path: Path):
    """Verify coordinator environment secrets are stripped and not inherited by bot subprocess."""
    import os

    secret_key = "BATTLELAB_TEST_SECRET_DO_NOT_INHERIT"
    secret_val = "SECRET_COORDINATOR_TOKEN_ABC123"
    os.environ[secret_key] = secret_val

    bot_script = tmp_path / "secret_probe_bot.py"
    bot_script.write_text(
        f"""import sys, json, os
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    leaked = os.environ.get("{secret_key}")
    if leaked:
        act = {{"type": "LEAK", "value": leaked}}
    else:
        act = {{"type": "CLAIM"}}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    try:
        probe_bot = create_bot_artifact(bot_script, display_name="SecretProbeBot")
        mock = get_adapter("mock")
        fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
        spec = MatchSpec(
            match_id="m_secret_test",
            adapter_name="mock",
            adapter_version=mock.version,
            bot_a_id=probe_bot.artifact_id,
            bot_b_id=fixed.artifact_id,
            map_name="grid_tiny_4x4",
            seed=42,
        )
        res = mock.run_local_match(spec, probe_bot, fixed, tmp_path / "work_secret")
        assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)
        # Verify the secret was NOT leaked
        replay_file = Path(res.replay_path or "")
        if replay_file.is_file():
            content = replay_file.read_text(encoding="utf-8")
            assert secret_val not in content
    finally:
        os.environ.pop(secret_key, None)


def test_stdout_pre_answer_detection(tmp_path: Path):
    """Verify bot that emits output before observation is sent triggers PROTOCOL_VIOLATION."""
    pre_script = tmp_path / "pre_answer_bot.py"
    pre_script.write_text(
        """import sys, json
# Emit output immediately before reading any observation from stdin
sys.stdout.write(json.dumps({"type": "CLAIM"}) + "\\n")
sys.stdout.flush()
for line in sys.stdin:
    if not line.strip(): continue
    sys.stdout.write(json.dumps({"type": "CLAIM"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    pre_bot = create_bot_artifact(pre_script, display_name="PreAnswerBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_pre_answer_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=pre_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, pre_bot, fixed, tmp_path / "work_pre")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert res.protocol_violation_a is True
    assert res.invalid_action_a is False


def test_stdout_multiple_action_detection(tmp_path: Path):
    """Verify bot that emits multiple actions for single observation is terminated with PROTOCOL_VIOLATION."""
    multi_script = tmp_path / "multi_action_bot.py"
    multi_script.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    req_id = state.get("_battlelab_request_id")
    a1 = {"type": "CLAIM"}
    a2 = {"type": "CLAIM"}
    if req_id is not None:
        a1["_battlelab_request_id"] = req_id
        a2["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(a1) + "\\n" + json.dumps(a2) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    multi_bot = create_bot_artifact(multi_script, display_name="MultiActionBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_multi_action_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=multi_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, multi_bot, fixed, tmp_path / "work_multi")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert res.protocol_violation_a is True


def test_stdout_spam_bounding(tmp_path: Path):
    """Verify bot spamming thousands of JSON lines is bounded and killed as PROTOCOL_VIOLATION."""
    spam_script = tmp_path / "spam_bot.py"
    spam_script.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    for _ in range(5000):
        sys.stdout.write(json.dumps({"type": "CLAIM"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    spam_bot = create_bot_artifact(spam_script, display_name="SpamBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_spam_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=spam_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, spam_bot, fixed, tmp_path / "work_spam")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION


def test_giant_unterminated_stdout_message_bounding(tmp_path: Path):
    """Verify bot emitting a giant 5MB line without newline is bounded at 64KB and terminated as PROTOCOL_VIOLATION."""
    giant_script = tmp_path / "giant_line_bot.py"
    giant_script.write_text(
        """import sys
for line in sys.stdin:
    if not line.strip(): continue
    # Emit 5MB without newline
    sys.stdout.write("X" * (5 * 1024 * 1024))
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    giant_bot = create_bot_artifact(giant_script, display_name="GiantLineBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_giant_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=giant_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, giant_bot, fixed, tmp_path / "work_giant")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION


def test_stderr_bounding(tmp_path: Path):
    """Verify bot flooding stderr is bounded to 500 lines / 64KB and terminated as PROTOCOL_VIOLATION."""
    flood_script = tmp_path / "stderr_flood_bot.py"
    flood_script.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    for i in range(2000):
        sys.stderr.write(f"FLOODING_STDERR_LOG_LINE_{i}\\n")
    sys.stderr.flush()
    sys.stdout.write(json.dumps({"type": "CLAIM"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    flood_bot = create_bot_artifact(flood_script, display_name="StderrFloodBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_stderr_flood_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=flood_bot.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, flood_bot, fixed, tmp_path / "work_flood")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION


def test_missing_artifact_snapshot_fail_closed(tmp_path: Path):
    """Verify match fails closed with INFRASTRUCTURE_FAILURE if artifact snapshot entrypoint is missing."""
    mock = get_adapter("mock")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")

    # Construct artifact with non-existent snapshot path
    broken_artifact = BotArtifact(
        artifact_id="art_missing_snapshot",
        display_name="MissingSnapshotBot",
        source_location=str(tmp_path / "non_existent_snapshot_dir"),
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="0000000000000000",
        entrypoint_relpath="bot.py",
        manifest={},
    )
    spec = MatchSpec(
        match_id="m_missing_snap_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=broken_artifact.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, broken_artifact, fixed, tmp_path / "work_missing")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.UNKNOWN_INFRASTRUCTURE
    assert "No recorded executable entrypoint found" in res.failure_classification.evidence


def test_windows_process_tree_containment_descendant_race(tmp_path: Path):
    """Verify best-effort termination of descendant process tree during child spawning race."""
    code = """import subprocess, sys, time
# Rapidly spawn multiple background children
children = []
for _ in range(3):
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    children.append(c.pid)
print(children, flush=True)
while True:
    time.sleep(0.5)
"""
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )
    else:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    assert proc.stdout is not None
    pids_line = proc.stdout.readline().strip()
    import ast

    child_pids = ast.literal_eval(pids_line)

    terminate_process_tree(proc, timeout_seconds=1.5)
    time.sleep(0.2)

    assert not is_process_active(proc.pid)
    for c_pid in child_pids:
        assert not is_process_active(c_pid)


def test_pythonpath_and_secret_isolation(tmp_path: Path, monkeypatch):
    """Verify that bot execution isolates parent PYTHONPATH and coordinator secrets."""
    # 1. External mutable module outside artifact snapshot
    external_dir = tmp_path / "external_leak"
    external_dir.mkdir()
    (external_dir / "leaked_secret_module.py").write_text(
        "FLAG = 'coordinator_leaked_module'\n", encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(external_dir))
    monkeypatch.setenv("COORDINATOR_SECRET_KEY", "super_secret_coordinator_token_12345")

    # 2. Registered artifact with local helper module and main entrypoint
    src_dir = tmp_path / "bot_src"
    src_dir.mkdir()
    (src_dir / "local_helper.py").write_text(
        "HELPER_TOKEN = 'valid_local_import'\n", encoding="utf-8"
    )
    (src_dir / "bot.py").write_text(
        """import sys, json, os

# 1. External import must fail
external_leaked = False
try:
    import leaked_secret_module
    external_leaked = True
except ImportError:
    external_leaked = False

# 2. Local import must succeed
try:
    import local_helper
    local_ok = (local_helper.HELPER_TOKEN == 'valid_local_import')
except ImportError:
    local_ok = False

# 3. Coordinator secret env var must not exist
secret_leaked = bool(os.environ.get("COORDINATOR_SECRET_KEY"))

for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    if not external_leaked and local_ok and not secret_leaked:
        act = {"type": "CLAIM"}
    else:
        act = {"type": "FAIL_LEAK", "ext": external_leaked, "local": local_ok, "sec": secret_leaked}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    art = create_bot_artifact(src_dir, display_name="IsolationBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_isolation_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_iso")
    assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)
    assert not res.crashed_a
    assert not res.protocol_violation_a
    assert res.score_a > 0


def test_stdout_queue_spam_bounding(tmp_path: Path):
    """Stress test: bot spamming stdout without turn inputs is bounded to max capacity and killed."""
    from battlelab.bots.process_runner import BotSubprocess

    spam_dir = tmp_path / "spam_bot"
    spam_dir.mkdir()
    (spam_dir / "bot.py").write_text(
        """import sys, time, json
for i in range(1000):
    sys.stdout.write(json.dumps({"type": "SPAM", "i": i}) + "\\n")
    sys.stdout.flush()
while True:
    time.sleep(0.5)
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(spam_dir, display_name="SpamQueueBot")
    entrypoint = Path(art.source_location) / "bot.py"
    proc = BotSubprocess(
        entrypoint_path=entrypoint,
        cwd=entrypoint.parent,
        max_stdout_queue_capacity=16,
    )
    proc.start()
    assert proc.proc is not None
    pid = proc.proc.pid
    time.sleep(0.5)

    # Invariant: queue size never exceeds bounded capacity
    assert proc.stdout_queue.qsize() <= 16
    assert proc.max_observed_queue_size <= 16

    # Verify protocol violation classification and termination
    action, status = proc.send_turn({"turn": 1}, timeout_seconds=1.0)
    assert status["protocol_violation"]
    assert "queue capacity" in proc.protocol_violation_reason.lower()
    assert not is_process_active(pid)


def test_laboratory_state_tampering_detected(tmp_path: Path, monkeypatch):
    """Adversarial test: verify that bots attempting to mutate lab assets are caught and disqualified."""
    import json

    mock = get_adapter("mock")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")

    # Set isolated paths
    db_file = tmp_path / "test_lab.db"
    db_file.write_text("INITIAL_DB_CONTENT", encoding="utf-8")
    champ_manifest = tmp_path / "champion_manifest.json"
    champ_manifest.write_text(json.dumps({"champion": "legit"}), encoding="utf-8")

    monkeypatch.setenv("BATTLELAB_DATABASE_PATH", str(db_file))
    monkeypatch.setenv("BATTLELAB_CHAMPION_MANIFEST", str(champ_manifest))
    monkeypatch.setenv("BATTLELAB_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    # Vector A: Bot modifies champion manifest
    tamper_champ_dir = tmp_path / "bot_tamper_champ"
    tamper_champ_dir.mkdir()
    champ_posix = champ_manifest.resolve().as_posix()
    (tamper_champ_dir / "bot.py").write_text(
        f"""import sys, json, pathlib
p = pathlib.Path("{champ_posix}")
if p.exists():
    p.write_text(json.dumps({{"champion": "hacked"}}))
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
    art_a = create_bot_artifact(tamper_champ_dir, display_name="TamperChampBot")
    spec_a = MatchSpec(
        match_id="m_tamper_champ",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art_a.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res_a = mock.run_local_match(spec_a, art_a, fixed, tmp_path / "work_a")
    assert res_a.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res_a.failure_classification is not None
    assert res_a.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "champion manifest tampering detected" in res_a.failure_classification.evidence

    # Restore champ manifest
    champ_manifest.write_text(json.dumps({"champion": "legit"}), encoding="utf-8")

    # Vector B: Bot attempts cross-artifact corruption
    victim_art = create_bot_artifact(
        Path("bots/baselines/resource_bot.py"), display_name="VictimBot"
    )
    tamper_art_dir = tmp_path / "bot_tamper_art"
    tamper_art_dir.mkdir()
    victim_manifest_posix = (
        (Path(victim_art.source_location) / "manifest.json").resolve().as_posix()
    )
    (tamper_art_dir / "bot.py").write_text(
        f"""import sys, json, pathlib, os, stat
victim_manifest = pathlib.Path("{victim_manifest_posix}")
if victim_manifest.exists():
    try:
        os.chmod(victim_manifest, stat.S_IWRITE)
        victim_manifest.write_text(json.dumps({{"corrupted": True}}))
    except Exception:
        pass
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
    art_b = create_bot_artifact(tamper_art_dir, display_name="TamperArtBot")
    spec_b = MatchSpec(
        match_id="m_tamper_art",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art_b.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res_b = mock.run_local_match(spec_b, art_b, fixed, tmp_path / "work_b")
    assert res_b.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res_b.failure_classification is not None
    assert res_b.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "Cross-artifact tampering detected" in res_b.failure_classification.evidence

    # Vector C: Bot attempts database file tampering
    tamper_db_dir = tmp_path / "bot_tamper_db"
    tamper_db_dir.mkdir()
    db_posix = db_file.resolve().as_posix()
    (tamper_db_dir / "bot.py").write_text(
        f"""import sys, json, pathlib
p = pathlib.Path("{db_posix}")
if p.exists():
    p.write_text("CORRUPTED_DATABASE_CONTENTS")
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
    art_c = create_bot_artifact(tamper_db_dir, display_name="TamperDbBot")
    spec_c = MatchSpec(
        match_id="m_tamper_db",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art_c.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res_c = mock.run_local_match(spec_c, art_c, fixed, tmp_path / "work_c")
    assert res_c.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res_c.failure_classification is not None
    assert res_c.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "database tampering detected" in res_c.failure_classification.evidence.lower()


def test_outbound_network_capability_detection(tmp_path: Path):
    """Verify mock bot outbound network capability is truthfully documented as NOT_IMPLEMENTED."""
    from battlelab.bots.process_runner import get_containment_capabilities

    caps = get_containment_capabilities()
    assert caps["network_isolation"]["status"] == "NOT_IMPLEMENTED"

    # Demonstrate that an unisolated mock bot can open local socket in current execution model
    net_dir = tmp_path / "net_bot"
    net_dir.mkdir()
    (net_dir / "bot.py").write_text(
        """import sys, json, socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('127.0.0.1', 0))
port = sock.getsockname()[1]
sock.close()

for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    act = {"type": "CLAIM", "bound_port": port}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(net_dir, display_name="SocketTestBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_net_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_net")
    # Match completes, confirming outbound network socket succeeds and is NOT_IMPLEMENTED
    assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)


def test_utf8_multibyte_payload_exceeding_byte_limit(tmp_path: Path):
    """Verify stdout framing enforces max_line_bytes (64KB) on raw UTF-8 bytes, not characters.

    25,000 euro signs ('€') = 25,000 characters, but 75,000 bytes in UTF-8.
    Text readline(65536) would allow up to 65,536 characters (196,608 bytes), failing wire bounds.
    Binary framing strictly bounds wire transmission to max_line_bytes.
    """
    bot_dir = tmp_path / "bot_multibyte"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text(
        """import sys
# 25,000 '€' characters = 75,000 UTF-8 bytes
euro_msg = "€" * 25000 + "\\n"
for line in sys.stdin:
    if not line.strip(): continue
    sys.stdout.write(euro_msg)
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_dir, display_name="MultiByteOverflowBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_multibyte_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_mb")
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert "exceeded maximum allowed bytes" in res.failure_classification.evidence.lower()


def test_malformed_utf8_protocol_bytes_rejection(tmp_path: Path):
    """Verify raw non-UTF-8 bytes on stdout are rejected as PROTOCOL_VIOLATION without coordinator crash."""
    bot_dir = tmp_path / "bot_malformed_bytes"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text(
        """import sys
for line in sys.stdin:
    if not line.strip(): continue
    # Write invalid UTF-8 bytes followed by newline
    sys.stdout.buffer.write(b"\\xff\\xfe\\x00\\x00\\n")
    sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_dir, display_name="MalformedBytesBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_malformed_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_mf")
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert "utf-8" in res.failure_classification.evidence.lower()


def test_mock_python_code_boundary_import_isolation(tmp_path: Path):
    """Verify MOCK_PYTHON_CODE_BOUNDARY = IMMUTABLE_ARTIFACT + PYTHON_STDLIB.

    A mock bot running in isolated mode (-I -S):
    1. CAN import pure-Python stdlib modules not pre-imported by bootstrap (pathlib, statistics, fractions, random).
    2. CAN import sibling modules inside its immutable artifact snapshot.
    3. CANNOT import mutable 'battlelab' from coordinator installation.
    4. CANNOT import modules from parent PYTHONPATH.
    5. CANNOT import modules from site-packages (e.g. psutil).
    6. sys.path contains [immutable_artifact_root, *safe_stdlib_paths] and excludes
       repo root, site-packages, user-site, and external PYTHONPATH.
    """
    import json
    import os
    from pathlib import Path

    from battlelab.bots.process_runner import MOCK_PYTHON_CODE_BOUNDARY

    assert MOCK_PYTHON_CODE_BOUNDARY == "IMMUTABLE_ARTIFACT + PYTHON_STDLIB"

    repo_root = Path.cwd().resolve()

    # Set external PYTHONPATH in coordinator process
    external_dir = tmp_path / "external_pth"
    external_dir.mkdir()
    (external_dir / "external_forbidden_mod.py").write_text("SECRET = 'leaked'\n", encoding="utf-8")
    os.environ["PYTHONPATH"] = str(external_dir.resolve())

    # Build bot artifact with sibling module inside artifact dir
    bot_dir = tmp_path / "artifact_snapshot_bot"
    bot_dir.mkdir()
    (bot_dir / "sibling_mod.py").write_text("SIBLING_CONST = 42\n", encoding="utf-8")

    proof_file = tmp_path / "isolation_proof.json"
    proof_posix = proof_file.resolve().as_posix()

    (bot_dir / "bot.py").write_text(
        f"""import sys, json, math, os
# 1. Pure-Python standard library imports NOT pre-imported by bootstrap
import pathlib
import statistics
import fractions
import random

frac = fractions.Fraction(1, 3) + fractions.Fraction(1, 6)
stat_mean = statistics.mean([10, 20, 30])
rand_choice = random.choice([42, 42, 42])
p_test = pathlib.Path(".")
stdlib_ok = (
    frac == fractions.Fraction(1, 2)
    and stat_mean == 20
    and rand_choice == 42
    and p_test.exists()
    and math.sqrt(16) == 4.0
)

# 2. Sibling module import inside immutable artifact works
import sibling_mod
sibling_ok = (sibling_mod.SIBLING_CONST == 42)

# 3. Mutable battlelab package MUST NOT be importable
battlelab_blocked = False
try:
    import battlelab
except ModuleNotFoundError:
    battlelab_blocked = True

# 4. Parent PYTHONPATH / external module MUST NOT be importable
external_blocked = False
try:
    import external_forbidden_mod
except ModuleNotFoundError:
    external_blocked = True

# 5. Site-packages only package (e.g. psutil) MUST NOT be importable
site_packages_blocked = False
try:
    import psutil
except ModuleNotFoundError:
    site_packages_blocked = True

# Record verification proof file including final sys.path
with open("{proof_posix}", "w", encoding="utf-8") as f:
    json.dump({{
        "stdlib_ok": stdlib_ok,
        "sibling_ok": sibling_ok,
        "battlelab_blocked": battlelab_blocked,
        "external_blocked": external_blocked,
        "site_packages_blocked": site_packages_blocked,
        "bot_sys_path": list(sys.path),
    }}, f)

# Normal protocol execution
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    resp = {{"type": "MOVE", "direction": "UP"}}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        resp["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    art = create_bot_artifact(bot_dir, display_name="IsolatedBoundaryBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_boundary_iso_test",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_iso")
    assert res.outcome != MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)

    # Inspect isolated execution proof
    assert proof_file.exists(), "Bot failed to write isolation proof"
    proof = json.loads(proof_file.read_text(encoding="utf-8"))
    assert proof["stdlib_ok"] is True
    assert proof["sibling_ok"] is True
    assert proof["battlelab_blocked"] is True
    assert proof["external_blocked"] is True
    assert proof["site_packages_blocked"] is True

    bot_paths = proof["bot_sys_path"]
    assert len(bot_paths) >= 2, "sys.path should contain artifact root and stdlib paths"

    # 2. Approved Python stdlib/runtime paths
    repo_root = Path.cwd().resolve()
    external_dir_resolved = external_dir.resolve()
    base_prefix_path = Path(sys.base_prefix).resolve()
    prefix_path = Path(sys.prefix).resolve()

    import site

    user_site = site.getusersitepackages() if hasattr(site, "getusersitepackages") else None
    user_site_path = Path(user_site).resolve() if user_site and isinstance(user_site, str) else None

    for i, p in enumerate(bot_paths):
        p_path = Path(p).resolve()
        p_low = str(p_path).lower()

        # Disallowed locations
        assert "site-packages" not in p_low, f"Found site-packages in sys.path: {p}"
        assert "dist-packages" not in p_low, f"Found dist-packages in sys.path: {p}"
        assert p_path != repo_root, f"Found repository root in sys.path: {p}"
        assert not p_path.is_relative_to(repo_root / "src"), f"Found src checkout in sys.path: {p}"
        assert p_path != external_dir_resolved, f"Found external PYTHONPATH dir in sys.path: {p}"
        assert not p_path.is_relative_to(external_dir_resolved), (
            f"Found path inside external dir: {p}"
        )
        if user_site_path:
            assert p_path != user_site_path, f"Found user-site in sys.path: {p}"

        # First path is artifact root; remaining paths must belong to Python stdlib/runtime
        if i == 0:
            assert p_path == Path(art.source_location).resolve()
        else:
            is_runtime = (
                p_path.is_relative_to(base_prefix_path)
                or p_path.is_relative_to(prefix_path)
                or p.endswith(".zip")
            )
            assert is_runtime, f"Path not in Python stdlib/runtime: {p}"


def test_promotion_gate_rollback_audit_hardening(tmp_path: Path):
    """Verify PromotionGate.rollback requires explicit, named non-generic actor and non-empty reason."""
    from datetime import datetime, timezone

    import pytest

    from battlelab.bots.registry import BotRegistry
    from battlelab.experiments.promotion import PromotionGate, PromotionGateError
    from battlelab.storage.database import Database

    db_path = tmp_path / "rollback_test.db"
    db = Database(db_path)
    registry = BotRegistry(db)
    gate = PromotionGate(db=db)

    art1 = registry.register_bot(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    art2 = registry.register_bot(Path("bots/baselines/random_bot.py"), display_name="Random")

    # Set art2 as initial champion
    registry.update_champion_manifest(
        artifact_id=art2.artifact_id,
        experiment_id="init",
        reason="Initial champion",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    # 1. Blank or whitespace actor rejected
    with pytest.raises(
        PromotionGateError, match="Rollback requires an explicit, named non-generic actor"
    ):
        gate.rollback(art1.artifact_id, reason="Testing rollback", actor="")

    with pytest.raises(
        PromotionGateError, match="Rollback requires an explicit, named non-generic actor"
    ):
        gate.rollback(art1.artifact_id, reason="Testing rollback", actor="   ")

    # 2. Generic actor rejected
    for generic in ("human", "default", "unknown", "HUMAN", "Default"):
        with pytest.raises(
            PromotionGateError, match="Rollback requires an explicit, named non-generic actor"
        ):
            gate.rollback(art1.artifact_id, reason="Testing rollback", actor=generic)

    # 3. Blank or whitespace reason rejected
    with pytest.raises(
        PromotionGateError, match="Rollback requires an explicit, meaningful non-empty reason"
    ):
        gate.rollback(art1.artifact_id, reason="", actor="dr_researcher")

    with pytest.raises(
        PromotionGateError, match="Rollback requires an explicit, meaningful non-empty reason"
    ):
        gate.rollback(art1.artifact_id, reason="   ", actor="dr_researcher")

    # 4. Valid audited rollback succeeds and records full audit trail
    res = gate.rollback(
        historical_artifact_id=art1.artifact_id,
        reason="Reverting degraded model due to map regression",
        actor="lead_researcher_carol",
    )
    assert res["status"] == "ROLLED_BACK"
    assert res["champion_artifact_id"] == art1.artifact_id
    assert res["promoted_by"] == "lead_researcher_carol"
    assert "Reverting degraded model" in res["reason"]

    # Verify manifest updated
    champ = registry.get_champion_artifact()
    assert champ is not None
    assert champ.artifact_id == art1.artifact_id

    # Verify promotions table record
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM promotions WHERE promotion_id = ?", (res["promotion_id"],)
        ).fetchone()
        assert row is not None
        assert row["artifact_id"] == art1.artifact_id
        assert row["previous_champion_id"] == art2.artifact_id
        assert row["promoted_by"] == "lead_researcher_carol"
        assert row["mode"] == "ROLLBACK"
        assert row["artifact_manifest_hash"] == art1.manifest_hash


def test_windows_job_object_create_failure_fails_closed(tmp_path: Path, monkeypatch):
    """Simulate Windows CreateJobObjectW failure: bot must not run, match fails closed as INFRASTRUCTURE_FAILURE."""
    if sys.platform != "win32":
        pytest.skip("Windows Job Object tests require Windows")

    import battlelab.bots.process_runner as pr

    marker_file = tmp_path / "executed_marker.txt"
    bot_file = tmp_path / "fail_closed_bot.py"
    bot_file.write_text(
        f"""import pathlib, sys
pathlib.Path(r"{marker_file.resolve()}").write_text("EXECUTED")
for line in sys.stdin:
    sys.stdout.write('{{"type": "PASS"}}\\n')
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_file, display_name="FailClosedBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_job_create_fail",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )

    # Monkeypatch CreateJobObjectW to return 0/NULL
    monkeypatch.setattr(pr.kernel32, "CreateJobObjectW", lambda sec, name: None)

    runner = pr.BotSubprocess(entrypoint_path=bot_file, cwd=tmp_path)
    with pytest.raises(pr.BotStartupError, match="Windows Job Object creation failed"):
        runner.start()
    assert not runner.is_alive
    assert runner.proc is None

    # Run through MockAdapter: reports INFRASTRUCTURE_FAILURE
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_fail_create")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.UNKNOWN_INFRASTRUCTURE
    assert not marker_file.exists()


def test_windows_job_object_assign_failure_fails_closed(tmp_path: Path, monkeypatch):
    """Simulate Windows AssignProcessToJobObject failure: suspended process is terminated, PID dead, fails closed."""
    if sys.platform != "win32":
        pytest.skip("Windows Job Object tests require Windows")

    import battlelab.bots.process_runner as pr

    marker_file = tmp_path / "assign_marker.txt"
    bot_file = tmp_path / "fail_assign_bot.py"
    bot_file.write_text(
        f"""import pathlib, sys
pathlib.Path(r"{marker_file.resolve()}").write_text("EXECUTED")
for line in sys.stdin:
    sys.stdout.write('{{"type": "PASS"}}\\n')
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    captured_pids = []
    orig_popen = subprocess.Popen

    def mock_popen(*args, **kwargs):
        p = orig_popen(*args, **kwargs)
        captured_pids.append(p.pid)
        return p

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    # Monkeypatch AssignProcessToJobObject to return 0/False
    monkeypatch.setattr(pr.kernel32, "AssignProcessToJobObject", lambda job, proc: 0)

    runner = pr.BotSubprocess(entrypoint_path=bot_file, cwd=tmp_path)
    with pytest.raises(pr.BotStartupError, match="Failed to assign bot process"):
        runner.start()
    assert not runner.is_alive
    assert runner.proc is None

    assert len(captured_pids) == 1
    child_pid = captured_pids[0]
    time.sleep(0.1)
    assert not is_process_active(child_pid)
    assert not marker_file.exists()


def test_windows_job_object_resume_failure_fails_closed(tmp_path: Path, monkeypatch):
    """Simulate Windows thread resume failure: suspended process terminated, PID dead, fails closed."""
    if sys.platform != "win32":
        pytest.skip("Windows Job Object tests require Windows")

    import battlelab.bots.process_runner as pr

    marker_file = tmp_path / "resume_marker.txt"
    bot_file = tmp_path / "fail_resume_bot.py"
    bot_file.write_text(
        f"""import pathlib, sys
pathlib.Path(r"{marker_file.resolve()}").write_text("EXECUTED")
for line in sys.stdin:
    sys.stdout.write('{{"type": "PASS"}}\\n')
    sys.stdout.flush()
""",
        encoding="utf-8",
    )

    captured_pids = []
    orig_popen = subprocess.Popen

    def mock_popen(*args, **kwargs):
        p = orig_popen(*args, **kwargs)
        captured_pids.append(p.pid)
        return p

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    # Monkeypatch resume_process_threads to return False
    monkeypatch.setattr(pr, "resume_process_threads", lambda pid: False)

    runner = pr.BotSubprocess(entrypoint_path=bot_file, cwd=tmp_path)
    with pytest.raises(pr.BotStartupError, match="Failed to resume suspended threads"):
        runner.start()
    assert not runner.is_alive
    assert runner.proc is None

    assert len(captured_pids) == 1
    child_pid = captured_pids[0]
    time.sleep(0.1)
    assert not is_process_active(child_pid)
    assert not marker_file.exists()


def test_giant_unterminated_stderr_bounded_and_terminated(tmp_path: Path):
    """Adversarial test: bot emits 25MB to stderr with NO newlines.

    Proves:
    1. Coordinator survives without memory exhaustion.
    2. Retained stderr is <= bound + truncation marker.
    3. Bot is terminated immediately as PROTOCOL_VIOLATION.
    4. Child process is killed and does not remain active.
    """
    bot_file = tmp_path / "giant_stderr_bot.py"
    bot_file.write_text(
        """import sys
# Emit 25MB to stderr with NO newlines
chunk = "E" * 65536
for _ in range(400):
    sys.stderr.write(chunk)
    sys.stderr.flush()
while True:
    pass
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_file, display_name="GiantStderrBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_giant_stderr",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_stderr_giant")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert res.protocol_violation_a is True
    # Verify stderr lines and byte boundedness
    full_stderr = res.failure_classification.evidence or ""
    assert "[STDERR TRUNCATED: Exceeded line/byte limits]" in full_stderr


def test_turn_correlation_missing_request_id(tmp_path: Path):
    """Verify response missing _battlelab_request_id is rejected as PROTOCOL_VIOLATION."""
    bot_file = tmp_path / "missing_id_bot.py"
    bot_file.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    # Return action without _battlelab_request_id
    sys.stdout.write(json.dumps({"type": "CLAIM"}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_file, display_name="MissingIdBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_missing_id",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_missing_id")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert "missing required internal request id" in res.failure_classification.evidence.lower()


def test_turn_correlation_mismatched_request_id(tmp_path: Path):
    """Verify response with incorrect _battlelab_request_id is rejected as PROTOCOL_VIOLATION."""
    bot_file = tmp_path / "mismatched_id_bot.py"
    bot_file.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    req_id = state.get("_battlelab_request_id", 0)
    # Forge wrong request ID
    sys.stdout.write(json.dumps({"type": "CLAIM", "_battlelab_request_id": req_id + 999}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_file, display_name="MismatchedIdBot")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="Fixed")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_mismatch_id",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, fixed, tmp_path / "work_mismatch_id")
    assert res.outcome == MatchOutcome.WIN_B
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.PROTOCOL_VIOLATION
    assert "request id mismatch" in res.failure_classification.evidence.lower()


def test_turn_correlation_delayed_stale_action(tmp_path: Path):
    """Verify delayed stale action from request 1 is rejected on request 2 as PROTOCOL_VIOLATION."""
    bot_file = tmp_path / "delayed_stale_bot.py"
    bot_file.write_text(
        """import sys, json, time, threading
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    req_id = state.get("_battlelab_request_id", 0)
    if req_id == 1:
        # Return valid PASS for request 1 immediately
        sys.stdout.write(json.dumps({"type": "PASS", "_battlelab_request_id": 1}) + "\\n")
        sys.stdout.flush()
        # In background, wait 20ms then emit stale duplicate action with request ID 1
        def delayed():
            time.sleep(0.02)
            sys.stdout.write(json.dumps({"type": "PASS", "_battlelab_request_id": 1}) + "\\n")
            sys.stdout.flush()
        threading.Thread(target=delayed, daemon=True).start()
    elif req_id == 2:
        # Deliberately delay legitimate response so stale ID 1 arrives first
        time.sleep(0.15)
        sys.stdout.write(json.dumps({"type": "PASS", "_battlelab_request_id": 2}) + "\\n")
        sys.stdout.flush()
""",
        encoding="utf-8",
    )
    runner = BotSubprocess(entrypoint_path=bot_file, cwd=tmp_path)
    runner.start()
    try:
        # Request 1: returns immediately with request ID 1
        act1, status1 = runner.send_turn({"turn": 1}, timeout_seconds=2.0)
        assert act1 == {"type": "PASS"}
        assert status1["protocol_violation"] is False

        # Request 2: bot delays legitimate response by 150ms; stale ID 1 arrives in ~20ms
        act2, status2 = runner.send_turn({"turn": 2}, timeout_seconds=2.0)
        assert act2 is None
        assert status2["protocol_violation"] is True
        assert "mismatch" in status2["reason"].lower()
        assert runner.protocol_violation is True
    finally:
        runner.stop()


def test_turn_correlation_multi_turn_success_zero_delay(tmp_path: Path):
    """Verify normal bot successfully completes turns with 0ms artificial sleep."""
    bot_file = tmp_path / "fast_echo_bot.py"
    bot_file.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    turn = state.get("turn", 0)
    act = {"type": "CLAIM" if turn <= 1 else "PASS"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    bot_b_file = tmp_path / "fast_echo_bot_b.py"
    bot_b_file.write_text(
        """import sys, json
for line in sys.stdin:
    if not line.strip(): continue
    state = json.loads(line)
    if state.get("event") == "SHUTDOWN": break
    act = {"type": "PASS"}
    req_id = state.get("_battlelab_request_id")
    if req_id is not None:
        act["_battlelab_request_id"] = req_id
    sys.stdout.write(json.dumps(act) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    art = create_bot_artifact(bot_file, display_name="FastEchoBot")
    bot_b = create_bot_artifact(bot_b_file, display_name="FastEchoBotB")
    mock = get_adapter("mock")
    spec = MatchSpec(
        match_id="m_fast_echo",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=art.artifact_id,
        bot_b_id=bot_b.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, art, bot_b, tmp_path / "work_fast")
    assert res.outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)
    assert (
        res.failure_classification is None
        or res.failure_classification.category == FailureCategory.GAMEPLAY_LOSS
    )
    assert not res.protocol_violation_a
    assert not res.protocol_violation_b
    assert not res.crashed_a and not res.crashed_b
    assert not res.timed_out_a and not res.timed_out_b
    assert res.turns_played > 1


def test_unreadable_champion_manifest_pre_match_fails_closed(tmp_path: Path, monkeypatch):
    """Verify that unreadable champion manifest pre-match causes coordinator to fail closed."""
    import battlelab.adapters.mock.adapter as mock_adapter_mod

    champ_file = tmp_path / "champion.json"
    champ_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BATTLELAB_CHAMPION_MANIFEST", str(champ_file))

    orig_get_state = mock_adapter_mod.get_protected_file_state

    def fake_get_state(p):
        if Path(p).resolve() == champ_file.resolve():
            return ProtectedFileState(
                exists=True, readable=False, error="Permission denied [Errno 13]"
            )
        return orig_get_state(p)

    monkeypatch.setattr(mock_adapter_mod, "get_protected_file_state", fake_get_state)

    mock = get_adapter("mock")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="FixedPreChamp")
    spec = MatchSpec(
        match_id="m_pre_champ_unreadable",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=fixed.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, fixed, fixed, tmp_path / "work_pre_champ")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "Pre-match champion manifest cannot be verified" in res.failure_classification.evidence


def test_unreadable_champion_manifest_post_match_tampering_detected(tmp_path: Path, monkeypatch):
    """Verify that champion manifest becoming unreadable post-match is detected as tampering."""
    import battlelab.adapters.mock.adapter as mock_adapter_mod

    champ_file = tmp_path / "champion.json"
    champ_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BATTLELAB_CHAMPION_MANIFEST", str(champ_file))

    orig_get_state = mock_adapter_mod.get_protected_file_state
    champ_call_count = 0

    def fake_get_state(p):
        nonlocal champ_call_count
        if Path(p).resolve() == champ_file.resolve():
            champ_call_count += 1
            if champ_call_count > 1:
                return ProtectedFileState(
                    exists=True, readable=False, error="Permission denied [Errno 13]"
                )
        return orig_get_state(p)

    monkeypatch.setattr(mock_adapter_mod, "get_protected_file_state", fake_get_state)

    mock = get_adapter("mock")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="FixedPostChamp")
    spec = MatchSpec(
        match_id="m_post_champ_unreadable",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=fixed.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, fixed, fixed, tmp_path / "work_post_champ")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "champion manifest tampering detected" in res.failure_classification.evidence


def test_unreadable_cross_artifact_manifest_post_match_tampering_detected(
    tmp_path: Path, monkeypatch
):
    """Verify that cross-artifact manifest becoming unreadable post-match is detected as tampering."""
    import battlelab.adapters.mock.adapter as mock_adapter_mod

    art_root = tmp_path / "artifacts"
    art_root.mkdir()
    monkeypatch.setenv("BATTLELAB_ARTIFACTS_DIR", str(art_root))

    victim = create_bot_artifact(
        Path("bots/baselines/resource_bot.py"),
        display_name="VictimCross",
    )
    victim_manifest = Path(victim.source_location) / "manifest.json"
    assert victim_manifest.exists()

    orig_get_state = mock_adapter_mod.get_protected_file_state
    victim_call_count = 0

    def fake_get_state(p):
        nonlocal victim_call_count
        if Path(p).resolve() == victim_manifest.resolve():
            victim_call_count += 1
            if victim_call_count > 1:
                return ProtectedFileState(
                    exists=True, readable=False, error="Permission denied [Errno 13]"
                )
        return orig_get_state(p)

    monkeypatch.setattr(mock_adapter_mod, "get_protected_file_state", fake_get_state)

    mock = get_adapter("mock")
    fixed = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="FixedCross")
    spec = MatchSpec(
        match_id="m_cross_art_unreadable",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=fixed.artifact_id,
        bot_b_id=fixed.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, fixed, fixed, tmp_path / "work_cross_art")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "Cross-artifact tampering detected" in res.failure_classification.evidence


def test_unreadable_participant_manifest_post_match_tampering_detected(tmp_path: Path, monkeypatch):
    """Verify that participant manifest becoming unreadable post-match is detected as tampering."""
    import battlelab.adapters.mock.adapter as mock_adapter_mod

    mock = get_adapter("mock")
    bot_a = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="FixedPartA")
    bot_b = create_bot_artifact(Path("bots/baselines/fixed_bot.py"), display_name="FixedPartB")
    a_manifest = Path(bot_a.source_location) / "manifest.json"

    orig_get_state = mock_adapter_mod.get_protected_file_state
    a_call_count = 0

    def fake_get_state(p):
        nonlocal a_call_count
        if Path(p).resolve() == a_manifest.resolve():
            a_call_count += 1
            if a_call_count > 1:
                return ProtectedFileState(
                    exists=True, readable=False, error="Permission denied [Errno 13]"
                )
        return orig_get_state(p)

    monkeypatch.setattr(mock_adapter_mod, "get_protected_file_state", fake_get_state)

    spec = MatchSpec(
        match_id="m_part_art_unreadable",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id=bot_a.artifact_id,
        bot_b_id=bot_b.artifact_id,
        map_name="grid_tiny_4x4",
        seed=1,
    )
    res = mock.run_local_match(spec, bot_a, bot_b, tmp_path / "work_part_art")
    assert res.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE
    assert res.failure_classification is not None
    assert res.failure_classification.category == FailureCategory.STORAGE_FAILURE
    assert "manifest became unreadable" in res.failure_classification.evidence
