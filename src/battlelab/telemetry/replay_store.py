"""Content-addressed replay store with integrity verification and retention policies."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from battlelab.core.errors import ReplayCorruptedError, ReplayError
from battlelab.core.hashing import hash_file
from battlelab.storage.paths import get_replays_dir


class ReplayStore:
    """Manages content-addressed storage and integrity verification of replays."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or get_replays_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def store_replay(self, temp_replay_path: Path | str) -> tuple[Path, str]:
        """Verify, hash, and move replay file into content-addressed location."""
        src = Path(temp_replay_path)
        if not src.exists() or src.stat().st_size == 0:
            raise ReplayError(f"Replay file is missing or empty: {src}")

        # Validate JSON integrity
        self.validate_replay_file(src)

        file_hash = hash_file(src)
        dest_filename = f"{file_hash}.jsonl"
        dest_path = self.base_dir / dest_filename

        if not dest_path.exists():
            # Atomic copy
            tmp_dest = dest_path.with_suffix(".tmp")
            shutil.copy2(src, tmp_dest)
            tmp_dest.replace(dest_path)

        return dest_path, file_hash

    def validate_replay_file(self, replay_path: Path) -> dict[str, Any]:
        """Validate structure and JSON parsing of a replay file."""
        if not replay_path.is_file():
            raise ReplayCorruptedError(f"Replay not found: {replay_path}")

        frames_count = 0
        meta: dict[str, Any] = {}
        with open(replay_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean_line = line.strip()
                if not clean_line:
                    continue
                try:
                    obj = json.loads(clean_line)
                    if line_no == 1 and obj.get("type") == "META":
                        meta = obj
                    frames_count += 1
                except Exception as e:
                    raise ReplayCorruptedError(
                        f"Replay syntax error at line {line_no}: {e}"
                    ) from e

        if frames_count == 0:
            raise ReplayCorruptedError("Replay contains zero frames")

        return {"frames_count": frames_count, "meta": meta}

    def verify_stored_replay(self, replay_hash: str) -> bool:
        """Verify that the stored replay matches its content hash and parses cleanly."""
        file_path = self.base_dir / f"{replay_hash}.jsonl"
        if not file_path.exists():
            return False
        if hash_file(file_path) != replay_hash:
            return False
        try:
            self.validate_replay_file(file_path)
            return True
        except ReplayCorruptedError:
            return False
