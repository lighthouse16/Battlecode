#!/usr/bin/env python3
"""Manage season-isolated competition workspace for Battlecode.

Ensures official competition state, artifacts, database, and active champion manifest
are strictly isolated from legacy pre-season mock state.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from battlelab.storage.paths import (
    get_artifacts_dir,
    get_champion_manifest_path,
    get_data_dir,
    get_database_path,
    get_project_root,
)

SEASON_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

WORKSPACE_ENV_KEYS = {
    "BATTLELAB_DATA_DIR",
    "BATTLELAB_CHAMPION_MANIFEST",
    "BATTLELAB_DATABASE_PATH",
    "BATTLELAB_ARTIFACTS_DIR",
}


def get_default_env_file() -> Path:
    """Return canonical path to .env file, respecting BATTLELAB_ENV_FILE override."""
    custom_env = os.environ.get("BATTLELAB_ENV_FILE")
    return Path(custom_env) if custom_env else get_project_root() / ".env"


def validate_season_slug(season: str) -> str:
    """Validate season is a simple safe slug without traversal, separators, or absolute paths."""
    if not season or not isinstance(season, str):
        raise ValueError("Season identifier must be a non-empty string.")
    season_clean = season.strip()
    if "/" in season_clean or "\\" in season_clean or ".." in season_clean:
        raise ValueError(
            f"Invalid season '{season}': directory traversal characters ('..') and path separators ('/' or '\\') are forbidden."
        )
    if not SEASON_PATTERN.match(season_clean):
        raise ValueError(
            f"Invalid season '{season}'. Must be alphanumeric with optional hyphens or underscores (1-64 characters)."
        )
    return season_clean


def get_workspace_paths(season: str = "autumn2026") -> tuple[Path, Path]:
    """Return absolute workspace dir and champion manifest paths for season."""
    season = validate_season_slug(season)
    root = get_project_root()
    workspace_dir = root / "data" / "competition" / season
    manifest_path = workspace_dir / "champion_manifest.json"
    return workspace_dir, manifest_path


def update_dotenv_file(
    env_file: Path, updates: dict[str, str], comment_header: str | None = None
) -> None:
    """Safely update or append keys in .env preserving all unrelated lines, comments, and order."""
    existing_lines: list[str] = []
    if env_file.is_file():
        existing_lines = env_file.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    unseen_keys = [k for k in updates if k not in updated_keys]
    if unseen_keys:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        if comment_header:
            new_lines.append(comment_header)
        for k in unseen_keys:
            new_lines.append(f"{k}={updates[k]}")

    content = "\n".join(new_lines).rstrip() + "\n"
    env_file.write_text(content, encoding="utf-8")


def init_workspace(
    season: str = "autumn2026",
    write_dotenv: bool = True,
    env_file: Path | None = None,
) -> dict[str, str]:
    """Initialize season-isolated competition workspace directory and persist overrides to .env."""
    season = validate_season_slug(season)
    root = get_project_root()
    workspace_dir, manifest_path = get_workspace_paths(season)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    expected_db = (workspace_dir / "battlelab.db").resolve()
    expected_artifacts = (workspace_dir / "artifacts").resolve()

    rel_dir = f"data/competition/{season}"
    rel_manifest = f"data/competition/{season}/champion_manifest.json"

    # Pre-flight check: detect contradictory or unsupported overrides in caller's environment
    conflicts: list[str] = []
    if "BATTLELAB_DATABASE_PATH" in os.environ:
        db_p = Path(os.environ["BATTLELAB_DATABASE_PATH"])
        if not db_p.is_absolute():
            db_p = root / db_p
        if db_p.resolve() != expected_db:
            conflicts.append(
                f"BATTLELAB_DATABASE_PATH='{os.environ['BATTLELAB_DATABASE_PATH']}' differs from canonical season database ({expected_db}). "
                "Unset BATTLELAB_DATABASE_PATH to prevent cross-session identity drift."
            )

    if "BATTLELAB_ARTIFACTS_DIR" in os.environ:
        art_p = Path(os.environ["BATTLELAB_ARTIFACTS_DIR"])
        if not art_p.is_absolute():
            art_p = root / art_p
        if art_p.resolve() != expected_artifacts:
            conflicts.append(
                f"BATTLELAB_ARTIFACTS_DIR='{os.environ['BATTLELAB_ARTIFACTS_DIR']}' differs from canonical season artifacts directory ({expected_artifacts}). "
                "Unset BATTLELAB_ARTIFACTS_DIR to prevent cross-session identity drift."
            )

    if "BATTLELAB_DATA_DIR" in os.environ:
        data_p = Path(os.environ["BATTLELAB_DATA_DIR"])
        if not data_p.is_absolute():
            data_p = root / data_p
        if data_p.resolve() != workspace_dir.resolve():
            conflicts.append(
                f"BATTLELAB_DATA_DIR='{os.environ['BATTLELAB_DATA_DIR']}' points to a different directory than ({workspace_dir})"
            )

    if "BATTLELAB_CHAMPION_MANIFEST" in os.environ:
        man_p = Path(os.environ["BATTLELAB_CHAMPION_MANIFEST"])
        if not man_p.is_absolute():
            man_p = root / man_p
        if man_p.resolve() != manifest_path.resolve():
            conflicts.append(
                f"BATTLELAB_CHAMPION_MANIFEST='{os.environ['BATTLELAB_CHAMPION_MANIFEST']}' points to a different manifest than ({manifest_path})"
            )

    if conflicts:
        raise ValueError(
            "Contradictory or unsupported environment overrides detected in active session. "
            "Remediate the following before initializing:\n  " + "\n  ".join(conflicts)
        )

    target_env = env_file or get_default_env_file()
    if write_dotenv:
        update_dotenv_file(
            env_file=target_env,
            updates={
                "BATTLELAB_DATA_DIR": rel_dir,
                "BATTLELAB_CHAMPION_MANIFEST": rel_manifest,
            },
            comment_header=f"# Battlecode Competition Workspace — {season}",
        )

    # Set in current process
    os.environ["BATTLELAB_DATA_DIR"] = rel_dir
    os.environ["BATTLELAB_CHAMPION_MANIFEST"] = rel_manifest

    # Remove redundant overrides matching canonical so resolution is uniform across sessions
    if (
        "BATTLELAB_DATABASE_PATH" in os.environ
        and Path(os.environ["BATTLELAB_DATABASE_PATH"]).resolve() == expected_db
    ):
        os.environ.pop("BATTLELAB_DATABASE_PATH", None)
    if (
        "BATTLELAB_ARTIFACTS_DIR" in os.environ
        and Path(os.environ["BATTLELAB_ARTIFACTS_DIR"]).resolve() == expected_artifacts
    ):
        os.environ.pop("BATTLELAB_ARTIFACTS_DIR", None)

    # Report effective paths
    eff_data_dir = get_data_dir().resolve()
    eff_manifest = get_champion_manifest_path().resolve()
    eff_db = get_database_path().resolve()
    eff_artifacts = get_artifacts_dir().resolve()

    assert eff_data_dir == workspace_dir.resolve()
    assert eff_manifest == manifest_path.resolve()
    assert eff_db == expected_db
    assert eff_artifacts == expected_artifacts

    return {
        "workspace_dir": str(workspace_dir),
        "champion_manifest": str(manifest_path),
        "database_path": str(expected_db),
        "artifacts_dir": str(expected_artifacts),
        "rel_data_dir": rel_dir,
        "rel_champion_manifest": rel_manifest,
        "effective_data_dir": str(eff_data_dir),
        "effective_champion_manifest": str(eff_manifest),
        "effective_database": str(eff_db),
        "effective_artifacts": str(eff_artifacts),
    }


def check_workspace_status(
    season: str = "autumn2026", env_file: Path | None = None
) -> dict[str, Any]:
    """Check whether active environment matches expected season workspace and is strictly co-scoped."""
    season = validate_season_slug(season)
    root = get_project_root()
    expected_workspace_dir, expected_manifest = get_workspace_paths(season)
    expected_db = (expected_workspace_dir / "battlelab.db").resolve()
    expected_artifacts = (expected_workspace_dir / "artifacts").resolve()

    legacy_manifest = (root / "bots" / "champion" / "champion_manifest.json").resolve()
    legacy_data_dir = (root / "data").resolve()
    legacy_db = (legacy_data_dir / "battlelab.db").resolve()

    curr_data_dir = get_data_dir().resolve()
    curr_manifest = get_champion_manifest_path().resolve()
    curr_db = get_database_path().resolve()
    curr_artifacts = get_artifacts_dir().resolve()

    issues: list[str] = []

    # Check for legacy default usage
    is_legacy = curr_manifest == legacy_manifest and curr_data_dir == legacy_data_dir
    if curr_manifest == legacy_manifest:
        issues.append(f"Champion manifest points to legacy repository manifest: {legacy_manifest}")
    if curr_data_dir == legacy_data_dir:
        issues.append(
            f"Data directory points to legacy repository default data dir: {legacy_data_dir}"
        )

    # Check match with expected season
    if curr_data_dir != expected_workspace_dir.resolve():
        issues.append(
            f"Active data directory ({curr_data_dir}) does not match expected season workspace ({expected_workspace_dir.resolve()})"
        )
    if curr_manifest != expected_manifest.resolve():
        issues.append(
            f"Active champion manifest ({curr_manifest}) does not match expected season manifest ({expected_manifest.resolve()})"
        )

    # Check database co-scoping & canonical identity
    if curr_db == legacy_db and curr_data_dir != legacy_data_dir:
        issues.append(
            f"Database ({curr_db}) is split: pointing to legacy repository database while data dir is isolated."
        )
    elif curr_db != expected_db:
        issues.append(
            f"Database ({curr_db}) differs from canonical season database ({expected_db})."
        )

    # Check artifacts co-scoping & canonical identity
    if curr_artifacts != expected_artifacts:
        issues.append(
            f"Artifacts directory ({curr_artifacts}) differs from canonical season artifacts directory ({expected_artifacts})."
        )

    # Check disagreement with .env if present — scoped strictly to WORKSPACE_ENV_KEYS
    target_env = env_file or get_default_env_file()
    if target_env.is_file():
        try:
            for line in target_env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    # ONLY evaluate workspace-relevant keys; ignore unrelated variables (e.g. DATABASE_URL, SECRET_KEY)
                    if k in WORKSPACE_ENV_KEYS and k in os.environ:
                        active_v = os.environ[k]
                        p_active = Path(active_v)
                        if not p_active.is_absolute():
                            p_active = root / p_active
                        p_env = Path(v)
                        if not p_env.is_absolute():
                            p_env = root / p_env
                        if p_active.resolve() != p_env.resolve():
                            issues.append(
                                f"Environment variable {k}='{active_v}' contradicts .env setting '{v}'"
                            )
        except Exception:
            pass

    is_isolated = len(issues) == 0

    return {
        "season": season,
        "active_data_dir": str(curr_data_dir),
        "active_champion_manifest": str(curr_manifest),
        "active_database": str(curr_db),
        "active_artifacts": str(curr_artifacts),
        "expected_workspace_dir": str(expected_workspace_dir),
        "expected_champion_manifest": str(expected_manifest),
        "expected_database": str(expected_db),
        "expected_artifacts": str(expected_artifacts),
        "is_isolated": is_isolated,
        "is_legacy": is_legacy,
        "issues": issues,
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

    try:
        season = validate_season_slug(args.season)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.status:
        st = check_workspace_status(season)
        print("BATTLELAB COMPETITION WORKSPACE STATUS:")
        if st["is_isolated"]:
            print(f"  Isolated Workspace:      YES (season: {season})")
        elif st["is_legacy"]:
            print("  Isolated Workspace:      NO (pointing to legacy repository defaults)")
        else:
            print("  Isolated Workspace:      NO (mis-scoped / split configuration)")

        print(f"  Data Directory:          {st['active_data_dir']}")
        print(f"  Champion Manifest:       {st['active_champion_manifest']}")
        print(f"  Database Path:           {st['active_database']}")
        print(f"  Artifacts Directory:     {st['active_artifacts']}")

        if st["issues"]:
            print("\nConfiguration Issues:")
            for issue in st["issues"]:
                print(f"  - {issue}")
            print(
                f"\nTo resolve: activate season workspace with 'python scripts/competition_workspace.py --season {season}'"
            )
            return 1

        return 0

    try:
        info = init_workspace(season=season, write_dotenv=not args.no_dotenv)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print("=" * 65)
    print(f"BATTLELAB COMPETITION WORKSPACE INITIALIZED: {season}")
    print("=" * 65)
    print(f"  Data Directory:      {info['effective_data_dir']}")
    print(f"  Champion Manifest:   {info['effective_champion_manifest']}")
    print(f"  Database Path:       {info['effective_database']}")
    print(f"  Artifacts Directory: {info['effective_artifacts']}")
    if not args.no_dotenv:
        print(f"  Persisted to:        {get_default_env_file()} (persists across shells and tools)")
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
