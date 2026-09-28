"""Bot Registry for tracking artifacts, champions, and baselines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from battlelab.bots.artifacts import create_bot_artifact
from battlelab.core.errors import ArtifactNotFoundError
from battlelab.core.models import BotArtifact
from battlelab.storage.database import Database
from battlelab.storage.paths import get_champion_manifest_path


class BotRegistry:
    """Manages bot registration, immutable artifacts, and champion manifest."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def register_bot(
        self,
        source_path: Path | str,
        display_name: str | None = None,
        language: str = "python",
        tags: list[str] | None = None,
        experiment_id: str | None = None,
        hypothesis: str | None = None,
        parent_artifact_id: str | None = None,
    ) -> BotArtifact:
        """Register and snapshot a bot from source path."""
        artifact = create_bot_artifact(
            source_path=source_path,
            display_name=display_name,
            language=language,
            tags=tags,
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            parent_artifact_id=parent_artifact_id,
        )
        self.db.save_artifact(artifact)
        return artifact

    def get_artifact(self, artifact_id: str) -> BotArtifact:
        """Retrieve an immutable artifact by ID."""
        art = self.db.get_artifact(artifact_id)
        if not art:
            raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")
        return art

    def list_artifacts(self) -> list[BotArtifact]:
        """List all registered artifacts."""
        return self.db.list_artifacts()

    def get_champion_artifact(self) -> BotArtifact | None:
        """Get currently active champion artifact from manifest."""
        manifest_path = get_champion_manifest_path()
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            champ_id = data.get("champion_artifact_id")
            if not champ_id:
                return None
            return self.get_artifact(champ_id)
        except Exception:
            return None

    def update_champion_manifest(
        self,
        artifact_id: str,
        experiment_id: str,
        updated_at: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Update champion manifest to point to a new immutable artifact."""
        manifest_path = get_champion_manifest_path()
        payload = {
            "champion_artifact_id": artifact_id,
            "experiment_id": experiment_id,
            "updated_at": updated_at,
            "reason": reason,
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write
        tmp_path = manifest_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(manifest_path)
        return payload
