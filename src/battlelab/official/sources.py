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
    Publishes atomically through a temporary sibling directory and refuses to overwrite
    an existing bundle with conflicting contents or metadata.
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
                if any(
                    p in ("..", "", ".") or p.startswith("/") or p.startswith("\\")
                    for p in rel_parts
                ):
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
    # Strictly excludes machine-specific local paths and timestamps
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
    target_bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = target_bundles_dir / bundle_hash

    total_size = sum(f.size_bytes for f in file_records)

    # Sanitize source_path: do not store machine-specific absolute paths
    if raw_path.is_absolute():
        sanitized_source_path = raw_path.name
    else:
        sanitized_source_path = raw_path.as_posix()
        if (
            Path(sanitized_source_path).is_absolute()
            or sanitized_source_path.startswith("/")
            or ":" in sanitized_source_path
        ):
            sanitized_source_path = raw_path.name

    manifest = SourceBundleManifest(
        schema_version="1.0.0",
        bundle_hash=bundle_hash,
        source_path=sanitized_source_path,
        created_at=datetime.now(timezone.utc).isoformat(),
        file_count=len(file_records),
        total_size_bytes=total_size,
        files=file_records,
    )

    # Overwrite protection: never overwrite an existing bundle with different metadata or contents
    if bundle_dir.exists():
        try:
            existing = load_source_bundle_manifest(bundle_hash, bundles_dir=target_bundles_dir)
            if [f.to_dict() for f in existing.files] != [f.to_dict() for f in file_records]:
                raise FileExistsError(
                    f"Bundle {bundle_hash} already exists with different file entries"
                )
            existing_files_dir = bundle_dir / "files"
            if copy_files and not existing_files_dir.exists():
                import uuid

                temp_files = target_bundles_dir / f".tmp_files_{bundle_hash}_{uuid.uuid4().hex}"
                try:
                    temp_files.mkdir(parents=True, exist_ok=True)
                    for src_abs, rel in source_files_to_copy:
                        dest_file = temp_files / Path(rel)
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_abs, dest_file)
                    os.replace(temp_files, existing_files_dir)
                except Exception:
                    shutil.rmtree(temp_files, ignore_errors=True)
                    raise
            return existing
        except (FileExistsError, ValueError):
            raise
        except Exception as e:
            raise FileExistsError(
                f"Bundle {bundle_hash} already exists and cannot be overwritten: {e}"
            ) from e

    # Atomic publication through a temporary sibling directory
    import uuid

    temp_dir = target_bundles_dir / f".tmp_{bundle_hash}_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        if copy_files:
            files_dest_dir = temp_dir / "files"
            files_dest_dir.mkdir(parents=True, exist_ok=True)
            for src_abs, rel in source_files_to_copy:
                dest_file = files_dest_dir / Path(rel)
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_abs, dest_file)

        manifest_path = temp_dir / "source_manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as fp:
            json.dump(manifest.to_dict(), fp, indent=2, sort_keys=True)

        # Verify staged bundle completely before publishing
        _verify_staged_bundle(temp_dir, manifest, bundle_hash, copy_files)

        try:
            os.replace(temp_dir, bundle_dir)
        except OSError:
            # Concurrent publication safety
            if bundle_dir.exists():
                existing = load_source_bundle_manifest(bundle_hash, bundles_dir=target_bundles_dir)
                if [f.to_dict() for f in existing.files] != [f.to_dict() for f in file_records]:
                    raise FileExistsError(
                        f"Bundle {bundle_hash} already exists with conflicting entries"
                    )
                return existing
            raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return manifest


def _verify_staged_bundle(
    staged_dir: Path,
    manifest: SourceBundleManifest,
    expected_hash: str,
    expect_copied_files: bool,
) -> None:
    """Verify complete staged bundle before atomic publication."""
    import stat

    st = os.lstat(staged_dir)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"Staged bundle root is invalid: {staged_dir}")

    man_path = staged_dir / "source_manifest.json"
    if not man_path.is_file():
        raise ValueError(f"Missing manifest in staged bundle: {man_path}")
    man_st = os.lstat(man_path)
    if stat.S_ISLNK(man_st.st_mode) or not stat.S_ISREG(man_st.st_mode):
        raise ValueError(f"Staged manifest is not a regular file: {man_path}")

    with open(man_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    loaded_manifest = SourceBundleManifest.from_dict(data)

    canonical_items = [
        {"relpath": f.relpath, "sha256": f.sha256, "size_bytes": f.size_bytes}
        for f in sorted(loaded_manifest.files, key=lambda x: x.relpath)
    ]
    recomputed_hash = hashlib.sha256(
        json.dumps(canonical_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if recomputed_hash != expected_hash:
        raise ValueError(f"Staged bundle hash mismatch: {recomputed_hash} != {expected_hash}")

    if expect_copied_files:
        files_dir = staged_dir / "files"
        if not files_dir.is_dir():
            raise ValueError(f"Missing files directory in staged bundle: {files_dir}")
        for entry in manifest.files:
            target_f = files_dir / Path(entry.relpath)
            if not target_f.is_file():
                raise ValueError(f"Staged file missing: {entry.relpath}")
            f_st = os.lstat(target_f)
            if stat.S_ISLNK(f_st.st_mode) or not stat.S_ISREG(f_st.st_mode):
                raise ValueError(f"Staged file cannot be symlink: {entry.relpath}")
            if f_st.st_size != entry.size_bytes:
                raise ValueError(f"Staged file size mismatch for {entry.relpath}")
            if hash_file(target_f) != entry.sha256:
                raise ValueError(f"Staged file hash mismatch for {entry.relpath}")


def load_source_bundle_manifest(
    bundle_hash: str, bundles_dir: Path | None = None
) -> SourceBundleManifest:
    """Load and verify an existing source bundle manifest."""
    # 1. Validate bundle_hash format before ANY filesystem access
    if (
        not isinstance(bundle_hash, str)
        or len(bundle_hash) != 64
        or not all(c in "0123456789abcdef" for c in bundle_hash)
    ):
        raise ValueError(f"Invalid source_bundle_hash format: {bundle_hash!r}")

    target_bundles_dir = (
        bundles_dir if bundles_dir is not None else get_official_source_bundles_dir()
    )
    _check_not_symlink(target_bundles_dir)

    bundle_dir = target_bundles_dir / bundle_hash
    if not bundle_dir.exists():
        raise FileNotFoundError(f"Source bundle directory not found for hash: {bundle_hash}")

    import stat

    # Reject bundle root symlinks or non-directories
    st = os.lstat(bundle_dir)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"Bundle root must be a regular directory, not a symlink: {bundle_dir}")

    # Ensure resolved bundle root remains a direct child of target_bundles_dir
    try:
        resolved_bundle = bundle_dir.resolve()
        resolved_parent = target_bundles_dir.resolve()
        if resolved_bundle.parent != resolved_parent:
            raise ValueError(
                f"Bundle directory traversal or ancestor symlink detected: {bundle_dir}"
            )
    except Exception as e:
        raise ValueError(f"Ancestor path verification failed: {e}") from e

    manifest_path = bundle_dir / "source_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Source bundle manifest not found for hash: {bundle_hash}")

    _check_not_symlink(manifest_path)
    man_st = os.lstat(manifest_path)
    if not stat.S_ISREG(man_st.st_mode) or stat.S_ISLNK(man_st.st_mode):
        raise ValueError(f"Manifest path must be a regular file: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. Parse and validate schema, types, relpaths, hashes, sizes
    manifest = SourceBundleManifest.from_dict(data)

    # 2. Recompute bundle_hash from sorted canonical items
    canonical_items = [
        {"relpath": f.relpath, "sha256": f.sha256, "size_bytes": f.size_bytes}
        for f in sorted(manifest.files, key=lambda x: x.relpath)
    ]
    recomputed_hash = hashlib.sha256(
        json.dumps(canonical_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    if manifest.bundle_hash != recomputed_hash:
        raise ValueError(
            f"Manifest bundle_hash tampered: expected {recomputed_hash}, found {manifest.bundle_hash}"
        )
    if bundle_hash != recomputed_hash:
        raise ValueError(
            f"Directory bundle hash mismatch: expected {bundle_hash}, found {recomputed_hash}"
        )

    # 3. If copied files exist, verify exact set, regular-file type, size, and SHA-256
    files_dir = bundle_dir / "files"
    if files_dir.exists():
        _check_not_symlink(files_dir)
        files_st = os.lstat(files_dir)
        if not stat.S_ISDIR(files_st.st_mode) or stat.S_ISLNK(files_st.st_mode):
            raise ValueError(f"files path in bundle is not a directory: {files_dir}")

        found_relpaths: set[str] = set()
        for root, dirs, filenames in os.walk(files_dir, followlinks=False):
            for d in dirs:
                d_path = Path(root) / d
                _check_not_symlink(d_path)
                d_st = os.lstat(d_path)
                if not stat.S_ISDIR(d_st.st_mode) or stat.S_ISLNK(d_st.st_mode):
                    raise ValueError(
                        f"Intermediate directory cannot be symlink or special: {d_path}"
                    )
            for fn in filenames:
                fn_path = Path(root) / fn
                _check_not_symlink(fn_path)
                fn_st = os.lstat(fn_path)
                if not stat.S_ISREG(fn_st.st_mode) or stat.S_ISLNK(fn_st.st_mode):
                    raise ValueError(f"Non-regular file found in copied source bundle: {fn_path}")
                rel = fn_path.relative_to(files_dir).as_posix()
                found_relpaths.add(rel)

        expected_relpaths = {f.relpath for f in manifest.files}
        extra = found_relpaths - expected_relpaths
        if extra:
            raise ValueError(
                f"Extraneous/added files found in copied source bundle: {sorted(list(extra))}"
            )
        missing = expected_relpaths - found_relpaths
        if missing:
            raise ValueError(f"Missing copied files in source bundle: {sorted(list(missing))}")

        for entry in manifest.files:
            f_path = files_dir / Path(entry.relpath)
            _check_not_symlink(f_path)
            f_st = os.lstat(f_path)
            if not stat.S_ISREG(f_st.st_mode) or stat.S_ISLNK(f_st.st_mode):
                raise ValueError(f"Copied file is not regular file: {entry.relpath}")
            if f_st.st_size != entry.size_bytes:
                raise ValueError(
                    f"Copied file size tampered ({entry.relpath}): expected {entry.size_bytes}, got {f_st.st_size}"
                )
            actual_sha = hash_file(f_path)
            if actual_sha != entry.sha256:
                raise ValueError(
                    f"Copied file hash tampered ({entry.relpath}): expected {entry.sha256}, got {actual_sha}"
                )

    return manifest
