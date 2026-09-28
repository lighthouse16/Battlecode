"""Contract tests for GameAdapter implementations."""

from pathlib import Path
import pytest
from battlelab.adapters import get_adapter, list_adapters
from battlelab.core.errors import CapabilityNotSupportedError
from battlelab.core.models import BotArtifact, MatchSpec, MatchOutcome

def test_adapter_listing():
    adapters = list_adapters()
    assert "mock" in adapters
    assert "official_placeholder" in adapters

def test_mock_adapter_contract(tmp_path: Path):
    mock = get_adapter("mock")
    ok, msg = mock.validate_installation()
    assert ok is True
    caps = mock.get_capabilities()
    assert caps.can_run_local is True
    assert caps.can_run_remote is False

    maps = mock.discover_maps()
    assert len(maps) >= 3
    assert "grid_classic_8x8" in maps

    bot_a = BotArtifact(
        artifact_id="art_a",
        display_name="FixedBot",
        source_location="bots/fixed",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_a",
        tags=["policy:fixed"],
    )
    bot_b = BotArtifact(
        artifact_id="art_b",
        display_name="RandomBot",
        source_location="bots/random",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_b",
        tags=["policy:random"],
    )

    compat_a, _ = mock.validate_bot_compatibility(bot_a)
    assert compat_a is True

    spec = MatchSpec(
        match_id="m_det1",
        adapter_name="mock",
        adapter_version=mock.version,
        bot_a_id="art_a",
        bot_b_id="art_b",
        map_name="grid_classic_8x8",
        seed=12345,
    )

    res1 = mock.run_local_match(spec, bot_a, bot_b, tmp_path / "run1")
    res2 = mock.run_local_match(spec, bot_a, bot_b, tmp_path / "run2")

    # Determinism verification
    assert res1.outcome == res2.outcome
    assert res1.score_a == res2.score_a
    assert res1.score_b == res2.score_b
    assert res1.turns_played == res2.turns_played
    assert res1.replay_hash == res2.replay_hash

    # Replay parse check
    assert res1.replay_path is not None
    parsed = mock.parse_replay(Path(res1.replay_path))
    assert parsed["total_frames"] > 0

def test_mock_seed_variation(tmp_path: Path):
    mock = get_adapter("mock")
    bot_a = BotArtifact(
        artifact_id="art_rand1",
        display_name="RandomBot",
        source_location="bots/rand",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_rand",
        tags=["policy:random"],
    )
    bot_b = BotArtifact(
        artifact_id="art_rand2",
        display_name="RandomBot2",
        source_location="bots/rand",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_rand",
        tags=["policy:random"],
    )

    spec1 = MatchSpec(
        match_id="m_seed1",
        adapter_name="mock",
        adapter_version="0.1.0",
        bot_a_id=bot_a.artifact_id,
        bot_b_id=bot_b.artifact_id,
        map_name="grid_classic_8x8",
        seed=1,
    )
    spec2 = MatchSpec(
        match_id="m_seed2",
        adapter_name="mock",
        adapter_version="0.1.0",
        bot_a_id=bot_a.artifact_id,
        bot_b_id=bot_b.artifact_id,
        map_name="grid_classic_8x8",
        seed=99999,
    )

    res1 = mock.run_local_match(spec1, bot_a, bot_b, tmp_path / "seed1")
    res2 = mock.run_local_match(spec2, bot_a, bot_b, tmp_path / "seed2")
    # Different seeds should produce different replay hashes or trajectories
    assert res1.replay_hash != res2.replay_hash

def test_official_placeholder_contract(tmp_path: Path):
    off = get_adapter("official_placeholder")
    ok, msg = off.validate_installation()
    assert ok is False
    caps = off.get_capabilities()
    assert caps.can_run_local is False
    assert caps.can_run_remote is False
    assert caps.can_submit is False

    dummy_bot = BotArtifact(
        artifact_id="art_x",
        display_name="Bot",
        source_location="bots/x",
        language="python",
        git_commit=None,
        dirty_worktree=False,
        source_hash="sha_x",
    )
    dummy_spec = MatchSpec(
        match_id="m_x",
        adapter_name="official",
        adapter_version="0.0.0",
        bot_a_id="art_x",
        bot_b_id="art_x",
        map_name="test",
        seed=1,
    )

    with pytest.raises(CapabilityNotSupportedError):
        off.run_local_match(dummy_spec, dummy_bot, dummy_bot, tmp_path)

    with pytest.raises(CapabilityNotSupportedError):
        off.run_remote_test(dummy_bot)

    with pytest.raises(CapabilityNotSupportedError):
        off.submit_artifact(dummy_bot, dry_run=True)
