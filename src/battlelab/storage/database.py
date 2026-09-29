"""SQLite database client with WAL mode and resilient concurrency support."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from battlelab.core.models import (
    BotArtifact,
    Experiment,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)
from battlelab.storage.migrations import apply_migrations
from battlelab.storage.paths import get_database_path


class Database:
    """Manages SQLite storage for Battlelab metadata."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else get_database_path()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_migrations(conn)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide a connection with WAL mode and busy timeout configured."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,  # Autocommit mode; transactions managed via 'with conn:'
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
        finally:
            conn.close()

    # --- Artifacts ---

    def save_artifact(self, artifact: BotArtifact) -> None:
        with self.connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, display_name, source_location, language,
                        git_commit, dirty_worktree, source_hash, build_config_hash,
                        parent_artifact_id, created_at, experiment_id, hypothesis,
                        tags_json, build_result_json, build_logs,
                        entrypoint_relpath, manifest_json, manifest_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        tags_json=excluded.tags_json,
                        experiment_id=excluded.experiment_id,
                        hypothesis=excluded.hypothesis,
                        entrypoint_relpath=excluded.entrypoint_relpath,
                        manifest_json=excluded.manifest_json,
                        manifest_hash=excluded.manifest_hash
                    """,
                    (
                        artifact.artifact_id,
                        artifact.display_name,
                        artifact.source_location,
                        artifact.language,
                        artifact.git_commit,
                        1 if artifact.dirty_worktree else 0,
                        artifact.source_hash,
                        artifact.build_config_hash,
                        artifact.parent_artifact_id,
                        artifact.created_at,
                        artifact.experiment_id,
                        artifact.hypothesis,
                        json.dumps(artifact.tags),
                        json.dumps(artifact.build_result),
                        artifact.build_logs,
                        artifact.entrypoint_relpath,
                        json.dumps(artifact.manifest),
                        artifact.manifest_hash,
                    ),
                )

    def get_artifact(self, artifact_id: str) -> BotArtifact | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if not row:
                return None
            keys = row.keys()
            return BotArtifact(
                artifact_id=row["artifact_id"],
                display_name=row["display_name"],
                source_location=row["source_location"],
                language=row["language"],
                git_commit=row["git_commit"],
                dirty_worktree=bool(row["dirty_worktree"]),
                source_hash=row["source_hash"],
                build_config_hash=row["build_config_hash"],
                parent_artifact_id=row["parent_artifact_id"],
                created_at=row["created_at"],
                experiment_id=row["experiment_id"],
                hypothesis=row["hypothesis"],
                tags=json.loads(row["tags_json"]),
                build_result=json.loads(row["build_result_json"]),
                build_logs=row["build_logs"],
                entrypoint_relpath=row["entrypoint_relpath"]
                if "entrypoint_relpath" in keys
                else "",
                manifest=json.loads(row["manifest_json"])
                if "manifest_json" in keys and row["manifest_json"]
                else {},
                manifest_hash=row["manifest_hash"]
                if "manifest_hash" in keys and row["manifest_hash"]
                else "",
            )

    def list_artifacts(self) -> list[BotArtifact]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM artifacts ORDER BY created_at DESC").fetchall()
            result: list[BotArtifact] = []
            for row in rows:
                keys = row.keys()
                result.append(
                    BotArtifact(
                        artifact_id=row["artifact_id"],
                        display_name=row["display_name"],
                        source_location=row["source_location"],
                        language=row["language"],
                        git_commit=row["git_commit"],
                        dirty_worktree=bool(row["dirty_worktree"]),
                        source_hash=row["source_hash"],
                        build_config_hash=row["build_config_hash"],
                        parent_artifact_id=row["parent_artifact_id"],
                        created_at=row["created_at"],
                        experiment_id=row["experiment_id"],
                        hypothesis=row["hypothesis"],
                        tags=json.loads(row["tags_json"]),
                        build_result=json.loads(row["build_result_json"]),
                        build_logs=row["build_logs"],
                        entrypoint_relpath=row["entrypoint_relpath"]
                        if "entrypoint_relpath" in keys
                        else "",
                        manifest=json.loads(row["manifest_json"])
                        if "manifest_json" in keys and row["manifest_json"]
                        else {},
                        manifest_hash=row["manifest_hash"]
                        if "manifest_hash" in keys and row["manifest_hash"]
                        else "",
                    )
                )
            return result

    # --- Tournaments ---

    def save_tournament(
        self,
        tournament_id: str,
        name: str,
        config_hash: str,
        created_at: str,
        config: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO tournaments (
                        tournament_id, name, status, config_hash, created_at, config_json, metadata_json
                    ) VALUES (?, ?, 'PENDING', ?, ?, ?, ?)
                    ON CONFLICT(tournament_id) DO NOTHING
                    """,
                    (
                        tournament_id,
                        name,
                        config_hash,
                        created_at,
                        json.dumps(config),
                        json.dumps(metadata or {}),
                    ),
                )

    def update_tournament_status(
        self, tournament_id: str, status: str, completed_at: str | None = None
    ) -> None:
        with self.connect() as conn:
            with conn:
                conn.execute(
                    "UPDATE tournaments SET status = ?, completed_at = ? WHERE tournament_id = ?",
                    (status, completed_at, tournament_id),
                )

    def get_tournament(self, tournament_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tournaments WHERE tournament_id = ?", (tournament_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "tournament_id": row["tournament_id"],
                "name": row["name"],
                "status": row["status"],
                "config_hash": row["config_hash"],
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
                "config": json.loads(row["config_json"]),
                "metadata": json.loads(row["metadata_json"]),
            }

    # --- Matches ---

    def save_match_spec(self, spec: MatchSpec, created_at: str) -> None:
        with self.connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO matches (
                        match_id, tournament_id, experiment_id, pair_id, adapter_name, adapter_version,
                        bot_a_id, bot_b_id, map_name, seed, side_assignment_json, status,
                        attempt_count, max_attempts, retry_attempt, created_at, spec_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)
                    ON CONFLICT(match_id) DO NOTHING
                    """,
                    (
                        spec.match_id,
                        spec.tournament_id,
                        spec.experiment_id,
                        spec.pair_id,
                        spec.adapter_name,
                        spec.adapter_version,
                        spec.bot_a_id,
                        spec.bot_b_id,
                        spec.map_name,
                        spec.seed,
                        json.dumps(spec.side_assignment),
                        spec.max_attempts,
                        spec.retry_attempt,
                        created_at,
                        json.dumps(spec.to_dict()),
                    ),
                )

    def lease_next_match(
        self,
        tournament_id: str | None = None,
        worker_id: str = "default_worker",
        lease_duration_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        """Atomically claim a pending or recoverable match under a fenced renewable lease."""
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_expires_iso = datetime.fromtimestamp(
            now.timestamp() + lease_duration_seconds, timezone.utc
        ).isoformat()
        lease_token = f"lease_{uuid.uuid4().hex}"

        with self.connect() as conn:
            while True:
                with conn:
                    if tournament_id is not None:
                        query = """
                        SELECT * FROM matches
                        WHERE tournament_id = ?
                          AND (
                              status IN ('PENDING', 'RETRYABLE_FAILURE')
                              OR (status = 'RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                          )
                          AND attempt_count < max_attempts
                        ORDER BY rowid ASC
                        LIMIT 1
                        """
                        row = conn.execute(query, (tournament_id, now_iso)).fetchone()
                    else:
                        query = """
                        SELECT * FROM matches
                        WHERE (
                              status IN ('PENDING', 'RETRYABLE_FAILURE')
                              OR (status = 'RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                          )
                          AND attempt_count < max_attempts
                        ORDER BY rowid ASC
                        LIMIT 1
                        """
                        row = conn.execute(query, (now_iso,)).fetchone()

                    if not row:
                        return None

                    match_id = row["match_id"]
                    cur = conn.execute(
                        """
                        UPDATE matches SET
                            status = 'RUNNING',
                            worker_id = ?,
                            lease_token = ?,
                            lease_timestamp = ?,
                            lease_expires_at = ?,
                            last_heartbeat = ?,
                            attempt_count = attempt_count + 1
                        WHERE match_id = ?
                          AND (
                              status IN ('PENDING', 'RETRYABLE_FAILURE')
                              OR (status = 'RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                          )
                        """,
                        (
                            worker_id,
                            lease_token,
                            now_iso,
                            lease_expires_iso,
                            now_iso,
                            match_id,
                            now_iso,
                        ),
                    )
                    if cur.rowcount == 0:
                        continue

                    updated_row = conn.execute(
                        "SELECT * FROM matches WHERE match_id = ?", (match_id,)
                    ).fetchone()
                    return dict(updated_row) if updated_row else None

    def renew_lease(
        self,
        match_id: str,
        worker_id: str,
        lease_token: str,
        additional_seconds: float = 30.0,
    ) -> bool:
        """Renew active match lease if worker and lease token match."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        new_expires_iso = datetime.fromtimestamp(
            now.timestamp() + additional_seconds, timezone.utc
        ).isoformat()

        with self.connect() as conn:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE matches SET
                        lease_expires_at = ?,
                        last_heartbeat = ?
                    WHERE match_id = ?
                      AND worker_id = ?
                      AND lease_token = ?
                      AND status = 'RUNNING'
                    """,
                    (new_expires_iso, now_iso, match_id, worker_id, lease_token),
                )
                return cur.rowcount == 1

    def recover_expired_leases(self) -> int:
        """Reset expired RUNNING leases back to PENDING."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE matches SET
                        status = 'PENDING',
                        worker_id = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE status = 'RUNNING'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < ?
                    """,
                    (now_iso,),
                )
                return cur.rowcount

    def update_match_result(self, result: MatchResult, lease_token: str | None = None) -> bool:
        """Atomically persist match result conditioned on active lease token."""
        with self.connect() as conn:
            with conn:
                fc_cat = (
                    result.failure_classification.category.value
                    if result.failure_classification
                    else None
                )
                fc_json = (
                    json.dumps(result.failure_classification.to_dict())
                    if result.failure_classification
                    else None
                )
                evidence = (
                    result.failure_classification.evidence
                    if result.failure_classification
                    else None
                )

                retry_class: str | None = None
                if result.outcome == MatchOutcome.INFRASTRUCTURE_FAILURE:
                    row = conn.execute(
                        "SELECT attempt_count, max_attempts FROM matches WHERE match_id = ?",
                        (result.match_id,),
                    ).fetchone()
                    attempts = row["attempt_count"] if row else 1
                    max_att = row["max_attempts"] if row else 3
                    if attempts >= max_att:
                        status = "TERMINAL_FAILURE"
                        retry_class = "INFRASTRUCTURE_MAX_EXCEEDED"
                    else:
                        status = "RETRYABLE_FAILURE"
                        retry_class = "INFRASTRUCTURE_RETRYABLE"
                else:
                    status = "COMPLETED"
                    retry_class = "NONE"

                where_clause = "WHERE match_id = ?"
                params: list[Any] = [
                    status,
                    result.outcome.value,
                    result.winner,
                    result.score_a,
                    result.score_b,
                    result.duration_ms,
                    result.turns_played,
                    1 if result.crashed_a else 0,
                    1 if result.crashed_b else 0,
                    1 if result.timed_out_a else 0,
                    1 if result.timed_out_b else 0,
                    1 if result.invalid_action_a else 0,
                    1 if result.invalid_action_b else 0,
                    result.replay_path,
                    result.replay_hash,
                    result.stdout_path,
                    result.stderr_path,
                    fc_cat,
                    fc_json,
                    evidence,
                    retry_class,
                    result.completed_at,
                    json.dumps(result.to_dict()),
                    result.match_id,
                ]

                if lease_token is not None:
                    where_clause = "WHERE match_id = ? AND lease_token = ? AND status = 'RUNNING'"
                    params.append(lease_token)

                sql = f"""
                UPDATE matches SET
                    status = ?,
                    outcome = ?,
                    winner = ?,
                    score_a = ?,
                    score_b = ?,
                    duration_ms = ?,
                    turns_played = ?,
                    crashed_a = ?,
                    crashed_b = ?,
                    timed_out_a = ?,
                    timed_out_b = ?,
                    invalid_action_a = ?,
                    invalid_action_b = ?,
                    replay_path = ?,
                    replay_hash = ?,
                    stdout_path = ?,
                    stderr_path = ?,
                    failure_category = ?,
                    failure_json = ?,
                    last_infrastructure_error = ?,
                    retry_classification = ?,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    completed_at = ?,
                    result_json = ?
                {where_clause}
                """
                cur = conn.execute(sql, tuple(params))
                return cur.rowcount > 0

    def get_match(self, match_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
            if not row:
                return None
            return dict(row)

    def list_matches_by_tournament(self, tournament_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE tournament_id = ? ORDER BY rowid ASC",
                (tournament_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_matches_by_experiment(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE experiment_id = ? ORDER BY rowid ASC",
                (experiment_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Experiments ---

    def save_experiment(self, exp: Experiment) -> None:
        with self.connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO experiments (
                        experiment_id, hypothesis, baseline_artifact_id, challenger_artifact_id,
                        intended_change, status, created_at, completed_at,
                        evaluation_matrix_json, acceptance_criteria_json,
                        results_summary_json, promotion_decision, rejection_reason,
                        representative_replays_json,
                        evaluation_config_json, evaluation_config_hash,
                        opponent_pool_config_json, opponent_pool_config_hash,
                        promotion_config_json, promotion_config_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(experiment_id) DO UPDATE SET
                        status=excluded.status,
                        completed_at=excluded.completed_at,
                        results_summary_json=excluded.results_summary_json,
                        promotion_decision=excluded.promotion_decision,
                        rejection_reason=excluded.rejection_reason,
                        representative_replays_json=excluded.representative_replays_json,
                        evaluation_config_json=excluded.evaluation_config_json,
                        evaluation_config_hash=excluded.evaluation_config_hash,
                        opponent_pool_config_json=excluded.opponent_pool_config_json,
                        opponent_pool_config_hash=excluded.opponent_pool_config_hash,
                        promotion_config_json=excluded.promotion_config_json,
                        promotion_config_hash=excluded.promotion_config_hash
                    """,
                    (
                        exp.experiment_id,
                        exp.hypothesis,
                        exp.baseline_artifact_id,
                        exp.challenger_artifact_id,
                        exp.intended_change,
                        exp.status,
                        exp.created_at,
                        exp.completed_at,
                        json.dumps(exp.evaluation_matrix),
                        json.dumps(exp.acceptance_criteria),
                        json.dumps(exp.results_summary) if exp.results_summary else None,
                        exp.promotion_decision,
                        exp.rejection_reason,
                        json.dumps(exp.representative_replays),
                        json.dumps(exp.evaluation_config),
                        exp.evaluation_config_hash,
                        json.dumps(exp.opponent_pool_config),
                        exp.opponent_pool_config_hash,
                        json.dumps(exp.promotion_config),
                        exp.promotion_config_hash,
                    ),
                )

    def get_experiment(self, experiment_id: str) -> Experiment | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if not row:
                return None
            keys = row.keys()
            return Experiment(
                experiment_id=row["experiment_id"],
                hypothesis=row["hypothesis"],
                baseline_artifact_id=row["baseline_artifact_id"],
                challenger_artifact_id=row["challenger_artifact_id"],
                intended_change=row["intended_change"],
                status=row["status"],
                created_at=row["created_at"],
                completed_at=row["completed_at"],
                evaluation_matrix=json.loads(row["evaluation_matrix_json"]),
                acceptance_criteria=json.loads(row["acceptance_criteria_json"]),
                results_summary=json.loads(row["results_summary_json"])
                if row["results_summary_json"]
                else None,
                promotion_decision=row["promotion_decision"],
                rejection_reason=row["rejection_reason"],
                representative_replays=json.loads(row["representative_replays_json"]),
                evaluation_config=json.loads(row["evaluation_config_json"])
                if "evaluation_config_json" in keys and row["evaluation_config_json"]
                else {},
                evaluation_config_hash=row["evaluation_config_hash"]
                if "evaluation_config_hash" in keys and row["evaluation_config_hash"]
                else "",
                opponent_pool_config=json.loads(row["opponent_pool_config_json"])
                if "opponent_pool_config_json" in keys and row["opponent_pool_config_json"]
                else {},
                opponent_pool_config_hash=row["opponent_pool_config_hash"]
                if "opponent_pool_config_hash" in keys and row["opponent_pool_config_hash"]
                else "",
                promotion_config=json.loads(row["promotion_config_json"])
                if "promotion_config_json" in keys and row["promotion_config_json"]
                else {},
                promotion_config_hash=row["promotion_config_hash"]
                if "promotion_config_hash" in keys and row["promotion_config_hash"]
                else "",
            )

    def list_experiments(self) -> list[Experiment]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
            result: list[Experiment] = []
            for row in rows:
                keys = row.keys()
                result.append(
                    Experiment(
                        experiment_id=row["experiment_id"],
                        hypothesis=row["hypothesis"],
                        baseline_artifact_id=row["baseline_artifact_id"],
                        challenger_artifact_id=row["challenger_artifact_id"],
                        intended_change=row["intended_change"],
                        status=row["status"],
                        created_at=row["created_at"],
                        completed_at=row["completed_at"],
                        evaluation_matrix=json.loads(row["evaluation_matrix_json"]),
                        acceptance_criteria=json.loads(row["acceptance_criteria_json"]),
                        results_summary=json.loads(row["results_summary_json"])
                        if row["results_summary_json"]
                        else None,
                        promotion_decision=row["promotion_decision"],
                        rejection_reason=row["rejection_reason"],
                        representative_replays=json.loads(row["representative_replays_json"]),
                        evaluation_config=json.loads(row["evaluation_config_json"])
                        if "evaluation_config_json" in keys and row["evaluation_config_json"]
                        else {},
                        evaluation_config_hash=row["evaluation_config_hash"]
                        if "evaluation_config_hash" in keys and row["evaluation_config_hash"]
                        else "",
                        opponent_pool_config=json.loads(row["opponent_pool_config_json"])
                        if "opponent_pool_config_json" in keys and row["opponent_pool_config_json"]
                        else {},
                        opponent_pool_config_hash=row["opponent_pool_config_hash"]
                        if "opponent_pool_config_hash" in keys and row["opponent_pool_config_hash"]
                        else "",
                        promotion_config=json.loads(row["promotion_config_json"])
                        if "promotion_config_json" in keys and row["promotion_config_json"]
                        else {},
                        promotion_config_hash=row["promotion_config_hash"]
                        if "promotion_config_hash" in keys and row["promotion_config_hash"]
                        else "",
                    )
                )
            return result

    # --- Promotions ---

    def save_promotion(
        self,
        promotion_id: str,
        experiment_id: str | None,
        artifact_id: str,
        promoted_at: str,
        manifest_snapshot: dict[str, Any],
        reason: str = "",
        mode: str = "MANUAL_VERIFIED",
        promoted_by: str = "system",
        override_acknowledgement: str | None = None,
        previous_champion_id: str | None = None,
        gate_violations: list[str] | None = None,
        gate_violations_json: str | None = None,
        artifact_manifest_hash: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        if gate_violations_json is None:
            gate_violations_json = json.dumps(gate_violations or [])
        with self.connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO promotions (
                        promotion_id, experiment_id, artifact_id, promoted_at,
                        promoted_by, mode, manifest_snapshot_json, reason, override_acknowledgement,
                        previous_champion_id, gate_violations_json, artifact_manifest_hash, config_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        promotion_id,
                        experiment_id,
                        artifact_id,
                        promoted_at,
                        promoted_by,
                        mode,
                        json.dumps(manifest_snapshot),
                        reason,
                        override_acknowledgement,
                        previous_champion_id,
                        gate_violations_json,
                        artifact_manifest_hash or "",
                        config_hash or "",
                    ),
                )

    def list_promotions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM promotions ORDER BY promoted_at DESC").fetchall()
            return [
                {
                    "promotion_id": r["promotion_id"],
                    "experiment_id": r["experiment_id"],
                    "artifact_id": r["artifact_id"],
                    "promoted_at": r["promoted_at"],
                    "promoted_by": r["promoted_by"],
                    "mode": r["mode"],
                    "manifest_snapshot": json.loads(r["manifest_snapshot_json"]),
                    "reason": r["reason"],
                    "override_acknowledgement": r["override_acknowledgement"]
                    if "override_acknowledgement" in r.keys()
                    else None,
                    "previous_champion_id": r["previous_champion_id"]
                    if "previous_champion_id" in r.keys()
                    else None,
                    "gate_violations": json.loads(r["gate_violations_json"])
                    if "gate_violations_json" in r.keys() and r["gate_violations_json"]
                    else [],
                    "artifact_manifest_hash": r["artifact_manifest_hash"]
                    if "artifact_manifest_hash" in r.keys()
                    else "",
                    "config_hash": r["config_hash"] if "config_hash" in r.keys() else "",
                }
                for r in rows
            ]
