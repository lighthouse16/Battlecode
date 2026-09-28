"""Parallel, resumable tournament scheduler."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from battlelab.core.hashing import hash_dict
from battlelab.core.models import MatchOutcome, MatchResult, MatchSpec
from battlelab.matches.worker import execute_match_job
from battlelab.storage.database import Database


class TournamentScheduler:
    """Manages parallel tournament execution with resume support and duplicate prevention."""

    def __init__(self, db: Database | None = None, max_workers: int = 4) -> None:
        self.db = db or Database()
        self.max_workers = max_workers

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

        # Save tournament record
        self.db.save_tournament(
            tournament_id=tournament_id,
            name=name,
            config_hash=conf_hash,
            created_at=created_at,
            config=conf,
        )

        # Save all match specs
        for spec in specs:
            spec.tournament_id = tournament_id
            self.db.save_match_spec(spec, created_at)

        return tournament_id

    def run_tournament(
        self,
        tournament_id: str,
        on_match_complete: Callable[[dict[str, Any]], None] | None = None,
        stop_after: int | None = None,
    ) -> dict[str, Any]:
        """Execute or resume tournament matches in parallel."""
        trn = self.db.get_tournament(tournament_id)
        if not trn:
            raise ValueError(f"Tournament not found: {tournament_id}")

        self.db.update_tournament_status(tournament_id, "RUNNING")
        all_matches = self.db.list_matches_by_tournament(tournament_id)

        # Identify pending matches
        pending_matches = [m for m in all_matches if m.get("status") != "COMPLETED"]

        if stop_after is not None and stop_after > 0:
            pending_to_run = pending_matches[:stop_after]
        else:
            pending_to_run = pending_matches

        completed_count = len(all_matches) - len(pending_matches)
        interrupted = False

        if pending_to_run:
            db_path_str = str(self.db.db_path)
            try:
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    future_to_spec = {
                        executor.submit(
                            execute_match_job,
                            json.loads(m["spec_json"]),
                            db_path_str,
                        ): m["match_id"]
                        for m in pending_to_run
                    }

                    for future in as_completed(future_to_spec):
                        match_id = future_to_spec[future]
                        res_dict = future.result()
                        completed_count += 1
                        if on_match_complete:
                            on_match_complete(res_dict)

            except (KeyboardInterrupt, SystemExit):
                interrupted = True
                self.db.update_tournament_status(tournament_id, "INTERRUPTED")
                raise

        # Check final status
        remaining = self.db.list_matches_by_tournament(tournament_id)
        still_pending = any(m.get("status") != "COMPLETED" for m in remaining)

        now_iso = datetime.now(timezone.utc).isoformat()
        if still_pending:
            self.db.update_tournament_status(tournament_id, "INTERRUPTED")
            final_status = "INTERRUPTED"
        else:
            self.db.update_tournament_status(tournament_id, "COMPLETED", completed_at=now_iso)
            final_status = "COMPLETED"

        return {
            "tournament_id": tournament_id,
            "status": final_status,
            "total_matches": len(all_matches),
            "completed_matches": completed_count,
        }
