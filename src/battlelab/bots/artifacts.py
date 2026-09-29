import json
import os
import shutil
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.core.hashing import hash_bytes, hash_dict, hash_directory, hash_file
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


def recompute_manifest_hash(files: dict[str, dict[str, Any]], entrypoint: str) -> str:
    """Canonically recompute SHA-256 hash across sorted normalized manifest entries."""
    lines = [f"{k}:{files[k]['sha256']}:{files[k]['size_bytes']}" for k in sorted(files.keys())]
    lines.append(f"entrypoint:{entrypoint}")
    return hash_bytes("\n".join(lines).encode("utf-8"))


def compute_artifact_manifest(
    src: Path, entrypoint: str | None = None
) -> tuple[dict[str, dict[str, Any]], str, str]:
    """Build a complete manifest of relative paths, content hashes, sizes, and entrypoint."""
    files: dict[str, dict[str, Any]] = {}

    if src.is_file():
        entrypoint_rel = src.name
        sha = hash_file(src)
        size = src.stat().st_size
        files[src.name] = {"sha256": sha, "size_bytes": size}
    elif src.is_dir():
        # Determine and validate explicit entrypoint if provided
        if entrypoint:
            ep_str = entrypoint.replace("\\", "/").strip()
            if (
                Path(ep_str).is_absolute()
                or ep_str.startswith("../")
                or "/../" in ep_str
                or ep_str == ".."
            ):
                raise ValueError(f"Entrypoint must be a relative path inside source: {entrypoint}")
            ep_path = src / ep_str
            try:
                ep_resolved = ep_path.resolve()
                src_resolved = src.resolve()
                ep_resolved.relative_to(src_resolved)
            except ValueError:
                raise ValueError(f"Entrypoint escapes source directory: {entrypoint}")
            if not ep_path.is_file():
                raise FileNotFoundError(f"Specified entrypoint not found: {entrypoint}")
            entrypoint_rel = Path(ep_str).as_posix()
        else:
            found = None
            for cand in ["main.py", "bot.py", "run.py"]:
                if (src / cand).is_file():
                    found = cand
                    break
            if not found:
                raise ValueError(
                    f"No standard entrypoint (main.py, bot.py, run.py) found in {src}. "
                    "Explicit entrypoint required. Specify with --entrypoint relative/path/to/file.py"
                )
            entrypoint_rel = found

        for root, dirs, filenames in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
            for fname in filenames:
                if fname.endswith(".pyc") or fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                if fpath.is_symlink():
                    raise ValueError(f"Symlinks are not permitted in bot source: {fpath}")
                rel = fpath.relative_to(src).as_posix()
                files[rel] = {
                    "sha256": hash_file(fpath),
                    "size_bytes": fpath.stat().st_size,
                }

        if entrypoint_rel not in files:
            raise FileNotFoundError(f"Entrypoint '{entrypoint_rel}' is not in scanned manifest.")
    else:
        raise FileNotFoundError(f"Source path is neither file nor directory: {src}")

    manifest_hash = recompute_manifest_hash(files, entrypoint_rel)
    return files, entrypoint_rel, manifest_hash


def verify_artifact_integrity(bot: BotArtifact | Path | str) -> tuple[bool, str]:
    """Verify complete snapshot integrity against stored manifest and canonical hash.

    Rejects modified, added, deleted, renamed, symlinked, or path-traversal files,
    as well as forged manifest.json metadata.
    """
    if isinstance(bot, BotArtifact):
        target_dir = Path(bot.source_location)
        expected_manifest_hash = bot.manifest_hash
        expected_entrypoint = bot.entrypoint_relpath
    else:
        target_dir = Path(bot)
        expected_manifest_hash = None
        expected_entrypoint = None

    if not target_dir.exists():
        return False, f"Artifact snapshot directory does not exist: {target_dir}"

    manifest_file = target_dir / "manifest.json"
    if not manifest_file.exists():
        return False, f"Missing manifest.json in artifact snapshot: {target_dir}"

    try:
        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception as e:
        return False, f"Failed reading manifest.json: {e}"

    # 1. Validate manifest schema and types
    if not isinstance(manifest_data, dict):
        return False, "Invalid manifest: root must be a JSON object"

    recorded_files = manifest_data.get("files")
    recorded_entrypoint = manifest_data.get("entrypoint_relpath")
    recorded_hash = manifest_data.get("manifest_hash")

    if not isinstance(recorded_files, dict):
        return False, "Invalid manifest schema: 'files' must be an object"
    if not isinstance(recorded_entrypoint, str) or not recorded_entrypoint:
        return False, "Invalid manifest schema: 'entrypoint_relpath' must be a non-empty string"
    if not isinstance(recorded_hash, str) or len(recorded_hash) != 64:
        return False, "Invalid manifest schema: 'manifest_hash' must be a 64-char SHA256 string"

    # 2. Path normalization, traversal, and symlink checks on recorded paths
    seen_normalized: set[str] = set()
    for rel_path, meta in recorded_files.items():
        if not isinstance(meta, dict) or "sha256" not in meta or "size_bytes" not in meta:
            return False, f"Invalid file metadata schema for '{rel_path}'"

        if Path(rel_path).is_absolute():
            return False, f"Absolute path rejected in manifest: {rel_path}"

        norm = Path(rel_path).as_posix()
        if norm != rel_path or norm.startswith("../") or "/../" in norm or norm == "..":
            return False, f"Path traversal rejected in manifest: {rel_path}"

        if norm in seen_normalized:
            return False, f"Duplicate normalized path in manifest: {norm}"
        seen_normalized.add(norm)

        fpath = target_dir / norm
        if fpath.is_symlink():
            return False, f"Symlink rejected in artifact snapshot: {norm}"

    # 3. Entrypoint checks
    if Path(recorded_entrypoint).is_absolute():
        return False, f"Absolute entrypoint rejected: {recorded_entrypoint}"
    norm_ep = Path(recorded_entrypoint).as_posix()
    if (
        norm_ep != recorded_entrypoint
        or norm_ep.startswith("../")
        or "/../" in norm_ep
        or norm_ep == ".."
    ):
        return False, f"Entrypoint traversal rejected: {recorded_entrypoint}"
    if recorded_entrypoint not in recorded_files:
        return False, f"Entrypoint '{recorded_entrypoint}' is not registered in manifest files"

    entry_path = target_dir / recorded_entrypoint
    if entry_path.is_symlink() or not entry_path.is_file():
        return False, f"Recorded entrypoint is not a regular file: {recorded_entrypoint}"

    # 4. Canonically recompute manifest hash from actual manifest content
    recomputed_hash = recompute_manifest_hash(recorded_files, recorded_entrypoint)
    if recomputed_hash != recorded_hash:
        return (
            False,
            f"Manifest hash forgery detected: recorded {recorded_hash[:12]} does not match recomputed {recomputed_hash[:12]}",
        )

    # Compare against expected database hash if available
    if expected_manifest_hash and recomputed_hash != expected_manifest_hash:
        return (
            False,
            f"Manifest hash mismatch against database: expected {expected_manifest_hash[:12]}, got {recomputed_hash[:12]}",
        )

    if expected_entrypoint and recorded_entrypoint != expected_entrypoint:
        return (
            False,
            f"Entrypoint mismatch against database: expected '{expected_entrypoint}', recorded '{recorded_entrypoint}'",
        )

    # 5. Verify all recorded files exist with exact size and SHA256
    for rel_path, meta in recorded_files.items():
        fpath = target_dir / rel_path
        if not fpath.exists() or not fpath.is_file() or fpath.is_symlink():
            return False, f"Missing or invalid regular file in artifact snapshot: {rel_path}"

        expected_size = meta.get("size_bytes")
        if fpath.stat().st_size != expected_size:
            return (
                False,
                f"Size mismatch for '{rel_path}': expected {expected_size}, got {fpath.stat().st_size}",
            )

        expected_sha = meta.get("sha256")
        actual_sha = hash_file(fpath)
        if actual_sha != expected_sha:
            return (
                False,
                f"Hash mismatch for '{rel_path}': expected {expected_sha[:12]}, got {actual_sha[:12]}",
            )

    # 6. Verify NO extra / extraneous files or symlinks exist in snapshot directory
    for root, dirs, filenames in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
        for fname in filenames:
            if fname == "manifest.json" or fname.startswith("."):
                continue
            fpath = Path(root) / fname
            if fpath.is_symlink():
                return False, f"Symlink detected in artifact snapshot: {fpath}"
            rel = fpath.relative_to(target_dir).as_posix()
            if rel not in recorded_files:
                return False, f"Extraneous file detected in artifact snapshot: {rel}"

    return True, "Integrity verified"


def create_bot_artifact(
    source_path: Path | str,
    display_name: str | None = None,
    language: str = "python",
    tags: list[str] | None = None,
    experiment_id: str | None = None,
    hypothesis: str | None = None,
    parent_artifact_id: str | None = None,
    entrypoint: str | None = None,
) -> BotArtifact:
    """Build and freeze a bot source into an immutable artifact snapshot with full manifest."""
    src = Path(source_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Bot source not found: {src}")

    name = display_name or src.stem

    # Compute complete cryptographic manifest
    file_manifest, entrypoint_rel, manifest_hash = compute_artifact_manifest(src, entrypoint)

    # Calculate overall content hash for backwards compatibility and artifact ID
    if src.is_dir():
        src_hash = hash_directory(src)
    else:
        src_hash = hash_file(src)

    build_config_dict = {
        "tags": sorted(tags or []),
        "display_name": name,
        "language": language,
    }
    build_config_hash = hash_dict(build_config_dict)

    git_commit, dirty = check_git_status(src)
    artifact_id = generate_artifact_id(src_hash, build_config_hash)

    target_dir = get_artifacts_dir() / artifact_id

    # If destination artifact already exists, check whether it satisfies the manifest completely
    should_publish = True
    if target_dir.exists():
        is_valid, _ = verify_artifact_integrity(target_dir)
        if is_valid:
            manifest_file = target_dir / "manifest.json"
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    curr_m = json.load(f)
                if curr_m.get("manifest_hash") == manifest_hash:
                    should_publish = False
            except Exception:
                pass

    if should_publish:
        staging_dir = get_artifacts_dir() / f"staging_{uuid.uuid4().hex}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            if src.is_file():
                shutil.copy2(src, staging_dir / src.name)
            else:
                for rel_path in file_manifest:
                    src_file = src / rel_path
                    dst_file = staging_dir / rel_path
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)

            # Write explicit manifest.json
            manifest_payload = {
                "entrypoint_relpath": entrypoint_rel,
                "manifest_hash": manifest_hash,
                "files": file_manifest,
            }
            manifest_json_path = staging_dir / "manifest.json"
            with open(manifest_json_path, "w", encoding="utf-8") as mf:
                json.dump(manifest_payload, mf, indent=2)

            # Best-effort read-only permissions
            for root, _, filenames in os.walk(staging_dir):
                for fname in filenames:
                    try:
                        p = Path(root) / fname
                        os.chmod(p, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
                    except Exception:
                        pass

            # Atomic publication
            if target_dir.exists():
                # Make writable to allow clean removal
                for root, _, filenames in os.walk(target_dir):
                    for fname in filenames:
                        try:
                            os.chmod(Path(root) / fname, stat.S_IWRITE | stat.S_IREAD)
                        except Exception:
                            pass
                shutil.rmtree(target_dir, ignore_errors=True)

            try:
                os.replace(staging_dir, target_dir)
            except OSError:
                shutil.move(str(staging_dir), str(target_dir))

        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

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
        entrypoint_relpath=entrypoint_rel,
        manifest=file_manifest,
        manifest_hash=manifest_hash,
    )
    return artifact
