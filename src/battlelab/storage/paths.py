"""Centralized path management for the Battlelab platform."""

from __future__ import annotations

import os
from pathlib import Path


def get_project_root() -> Path:
    """Return the absolute path to the project root."""
    return Path(__file__).resolve().parent.parent.parent.parent


def get_data_dir() -> Path:
    data_env = os.environ.get("BATTLELAB_DATA_DIR")
    if data_env:
        p = Path(data_env)
        if not p.is_absolute():
            p = get_project_root() / p
        return p
    return get_project_root() / "data"


def get_artifacts_dir() -> Path:
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
    p = get_data_dir() / "reports"
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
    p = get_project_root() / "bots" / "champion" / "champion_manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
