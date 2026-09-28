"""Immutable Bot Artifact builder and management."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from battlelab.core.hashing import hash_dict, hash_directory, hash_file
from battlelab.core.identifiers import generate_artifact_id
from battlelab.core.models import BotArtifact
from battlelab.storage.paths import get_artifacts_dir


def check_git_status(path: Path) -> tuple[str | None, bool]:
    """Check git commit hash and whether the worktree is dirty."""
    try:
        commit_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_proc.returncode != 0:
            return None, False
        commit_hash = commit_proc.stdout.strip()

        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        dirty = bool(status_proc.stdout.strip())
        return commit_hash, dirty
    except Exception:
        return None, False


def create_bot_artifact(
    source_path: Path | str,
    display_name: str | None = None,
    language: str = "python",
    tags: list[str] | None = None,
    experiment_id: str | None = None,
    hypothesis: str | None = None,
    parent_artifact_id: str | None = None,
) -> BotArtifact:
    """Build and freeze a bot source into an immutable artifact snapshot."""
    src = Path(source_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Bot source not found: {src}")

    name = display_name or src.stem

    # Calculate content hash
    if src.is_dir():
        src_hash = hash_directory(src)
    else:
        src_hash = hash_file(src)

    # Build config hash accounts for tags, name, and language
    build_config_dict = {
        "tags": sorted(tags or []),
        "display_name": name,
        "language": language,
    }
    build_config_hash = hash_dict(build_config_dict)

    git_commit, dirty = check_git_status(src)
    artifact_id = generate_artifact_id(src_hash, build_config_hash)

    # Immutable snapshot directory
    target_dir = get_artifacts_dir() / artifact_id
    target_dir.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        shutil.copytree(src, target_dir, dirs_exist_ok=True)
    else:
        shutil.copy2(src, target_dir / src.name)

    created_at = datetime.now(timezone.utc).isoformat()

    artifact = BotArtifact(
        artifact_id=artifact_id,
        display_name=name,
        source_location=str(target_dir),
        language=language,
        git_commit=git_commit,
        dirty_worktree=dirty,
        source_hash=src_hash,
        build_config_hash=build_config_hash,
        parent_artifact_id=parent_artifact_id,
        created_at=created_at,
        experiment_id=experiment_id,
        hypothesis=hypothesis,
        tags=tags or [],
        build_result={"status": "READY"},
    )
    return artifact
