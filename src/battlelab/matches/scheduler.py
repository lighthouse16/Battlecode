"""Parallel, safe, and resumable tournament scheduler with atomic SQLite job-leasing."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from battlelab.core.hashing import hash_dict
from battlelab.core.identifiers import generate_match_id
from battlelab.core.models import MatchSpec
from battlelab.matches.worker import execute_match_job
from battlelab.storage.database import Database


class TournamentScheduler:
    """Manages tournament execution with atomic SQLite leases, crash recovery, and multi-scheduler safety."""

    def __init__(
        self,
        db: Database | None = None,
        max_workers: int = 4,
        lease_duration_seconds: float = 30.0,
    ) -> None:
        self.db = db or Database()
        self.max_workers = max_workers
        self.lease_duration_seconds = lease_duration_seconds
        self.scheduler_id = f"sched_{uuid.uuid4().hex[:8]}"

    def create_tournament(
        self,
        tournament_id: str,
        name: str,
        specs: list[MatchSpec],
        config: dict[str, Any] | None = None,
    ) -> str:
        """Create and queue a tournament and its match specs idempotently."""
        conf = config or {}
        conf_hash = hash_dict(conf)
        created_at = datetime.now(timezone.utc).isoformat()

        self.db.save_tournament(
            tournament_id=tournament_id,
            name=name,
            config_hash=conf_hash,
            created_at=created_at,
            config=conf,
        )

        for spec in specs:
            if not spec.tournament_id:
                spec.tournament_id = tournament_id
                raw_d = spec.to_dict()
                raw_d["tournament_id"] = tournament_id
                spec.match_id = generate_match_id(raw_d)
            self.db.save_match_spec(spec, created_at)

        return tournament_id

    def run_tournament(
        self,
        tournament_id: str,
        on_match_complete: Callable[[dict[str, Any]], None] | None = None,
        stop_after: int | None = None,
    ) -> dict[str, Any]:
        """Execute or resume tournament matches using atomic job leases."""
        trn = self.db.get_tournament(tournament_id)
        if not trn:
            raise ValueError(f"Tournament not found: {tournament_id}")

        self.db.update_tournament_status(tournament_id, "RUNNING")
        db_path_str = str(self.db.db_path)

        completed_in_session = 0
        leased_in_session = 0
        stop_requested = False
        session_lock = threading.Lock()

        def _worker_loop(worker_num: int):
            nonlocal completed_in_session, leased_in_session, stop_requested
            worker_id = f"{self.scheduler_id}_w{worker_num}"
            while True:
                with session_lock:
                    if stop_requested:
                        break
                    if stop_after is not None and leased_in_session >= stop_after:
                        break
                    job = self.db.lease_next_match(
                        tournament_id=tournament_id,
                        worker_id=worker_id,
                        lease_duration_seconds=self.lease_duration_seconds,
                    )
                    if job:
                        leased_in_session += 1
                if not job:
                    # No pending or recoverable jobs
                    break

                # Execute claimed match job
                spec_dict = json.loads(job["spec_json"])
                res = execute_match_job(
                    spec_dict=spec_dict,
                    db_path=db_path_str,
                    worker_id=job.get("worker_id", worker_id),
                    lease_token=job.get("lease_token"),
                    lease_duration_seconds=self.lease_duration_seconds,
                )
                with session_lock:
                    completed_in_session += 1

                if on_match_complete:
                    on_match_complete(res)

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = [executor.submit(_worker_loop, i) for i in range(self.max_workers)]
                for f in futures:
                    f.result()
        except (KeyboardInterrupt, SystemExit):
            self.db.update_tournament_status(tournament_id, "INTERRUPTED")
            raise

        all_matches = self.db.list_matches_by_tournament(tournament_id)
        pending_matches = [
            m for m in all_matches if m.get("status") in ("PENDING", "RUNNING", "RETRYABLE_FAILURE")
        ]

        now_iso = datetime.now(timezone.utc).isoformat()
        if pending_matches:
            final_status = "INTERRUPTED"
            self.db.update_tournament_status(tournament_id, "INTERRUPTED")
        else:
            final_status = "COMPLETED"
            self.db.update_tournament_status(tournament_id, "COMPLETED", completed_at=now_iso)

        completed_count = sum(1 for m in all_matches if m.get("status") == "COMPLETED")
        return {
            "tournament_id": tournament_id,
            "status": final_status,
            "total_matches": len(all_matches),
            "completed_matches": completed_count,
        }
