"""Stable, canonical hashing utilities for artifacts, configs, and replays."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

def hash_bytes(data: bytes) -> str:
    """Compute SHA-256 hash of bytes."""
    return hashlib.sha256(data).hexdigest()

def hash_file(file_path: Path | str) -> str:
    """Compute SHA-256 hash of a file's contents in chunks."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def hash_directory(dir_path: Path | str, excludes: list[str] | None = None) -> str:
    """Compute SHA-256 hash of directory by sorting relative file paths and hashing contents.
    
    Ignores common non-deterministic/cache files (__pycache__, .git, etc.).
    """
    path = Path(dir_path)
    if not path.is_dir():
        raise NotADirectoryError(f"Directory not found: {path}")
    
    default_excludes = {".git", "__pycache__", ".pytest_cache", ".DS_Store", "Thumbs.db"}
    custom_excludes = set(excludes or [])
    all_excludes = default_excludes | custom_excludes
    
    records: list[tuple[str, str]] = []
    
    for item in sorted(path.rglob("*")):
        if item.is_file():
            # Check exclusions along parts
            if any(part in all_excludes or part.endswith((".pyc", ".pyo")) for part in item.parts):
                continue
            rel_path = item.relative_to(path).as_posix()
            f_hash = hash_file(item)
            records.append((rel_path, f_hash))
            
    # Combine relative paths and hashes deterministically
    hasher = hashlib.sha256()
    for rel_path, f_hash in sorted(records, key=lambda x: x[0]):
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(f_hash.encode("utf-8"))
        
    return hasher.hexdigest()

def hash_dict(data: dict[str, Any]) -> str:
    """Compute SHA-256 hash of a dictionary with sorted canonical JSON keys."""
    canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hash_bytes(canonical_json.encode("utf-8"))
