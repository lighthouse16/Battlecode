"""SQLite database client with WAL mode and resilient concurrency support."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from battlelab.core.errors import StorageError
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
                        tags_json, build_result_json, build_logs
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        tags_json=excluded.tags_json,
                        experiment_id=excluded.experiment_id,
                        hypothesis=excluded.hypothesis
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
                    ),
                )

    def get_artifact(self, artifact_id: str) -> BotArtifact | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if not row:
                return None
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
            )

    def list_artifacts(self) -> list[BotArtifact]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM artifacts ORDER BY created_at DESC").fetchall()
            return [
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
                )
                for row in rows
            ]

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
                        match_id, tournament_id, experiment_id, adapter_name, adapter_version,
                        bot_a_id, bot_b_id, map_name, seed, side_assignment_json, status,
                        retry_attempt, created_at, spec_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                    ON CONFLICT(match_id) DO NOTHING
                    """,
                    (
                        spec.match_id,
                        spec.tournament_id,
                        spec.experiment_id,
                        spec.adapter_name,
                        spec.adapter_version,
                        spec.bot_a_id,
                        spec.bot_b_id,
                        spec.map_name,
                        spec.seed,
                        json.dumps(spec.side_assignment),
                        spec.retry_attempt,
                        created_at,
                        json.dumps(spec.to_dict()),
                    ),
                )

    def update_match_result(self, result: MatchResult) -> None:
        with self.connect() as conn:
            with conn:
                fc_cat = result.failure_classification.category.value if result.failure_classification else None
                fc_json = json.dumps(result.failure_classification.to_dict()) if result.failure_classification else None
                conn.execute(
                    """
                    UPDATE matches SET
                        status = 'COMPLETED',
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
                        completed_at = ?,
                        result_json = ?
                    WHERE match_id = ?
                    """,
                    (
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
                        result.completed_at,
                        json.dumps(result.to_dict()),
                        result.match_id,
                    ),
                )

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
                        representative_replays_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(experiment_id) DO UPDATE SET
                        status=excluded.status,
                        completed_at=excluded.completed_at,
                        results_summary_json=excluded.results_summary_json,
                        promotion_decision=excluded.promotion_decision,
                        rejection_reason=excluded.rejection_reason,
                        representative_replays_json=excluded.representative_replays_json
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
                    ),
                )

    def get_experiment(self, experiment_id: str) -> Experiment | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if not row:
                return None
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
                results_summary=json.loads(row["results_summary_json"]) if row["results_summary_json"] else None,
                promotion_decision=row["promotion_decision"],
                rejection_reason=row["rejection_reason"],
                representative_replays=json.loads(row["representative_replays_json"]),
            )

    def list_experiments(self) -> list[Experiment]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
            return [
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
                    results_summary=json.loads(row["results_summary_json"]) if row["results_summary_json"] else None,
                    promotion_decision=row["promotion_decision"],
                    rejection_reason=row["rejection_reason"],
                    representative_replays=json.loads(row["representative_replays_json"]),
                )
                for row in rows
            ]

    # --- Promotions ---

    def save_promotion(
        self,
        promotion_id: str,
        experiment_id: str,
        artifact_id: str,
        promoted_at: str,
        manifest_snapshot: dict[str, Any],
        reason: str = "",
        mode: str = "MANUAL",
    ) -> None:
        with self.connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO promotions (
                        promotion_id, experiment_id, artifact_id, promoted_at,
                        mode, manifest_snapshot_json, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        promotion_id,
                        experiment_id,
                        artifact_id,
                        promoted_at,
                        mode,
                        json.dumps(manifest_snapshot),
                        reason,
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
                }
                for r in rows
            ]
