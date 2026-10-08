"""Centralized path management for the Battlelab platform."""

from __future__ import annotations

import os
from pathlib import Path


def get_project_root() -> Path:
    """Return the absolute path to the project root."""
    return Path(__file__).resolve().parent.parent.parent.parent


def load_dotenv_workspace() -> None:
    """Load season workspace env overrides from .env if present and not already set in environment."""
    env_file = get_project_root() / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


load_dotenv_workspace()


def get_data_dir() -> Path:
    data_env = os.environ.get("BATTLELAB_DATA_DIR")
    if data_env:
        p = Path(data_env)
        if not p.is_absolute():
            p = get_project_root() / p
        return p
    return get_project_root() / "data"


def get_artifacts_dir() -> Path:
    env_dir = os.environ.get("BATTLELAB_ARTIFACTS_DIR")
    if env_dir:
        p = Path(env_dir)
        if not p.is_absolute():
            p = get_project_root() / p
    else:
        p = get_data_dir() / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_replays_dir() -> Path:
    replays_env = os.environ.get("BATTLELAB_REPLAY_DIR")
    if replays_env:
        p = Path(replays_env)
        if not p.is_absolute():
            p = get_project_root() / p
    else:
        p = get_data_dir() / "replays"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_reports_dir() -> Path:
    reports_env = os.environ.get("BATTLELAB_REPORTS_DIR")
    if reports_env:
        p = Path(reports_env)
        if not p.is_absolute():
            p = get_project_root() / p
    else:
        p = get_data_dir() / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_runs_dir() -> Path:
    runs_env = os.environ.get("BATTLELAB_RUNS_DIR")
    if runs_env:
        p = Path(runs_env)
        if not p.is_absolute():
            p = get_project_root() / p
    else:
        p = get_data_dir() / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_scratch_dir() -> Path:
    scratch_env = os.environ.get("BATTLELAB_SCRATCH_DIR")
    if scratch_env:
        p = Path(scratch_env)
        if not p.is_absolute():
            p = get_project_root() / p
    else:
        p = get_data_dir() / "scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_manifests_dir() -> Path:
    p = get_data_dir() / "manifests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_database_path() -> Path:
    db_env = os.environ.get("BATTLELAB_DATABASE_PATH")
    if db_env:
        p = Path(db_env)
        if not p.is_absolute():
            p = get_project_root() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    db_path = get_data_dir() / "battlelab.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_champion_manifest_path() -> Path:
    manifest_env = os.environ.get("BATTLELAB_CHAMPION_MANIFEST")
    if manifest_env:
        p = Path(manifest_env)
        if not p.is_absolute():
            p = get_project_root() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    data_env = os.environ.get("BATTLELAB_DATA_DIR")
    if data_env:
        p = get_data_dir() / "champion_manifest.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    p = get_project_root() / "bots" / "champion" / "champion_manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_official_source_bundles_dir() -> Path:
    bundles_env = os.environ.get("BATTLELAB_OFFICIAL_BUNDLES_DIR")
    if bundles_env:
        p = Path(bundles_env)
        if not p.is_absolute():
            p = get_project_root() / p
    else:
        p = get_data_dir() / "official" / "source_bundles"
    p.mkdir(parents=True, exist_ok=True)
    return p
