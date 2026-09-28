"""Single match execution worker."""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.adapters import get_adapter
from battlelab.bots.registry import BotRegistry
from battlelab.core.errors import ReplayCorruptedError
from battlelab.core.models import (
    FailureCategory,
    FailureClassification,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)
from battlelab.storage.database import Database
from battlelab.storage.paths import get_data_dir
from battlelab.telemetry.replay_store import ReplayStore


def execute_match_job(spec_dict: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    """Execute a single match job safely and persist results to SQLite."""
    spec = MatchSpec.from_dict(spec_dict)
    db = Database(db_path)
    registry = BotRegistry(db)
    replay_store = ReplayStore()

    work_dir = get_data_dir() / "runs" / spec.match_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        bot_a = registry.get_artifact(spec.bot_a_id)
        bot_b = registry.get_artifact(spec.bot_b_id)
        adapter = get_adapter(spec.adapter_name)

        # Run match through adapter
        result = adapter.run_local_match(
            spec=spec,
            bot_a=bot_a,
            bot_b=bot_b,
            work_dir=work_dir,
        )

        # Process and store replay if present
        if result.replay_path:
            raw_replay_path = Path(result.replay_path)
            try:
                dest_path, file_hash = replay_store.store_replay(raw_replay_path)
                result.replay_path = str(dest_path)
                result.replay_hash = file_hash
            except ReplayCorruptedError as e:
                result.outcome = MatchOutcome.INFRASTRUCTURE_FAILURE
                result.failure_classification = FailureClassification(
                    category=FailureCategory.REPLAY_CORRUPTION,
                    culprit="engine",
                    evidence=f"Replay validation failed: {e}",
                    is_inference=False,
                )
            except Exception as e:
                result.outcome = MatchOutcome.INFRASTRUCTURE_FAILURE
                result.failure_classification = FailureClassification(
                    category=FailureCategory.STORAGE_FAILURE,
                    culprit="system",
                    evidence=f"Replay storage error: {e}",
                    is_inference=False,
                )

        # Update database with result
        db.update_match_result(result)
        return result.to_dict()

    except Exception as e:
        # Catch unexpected infrastructure exception
        fc = FailureClassification(
            category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
            culprit="system",
            evidence=f"Worker crashed with unhandled error: {e}\n{traceback.format_exc()}",
            is_inference=False,
        )
        fail_res = MatchResult(
            match_id=spec.match_id,
            outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
            failure_classification=fc,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.update_match_result(fail_res)
        return fail_res.to_dict()
