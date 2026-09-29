import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.adapters import get_adapter
from battlelab.bots.artifacts import verify_artifact_integrity
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
from battlelab.storage.paths import get_runs_dir
from battlelab.telemetry.replay_store import ReplayStore


def execute_match_job(
    spec_dict: dict[str, Any],
    db_path: str | None = None,
    worker_id: str | None = None,
    lease_token: str | None = None,
    lease_duration_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute a single match job safely with renewable heartbeat leases and fencing."""
    spec = MatchSpec.from_dict(spec_dict)
    db = Database(db_path)
    registry = BotRegistry(db)
    replay_store = ReplayStore()

    work_dir = get_runs_dir() / spec.match_id
    work_dir.mkdir(parents=True, exist_ok=True)

    heartbeat_stop = threading.Event()
    lost_lease = threading.Event()
    cancel_event = threading.Event()

    def _heartbeat_loop():
        # Heartbeat at ~1/3 of the lease duration (minimum 10ms for fast tests)
        interval = max(0.010, lease_duration_seconds / 3.0)
        try:
            while not heartbeat_stop.wait(interval):
                if worker_id and lease_token:
                    try:
                        renewed = db.renew_lease(
                            match_id=spec.match_id,
                            worker_id=worker_id,
                            lease_token=lease_token,
                            additional_seconds=lease_duration_seconds,
                        )
                        if not renewed:
                            lost_lease.set()
                            cancel_event.set()
                            break
                    except Exception:
                        lost_lease.set()
                        cancel_event.set()
                        break
        except Exception:
            lost_lease.set()
            cancel_event.set()

    heartbeat_thread = None
    if worker_id and lease_token:
        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

    try:
        bot_a = registry.get_artifact(spec.bot_a_id)
        bot_b = registry.get_artifact(spec.bot_b_id)

        # Verify artifact integrity before execution
        ok_a, msg_a = verify_artifact_integrity(bot_a)
        if not ok_a:
            fc = FailureClassification(
                category=FailureCategory.STORAGE_FAILURE,
                culprit="system",
                evidence=f"Bot A integrity check failed: {msg_a}",
            )
            fail_res = MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=fc,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            heartbeat_stop.set()
            committed = db.update_match_result(fail_res, lease_token=lease_token)
            res_dict = fail_res.to_dict()
            res_dict["committed"] = committed
            return res_dict

        ok_b, msg_b = verify_artifact_integrity(bot_b)
        if not ok_b:
            fc = FailureClassification(
                category=FailureCategory.STORAGE_FAILURE,
                culprit="system",
                evidence=f"Bot B integrity check failed: {msg_b}",
            )
            fail_res = MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=fc,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            heartbeat_stop.set()
            committed = db.update_match_result(fail_res, lease_token=lease_token)
            res_dict = fail_res.to_dict()
            res_dict["committed"] = committed
            return res_dict

        adapter = get_adapter(spec.adapter_name)

        # Run match through adapter with active cancellation
        result = adapter.run_local_match(
            spec=spec,
            bot_a=bot_a,
            bot_b=bot_b,
            work_dir=work_dir,
            cancel_event=cancel_event,
        )

        heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=0.5)

        if lost_lease.is_set():
            # Worker was fenced out because another worker claimed the expired lease
            return {
                "match_id": spec.match_id,
                "status": "STALE_ABORTED",
                "committed": False,
                "error": "Lease was lost during match execution",
            }

        # Process and store replay if present (only for authoritative owner)
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

        committed = db.update_match_result(result, lease_token=lease_token)
        res_dict = result.to_dict()
        res_dict["committed"] = committed
        return res_dict

    except Exception as e:
        heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=0.5)

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
        committed = False
        if not lost_lease.is_set():
            committed = db.update_match_result(fail_res, lease_token=lease_token)
        res_dict = fail_res.to_dict()
        res_dict["committed"] = committed
        return res_dict
