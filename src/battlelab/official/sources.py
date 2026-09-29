"""Official source document ingestion and integrity verification."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from battlelab.core.hashing import hash_file
from battlelab.official.models import SourceBundleManifest, SourceFileEntry
from battlelab.storage.paths import get_official_source_bundles_dir


def _check_not_symlink(path: Path) -> None:
    """Reject symlinks using lstat."""
    try:
        st = os.lstat(path)
        import stat

        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"Symlinks are rejected for security and provenance: {path}")
    except OSError as e:
        raise ValueError(f"Cannot access path: {path} ({e})") from e


def ingest_sources(
    source_path: Path,
    copy_files: bool = False,
    bundles_dir: Path | None = None,
) -> SourceBundleManifest:
    """Ingest authoritative documents from a file or directory into a verified source bundle.

    Rejects missing paths, empty directories, symlinks, and path traversal.
    """
    raw_path = Path(source_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {raw_path}")

    # Check root for symlink before resolving
    _check_not_symlink(raw_path)

    # Validate readability and type
    if not raw_path.is_file() and not raw_path.is_dir():
        raise ValueError(
            f"Unsupported source path type (must be regular file or directory): {raw_path}"
        )

    resolved_root = raw_path.resolve()
    file_records: list[SourceFileEntry] = []

    if raw_path.is_file():
        file_hash = hash_file(resolved_root)
        size = resolved_root.stat().st_size
        mime, _ = mimetypes.guess_type(str(raw_path.name))
        file_records.append(
            SourceFileEntry(
                relpath=raw_path.name,
                sha256=file_hash,
                size_bytes=size,
                media_type=mime or Path(raw_path.name).suffix or "application/octet-stream",
            )
        )
        source_files_to_copy = [(resolved_root, raw_path.name)]
    else:
        # Directory recursion
        source_files_to_copy = []
        for root, dirs, files in os.walk(raw_path, followlinks=False):
            # Check dirs for symlinks
            for d in dirs:
                dir_path = Path(root) / d
                _check_not_symlink(dir_path)

            for f in files:
                file_path = Path(root) / f
                _check_not_symlink(file_path)

                # Compute relpath from raw_path
                rel_parts = file_path.relative_to(raw_path).parts
                if any(p == ".." or p.startswith("/") or p.startswith("\\") for p in rel_parts):
                    raise ValueError(f"Path traversal detected in source directory: {file_path}")

                posix_rel = "/".join(rel_parts)
                resolved_file = file_path.resolve()
                if not resolved_file.is_file():
                    continue

                file_hash = hash_file(resolved_file)
                size = resolved_file.stat().st_size
                mime, _ = mimetypes.guess_type(f)
                file_records.append(
                    SourceFileEntry(
                        relpath=posix_rel,
                        sha256=file_hash,
                        size_bytes=size,
                        media_type=mime or Path(f).suffix or "application/octet-stream",
                    )
                )
                source_files_to_copy.append((resolved_file, posix_rel))

    if not file_records:
        raise ValueError(f"Source path contains no regular files: {source_path}")

    # Sort file records strictly by relpath for deterministic bundle hash
    file_records.sort(key=lambda x: x.relpath)

    # Compute bundle hash purely from sorted relpath, sha256, and size_bytes
    # Strictly excludes local paths and timestamps
    canonical_items = [
        {"relpath": r.relpath, "sha256": r.sha256, "size_bytes": r.size_bytes} for r in file_records
    ]
    bundle_hash = hashlib.sha256(
        json.dumps(canonical_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # Determine storage destination
    target_bundles_dir = (
        bundles_dir if bundles_dir is not None else get_official_source_bundles_dir()
    )
    bundle_dir = target_bundles_dir / bundle_hash
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # If copying files is requested
    if copy_files:
        files_dest_dir = bundle_dir / "files"
        files_dest_dir.mkdir(parents=True, exist_ok=True)
        for src_abs, rel in source_files_to_copy:
            dest_file = files_dest_dir / Path(rel)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_abs, dest_file)

    total_size = sum(f.size_bytes for f in file_records)
    manifest = SourceBundleManifest(
        schema_version="1.0.0",
        bundle_hash=bundle_hash,
        source_path=str(source_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        file_count=len(file_records),
        total_size_bytes=total_size,
        files=file_records,
    )

    manifest_path = bundle_dir / "source_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fp:
        json.dump(manifest.to_dict(), fp, indent=2, sort_keys=True)

    return manifest


def load_source_bundle_manifest(
    bundle_hash: str, bundles_dir: Path | None = None
) -> SourceBundleManifest:
    """Load and verify an existing source bundle manifest."""
    target_bundles_dir = (
        bundles_dir if bundles_dir is not None else get_official_source_bundles_dir()
    )
    manifest_path = target_bundles_dir / bundle_hash / "source_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Source bundle manifest not found for hash: {bundle_hash}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    manifest = SourceBundleManifest.from_dict(data)
    if manifest.bundle_hash != bundle_hash:
        raise ValueError(
            f"Bundle hash mismatch in manifest: expected {bundle_hash}, found {manifest.bundle_hash}"
        )
    return manifest
