"""Pytest configuration and test isolation fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Generator

import pytest

from battlelab.storage.paths import get_project_root


@pytest.fixture(autouse=True)
def isolate_test_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Isolate database, storage paths, and champion manifest to a temporary directory."""
    temp_data = tmp_path / "data"
    temp_data.mkdir(parents=True, exist_ok=True)

    temp_db = temp_data / "test_battlelab.db"
    temp_artifacts = temp_data / "artifacts"
    temp_replays = temp_data / "replays"
    temp_reports = temp_data / "reports"
    temp_runs = temp_data / "runs"
    temp_scratch = temp_data / "scratch"
    temp_manifest = tmp_path / "champion" / "champion_manifest.json"
    temp_manifest.parent.mkdir(parents=True, exist_ok=True)

    real_manifest = get_project_root() / "bots" / "champion" / "champion_manifest.json"
    if real_manifest.exists():
        shutil.copy(real_manifest, temp_manifest)

    monkeypatch.setenv("BATTLELAB_DATA_DIR", str(temp_data))
    monkeypatch.setenv("BATTLELAB_DATABASE_PATH", str(temp_db))
    monkeypatch.setenv("BATTLELAB_ARTIFACTS_DIR", str(temp_artifacts))
    monkeypatch.setenv("BATTLELAB_REPLAY_DIR", str(temp_replays))
    monkeypatch.setenv("BATTLELAB_REPORTS_DIR", str(temp_reports))
    monkeypatch.setenv("BATTLELAB_RUNS_DIR", str(temp_runs))
    monkeypatch.setenv("BATTLELAB_SCRATCH_DIR", str(temp_scratch))
    monkeypatch.setenv("BATTLELAB_CHAMPION_MANIFEST", str(temp_manifest))

    yield
