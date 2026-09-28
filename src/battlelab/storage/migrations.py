"""Database schema and migrations for Battlelab SQLite metadata."""

import sqlite3

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source_location TEXT NOT NULL,
    language TEXT NOT NULL,
    git_commit TEXT,
    dirty_worktree INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL,
    build_config_hash TEXT NOT NULL DEFAULT '',
    parent_artifact_id TEXT,
    created_at TEXT NOT NULL,
    experiment_id TEXT,
    hypothesis TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    build_result_json TEXT NOT NULL DEFAULT '{}',
    build_logs TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    tournament_id TEXT,
    experiment_id TEXT,
    pair_id TEXT,
    adapter_name TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    bot_a_id TEXT NOT NULL,
    bot_b_id TEXT NOT NULL,
    map_name TEXT NOT NULL,
    seed INTEGER NOT NULL,
    side_assignment_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    worker_id TEXT,
    lease_timestamp TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_infrastructure_error TEXT,
    retry_attempt INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    winner TEXT,
    score_a REAL DEFAULT 0.0,
    score_b REAL DEFAULT 0.0,
    duration_ms REAL DEFAULT 0.0,
    turns_played INTEGER DEFAULT 0,
    crashed_a INTEGER DEFAULT 0,
    crashed_b INTEGER DEFAULT 0,
    timed_out_a INTEGER DEFAULT 0,
    timed_out_b INTEGER DEFAULT 0,
    invalid_action_a INTEGER DEFAULT 0,
    invalid_action_b INTEGER DEFAULT 0,
    replay_path TEXT,
    replay_hash TEXT,
    stdout_path TEXT,
    stderr_path TEXT,
    failure_category TEXT,
    failure_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    spec_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    FOREIGN KEY(tournament_id) REFERENCES tournaments(tournament_id),
    FOREIGN KEY(bot_a_id) REFERENCES artifacts(artifact_id),
    FOREIGN KEY(bot_b_id) REFERENCES artifacts(artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_tournament ON matches(tournament_id);
CREATE INDEX IF NOT EXISTS idx_matches_experiment ON matches(experiment_id);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    baseline_artifact_id TEXT NOT NULL,
    challenger_artifact_id TEXT NOT NULL,
    intended_change TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    evaluation_matrix_json TEXT NOT NULL DEFAULT '{}',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '{}',
    results_summary_json TEXT,
    promotion_decision TEXT NOT NULL DEFAULT 'PENDING',
    rejection_reason TEXT,
    representative_replays_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(baseline_artifact_id) REFERENCES artifacts(artifact_id),
    FOREIGN KEY(challenger_artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS promotions (
    promotion_id TEXT PRIMARY KEY,
    experiment_id TEXT,
    artifact_id TEXT NOT NULL,
    promoted_at TEXT NOT NULL,
    promoted_by TEXT NOT NULL DEFAULT 'system',
    mode TEXT NOT NULL DEFAULT 'MANUAL',
    manifest_snapshot_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    override_acknowledgement TEXT,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
);
"""


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply database schema and upgrade missing columns gracefully."""
    with conn:
        conn.executescript(BASE_SCHEMA)

        cur = conn.cursor()

        # 1. Matches table migrations
        cur.execute("PRAGMA table_info(matches)")
        existing_cols = {row[1] for row in cur.fetchall()}
        new_match_cols = [
            ("pair_id", "TEXT"),
            ("worker_id", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_timestamp", "TEXT"),
            ("lease_expires_at", "TEXT"),
            ("last_heartbeat", "TEXT"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
            ("last_infrastructure_error", "TEXT"),
            ("retry_classification", "TEXT"),
        ]
        for col_name, col_type in new_match_cols:
            if col_name not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE matches ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_pair ON matches(pair_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_lease ON matches(lease_token)")

        # 2. Artifacts table migrations
        cur.execute("PRAGMA table_info(artifacts)")
        existing_art_cols = {row[1] for row in cur.fetchall()}
        new_art_cols = [
            ("entrypoint_relpath", "TEXT NOT NULL DEFAULT ''"),
            ("manifest_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("manifest_hash", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_type in new_art_cols:
            if col_name not in existing_art_cols:
                try:
                    conn.execute(f"ALTER TABLE artifacts ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        # 3. Promotions table migrations
        cur.execute("PRAGMA table_info(promotions)")
        existing_prom_cols = {row[1] for row in cur.fetchall()}
        new_prom_cols = [
            ("override_acknowledgement", "TEXT"),
            ("previous_champion_id", "TEXT"),
            ("gate_violations_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("artifact_manifest_hash", "TEXT NOT NULL DEFAULT ''"),
            ("config_hash", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_type in new_prom_cols:
            if col_name not in existing_prom_cols:
                try:
                    conn.execute(f"ALTER TABLE promotions ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        cur.execute("SELECT sql FROM sqlite_master WHERE name='promotions'")
        prom_row = cur.fetchone()
        if prom_row and (
            "experiment_id TEXT NOT NULL" in prom_row[0] or "REFERENCES experiments" in prom_row[0]
        ):
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("""
            CREATE TABLE promotions_v3 (
                promotion_id TEXT PRIMARY KEY,
                experiment_id TEXT,
                artifact_id TEXT NOT NULL,
                promoted_at TEXT NOT NULL,
                promoted_by TEXT NOT NULL DEFAULT 'system',
                mode TEXT NOT NULL DEFAULT 'MANUAL',
                manifest_snapshot_json TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                override_acknowledgement TEXT,
                previous_champion_id TEXT,
                gate_violations_json TEXT NOT NULL DEFAULT '[]',
                artifact_manifest_hash TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
            )
            """)
            conn.execute("""
            INSERT INTO promotions_v3 (
                promotion_id, experiment_id, artifact_id, promoted_at, promoted_by, mode,
                manifest_snapshot_json, reason, override_acknowledgement, previous_champion_id,
                gate_violations_json, artifact_manifest_hash, config_hash
            )
            SELECT
                promotion_id, experiment_id, artifact_id, promoted_at, promoted_by, mode,
                manifest_snapshot_json, reason, override_acknowledgement, previous_champion_id,
                gate_violations_json, artifact_manifest_hash, config_hash
            FROM promotions
            """)
            conn.execute("DROP TABLE promotions")
            conn.execute("ALTER TABLE promotions_v3 RENAME TO promotions")
            conn.execute("PRAGMA foreign_keys=ON")

        # 4. Experiments table migrations
        cur.execute("PRAGMA table_info(experiments)")
        existing_exp_cols = {row[1] for row in cur.fetchall()}
        new_exp_cols = [
            ("evaluation_config_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("evaluation_config_hash", "TEXT NOT NULL DEFAULT ''"),
            ("opponent_pool_config_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("opponent_pool_config_hash", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_type in new_exp_cols:
            if col_name not in existing_exp_cols:
                try:
                    conn.execute(f"ALTER TABLE experiments ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (3)")
