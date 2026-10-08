#!/usr/bin/env python3
"""Manage season-isolated competition workspace for Battlecode.

Ensures official competition state, artifacts, database, and active champion manifest
are strictly isolated from legacy pre-season mock state.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from battlelab.storage.paths import (
    get_champion_manifest_path,
    get_data_dir,
    get_database_path,
    get_project_root,
)


def get_workspace_paths(season: str = "autumn2026") -> tuple[Path, Path]:
    """Return absolute workspace dir and champion manifest paths for season."""
    root = get_project_root()
    workspace_dir = root / "data" / "competition" / season
    manifest_path = workspace_dir / "champion_manifest.json"
    return workspace_dir, manifest_path


def init_workspace(season: str = "autumn2026", write_dotenv: bool = True) -> dict[str, str]:
    """Initialize season-isolated competition workspace directory and persist overrides to .env."""
    root = get_project_root()
    workspace_dir, manifest_path = get_workspace_paths(season)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    rel_dir = f"data/competition/{season}"
    rel_manifest = f"data/competition/{season}/champion_manifest.json"

    if write_dotenv:
        env_file = root / ".env"
        lines = [
            f"# Battlecode Competition Workspace — {season}",
            f"BATTLELAB_DATA_DIR={rel_dir}",
            f"BATTLELAB_CHAMPION_MANIFEST={rel_manifest}",
            "",
        ]
        env_file.write_text("\n".join(lines), encoding="utf-8")

    # Set in current process
    os.environ["BATTLELAB_DATA_DIR"] = rel_dir
    os.environ["BATTLELAB_CHAMPION_MANIFEST"] = rel_manifest

    return {
        "workspace_dir": str(workspace_dir),
        "champion_manifest": str(manifest_path),
        "database_path": str(workspace_dir / "battlelab.db"),
        "rel_data_dir": rel_dir,
        "rel_champion_manifest": rel_manifest,
    }


def check_workspace_status(season: str = "autumn2026") -> dict[str, Any]:
    """Check whether the active environment is isolated or falling back to legacy repo defaults."""
    root = get_project_root()
    legacy_manifest = root / "bots" / "champion" / "champion_manifest.json"
    curr_data_dir = get_data_dir()
    curr_manifest = get_champion_manifest_path()
    curr_db = get_database_path()

    is_legacy = curr_manifest.resolve() == legacy_manifest.resolve()

    return {
        "season": season,
        "active_data_dir": str(curr_data_dir),
        "active_champion_manifest": str(curr_manifest),
        "active_database": str(curr_db),
        "is_isolated": not is_legacy,
        "legacy_manifest_path": str(legacy_manifest),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage season-isolated competition workspace.")
    parser.add_argument(
        "--season", default="autumn2026", help="Season identifier (default: autumn2026)"
    )
    parser.add_argument(
        "--status", action="store_true", help="Check current active workspace status"
    )
    parser.add_argument(
        "--no-dotenv", action="store_true", help="Do not write/update .env configuration"
    )
    args = parser.parse_args(argv)

    if args.status:
        st = check_workspace_status(args.season)
        print("BATTLELAB COMPETITION WORKSPACE STATUS:")
        isolated_str = "YES" if st["is_isolated"] else "NO (pointing to legacy default)"
        print(f"  Isolated Workspace:      {isolated_str}")
        print(f"  Data Directory:          {st['active_data_dir']}")
        print(f"  Champion Manifest:       {st['active_champion_manifest']}")
        print(f"  Database Path:           {st['active_database']}")
        if not st["is_isolated"]:
            print("\nNotice: Environment is currently using legacy repository default workspace.")
            print(
                "Run 'python scripts/competition_workspace.py' to activate an isolated season workspace."
            )
        return 0

    info = init_workspace(season=args.season, write_dotenv=not args.no_dotenv)
    print("=" * 65)
    print(f"BATTLELAB COMPETITION WORKSPACE INITIALIZED: {args.season}")
    print("=" * 65)
    print(f"  Data Directory:      {info['workspace_dir']}")
    print(f"  Champion Manifest:   {info['champion_manifest']}")
    print(f"  Database Path:       {info['database_path']}")
    if not args.no_dotenv:
        print("  Persisted to:        .env (persists across shells and tools)")
    print("-" * 65)
    print("Shell Environment Overrides (optional if .env is present):")
    print("  PowerShell:")
    print(f'    $env:BATTLELAB_DATA_DIR = "{info["rel_data_dir"]}"')
    print(f'    $env:BATTLELAB_CHAMPION_MANIFEST = "{info["rel_champion_manifest"]}"')
    print("  Bash / POSIX:")
    print(f'    export BATTLELAB_DATA_DIR="{info["rel_data_dir"]}"')
    print(f'    export BATTLELAB_CHAMPION_MANIFEST="{info["rel_champion_manifest"]}"')
    print("=" * 65)
    return 0


if __name__ == "__main__":
    sys.exit(main())
