"""Unit tests for SQLite concurrency and transaction resilience."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from battlelab.core.models import BotArtifact
from battlelab.storage.database import Database

def test_sqlite_concurrent_writes(tmp_path: Path):
    db_file = tmp_path / "concurrent.db"
    db = Database(db_file)

    def write_worker(idx: int):
        art = BotArtifact(
            artifact_id=f"art_thread_{idx}",
            display_name=f"Bot_{idx}",
            source_location="/tmp",
            language="python",
            git_commit=None,
            dirty_worktree=False,
            source_hash=f"hash_{idx}",
            created_at=f"2026-09-28T00:00:{idx:02d}Z",
        )
        db.save_artifact(art)
        return idx

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(write_worker, i) for i in range(20)]
        results = [f.result() for f in futures]

    assert len(results) == 20
    all_arts = db.list_artifacts()
    assert len(all_arts) == 20
