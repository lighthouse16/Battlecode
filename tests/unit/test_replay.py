"""Unit tests for content-addressed replay store and integrity verification."""

from pathlib import Path
import pytest
from battlelab.core.errors import ReplayCorruptedError, ReplayError
from battlelab.telemetry.replay_store import ReplayStore

def test_replay_store_roundtrip(tmp_path: Path):
    store = ReplayStore(base_dir=tmp_path / "replays")

    temp_replay = tmp_path / "temp.jsonl"
    temp_replay.write_text('{"type": "META", "match_id": "m1"}\n{"turn": 1, "event": "MOVE"}\n', encoding="utf-8")

    dest_path, file_hash = store.store_replay(temp_replay)
    assert dest_path.exists()
    assert dest_path.name == f"{file_hash}.jsonl"

    # Verify integrity
    assert store.verify_stored_replay(file_hash) is True

def test_replay_corruption_detection(tmp_path: Path):
    store = ReplayStore(base_dir=tmp_path / "replays")

    corrupt_file = tmp_path / "corrupt.jsonl"
    corrupt_file.write_text('{"type": "META"}\nNOT_VALID_JSON_HERE\n', encoding="utf-8")

    with pytest.raises(ReplayCorruptedError):
        store.store_replay(corrupt_file)

def test_empty_replay_rejection(tmp_path: Path):
    store = ReplayStore(base_dir=tmp_path / "replays")
    empty_file = tmp_path / "empty.jsonl"
    empty_file.touch()

    with pytest.raises(ReplayError):
        store.store_replay(empty_file)
