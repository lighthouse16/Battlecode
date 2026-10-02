"""MockAdapter implementation with true subprocess artifact execution."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.adapters.base import GameAdapter
from battlelab.adapters.mock.engine import MockEngine
from battlelab.adapters.mock.maps import MOCK_MAPS
from battlelab.bots.artifacts import verify_artifact_integrity
from battlelab.bots.process_runner import BotStartupError, BotSubprocess
from battlelab.core.hashing import hash_file
from battlelab.core.models import (
    BotArtifact,
    Capability,
    FailureCategory,
    FailureClassification,
    MatchOutcome,
    MatchResult,
    MatchSpec,
)
from battlelab.storage.paths import (
    get_artifacts_dir,
    get_champion_manifest_path,
    get_database_path,
)


def _snapshot_protected_db_state(db_path: Path, current_match_id: str) -> dict[str, Any] | None:
    """Snapshot protected database invariants before match execution."""
    if not db_path.is_file():
        return None
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r[0] for r in cur.fetchall()]
        other_matches: dict[str, tuple[Any, ...]] = {}
        if "matches" in tables:
            cur.execute(
                "SELECT match_id, status, outcome, winner, attempt_count FROM matches WHERE match_id != ?",
                (current_match_id,),
            )
            for r in cur.fetchall():
                other_matches[r[0]] = (r[1], r[2], r[3], r[4])
        tournaments: dict[str, str] = {}
        if "tournaments" in tables:
            cur.execute("SELECT tournament_id, config_hash FROM tournaments;")
            for r in cur.fetchall():
                tournaments[r[0]] = r[1]
        curr_status = None
        if "matches" in tables:
            cur.execute(
                "SELECT status, outcome FROM matches WHERE match_id = ?;", (current_match_id,)
            )
            row = cur.fetchone()
            if row:
                curr_status = (row[0], row[1])
        conn.close()
        return {
            "is_sqlite": True,
            "tables": set(tables),
            "other_matches": other_matches,
            "tournaments": tournaments,
            "curr_match_status": curr_status,
        }
    except Exception:
        try:
            return {"is_sqlite": False, "raw_hash": hash_file(db_path)}
        except Exception:
            return None


def _verify_protected_db_state(
    db_path: Path, current_match_id: str, pre_state: dict[str, Any] | None
) -> tuple[bool, str]:
    """Verify protected invariants: allow coordinator heartbeats, reject unauthorized mutations."""
    if pre_state is None:
        return True, "No pre-state recorded"
    if not db_path.exists():
        return False, "Tournament database was deleted during match execution"

    if not pre_state.get("is_sqlite", True):
        post_hash = hash_file(db_path)
        if post_hash != pre_state.get("raw_hash"):
            return False, "Tournament database tampering detected during match execution"
        return True, "Valid"

    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res_chk = cur.fetchone()
        if not res_chk or res_chk[0] != "ok":
            conn.close()
            return (
                False,
                f"Tournament database tampering detected: PRAGMA integrity check failed: {res_chk}",
            )

        # 1. Verify schema tables not dropped
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        post_tables = set(r[0] for r in cur.fetchall())
        pre_tables = pre_state.get("tables", set())
        if not pre_tables.issubset(post_tables):
            missing = pre_tables - post_tables
            conn.close()
            return False, f"Unauthorized schema mutation: tables dropped during match: {missing}"

        # 2. Verify non-current match records for unauthorized tampering
        if "matches" in post_tables:
            cur.execute(
                "SELECT match_id, status, outcome, winner, attempt_count, completed_at FROM matches WHERE match_id != ?",
                (current_match_id,),
            )
            for r in cur.fetchall():
                m_id = r[0]
                post_status, post_outcome, post_winner, post_attempts, post_completed = (
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                )
                expected = pre_state["other_matches"].get(m_id)
                if expected is not None:
                    pre_status, pre_outcome, pre_winner, pre_attempts = expected

                    # Case A: An already COMPLETED match before this match started is strictly immutable
                    if pre_status == "COMPLETED":
                        if (post_status, post_outcome, post_winner) != (
                            pre_status,
                            pre_outcome,
                            pre_winner,
                        ):
                            conn.close()
                            return (
                                False,
                                f"Unauthorized mutation of previously completed match {m_id}: {expected} -> {r[1:5]}",
                            )

                    # Case B: A PENDING match must never have outcome or winner set directly
                    if post_status == "PENDING" and (
                        post_outcome is not None or post_winner is not None
                    ):
                        conn.close()
                        return (
                            False,
                            f"Unauthorized mutation of pending match {m_id}: outcome/winner forged without execution",
                        )

                    # Case C: A COMPLETED match must have completed_at set and attempt_count > 0
                    if post_status == "COMPLETED" and (
                        post_completed is None or post_attempts == 0
                    ):
                        conn.close()
                        return (
                            False,
                            f"Unauthorized forged completion of match {m_id} without valid execution metadata",
                        )

        # 3. Verify tournament configurations untouched
        if "tournaments" in post_tables:
            cur.execute("SELECT tournament_id, config_hash FROM tournaments;")
            for r in cur.fetchall():
                t_id = r[0]
                expected_hash = pre_state["tournaments"].get(t_id)
                if expected_hash is not None and r[1] != expected_hash:
                    conn.close()
                    return (
                        False,
                        f"Unauthorized tournament config mutation for {t_id}: {expected_hash} -> {r[1]}",
                    )

        # 4. Verify current match was not prematurely completed by bot
        if "matches" in post_tables and pre_state.get("curr_match_status") is not None:
            pre_curr_status, _ = pre_state["curr_match_status"]
            if pre_curr_status != "COMPLETED":
                cur.execute(
                    "SELECT status, outcome FROM matches WHERE match_id = ?;", (current_match_id,)
                )
                row = cur.fetchone()
                if row and row[0] == "COMPLETED":
                    conn.close()
                    return (
                        False,
                        f"Unauthorized premature completion of match {current_match_id} in database by bot",
                    )

        conn.close()
        return True, "Valid"
    except Exception as e:
        return False, f"Tournament database tampering detected: unreadable after match: {e}"


class MockAdapter(GameAdapter):
    """Deterministic mock adapter executing real bot code via isolated subprocesses."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def version(self) -> str:
        return "0.2.0"

    def validate_installation(self) -> tuple[bool, str]:
        return True, "Mock engine is pure Python standard library and ready."

    def get_capabilities(self) -> Capability:
        return Capability(
            can_run_local=True,
            can_run_remote=False,
            can_submit=False,
            can_fetch_replays=False,
            can_list_matches=False,
            supported_languages=["python"],
            adapter_version=self.version,
            game_version="mock-v2-isolated",
        )

    def discover_maps(self) -> list[str]:
        return sorted(list(MOCK_MAPS.keys()))

    def validate_bot_compatibility(self, bot_artifact: BotArtifact) -> tuple[bool, str]:
        if bot_artifact.language != "python":
            return False, f"Mock adapter only supports python, got '{bot_artifact.language}'"
        return True, "Compatible"

    def build_or_prepare_artifact(self, source_path: Path, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        if source_path.is_file():
            shutil.copy2(source_path, output_dir / source_path.name)
        elif source_path.is_dir():
            shutil.copytree(source_path, output_dir, dirs_exist_ok=True)
        return {"status": "SUCCESS", "message": "Artifact staged cleanly"}

    def _policy_for_bot(self, bot: BotArtifact) -> str:
        for tag in bot.tags:
            if tag.startswith("policy:"):
                return tag.split(":", 1)[1]
            if tag.startswith("fail:"):
                return tag.split(":", 1)[1]
        name_lower = bot.display_name.lower()
        for p in [
            "fixed",
            "random",
            "resource",
            "aggressive",
            "defensive",
            "crash",
            "timeout",
            "invalid_action",
            "nondeterministic",
        ]:
            if p in name_lower:
                return p
        return "fixed"

    def _locate_artifact_entrypoint(self, bot: BotArtifact) -> Path:
        """Find executable entrypoint strictly via bot.entrypoint_relpath or manifest."""
        snapshot_loc = Path(bot.source_location)
        if snapshot_loc.is_file():
            return snapshot_loc

        # 1. Explicit entrypoint_relpath on bot artifact
        if getattr(bot, "entrypoint_relpath", None):
            p = snapshot_loc / bot.entrypoint_relpath
            if p.is_file():
                return p

        # 2. Check manifest.json inside snapshot directory
        manifest_file = snapshot_loc / "manifest.json"
        if manifest_file.is_file():
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    mdata = json.load(f)
                rel = mdata.get("entrypoint_relpath")
                if rel and (snapshot_loc / rel).is_file():
                    return snapshot_loc / rel
            except Exception:
                pass

        raise FileNotFoundError(
            f"No recorded executable entrypoint found in snapshot: {snapshot_loc}"
        )

    def run_local_match(
        self,
        spec: MatchSpec,
        bot_a: BotArtifact,
        bot_b: BotArtifact,
        work_dir: Path,
        cancel_event: threading.Event | None = None,
    ) -> MatchResult:
        work_dir.mkdir(parents=True, exist_ok=True)

        if spec.map_name not in MOCK_MAPS:
            infra_fc = FailureClassification(
                category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                evidence=f"Map '{spec.map_name}' not found",
            )
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=infra_fc,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        game_map = MOCK_MAPS[spec.map_name]
        engine = MockEngine(game_map=game_map, seed=spec.seed)

        # Locate entrypoints inside immutable snapshot directories (fail-closed if missing)
        try:
            entry_a = self._locate_artifact_entrypoint(bot_a)
            entry_b = self._locate_artifact_entrypoint(bot_b)
        except FileNotFoundError as e:
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=FailureClassification(
                    category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                    culprit="system",
                    evidence=str(e),
                ),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        # Check pre-match integrity of participant artifacts if snapshot manifests exist
        if (Path(bot_a.source_location) / "manifest.json").exists():
            ok_a, msg_a = verify_artifact_integrity(bot_a)
            if not ok_a:
                return MatchResult(
                    match_id=spec.match_id,
                    outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                    failure_classification=FailureClassification(
                        category=FailureCategory.STORAGE_FAILURE,
                        culprit="system",
                        evidence=f"Pre-match Bot A integrity check failed: {msg_a}",
                    ),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
        if (Path(bot_b.source_location) / "manifest.json").exists():
            ok_b, msg_b = verify_artifact_integrity(bot_b)
            if not ok_b:
                return MatchResult(
                    match_id=spec.match_id,
                    outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                    failure_classification=FailureClassification(
                        category=FailureCategory.STORAGE_FAILURE,
                        culprit="system",
                        evidence=f"Pre-match Bot B integrity check failed: {msg_b}",
                    ),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

        # Snapshot persistent laboratory state to detect adversarial tampering
        champ_path = get_champion_manifest_path()
        pre_champ_exists = champ_path.exists()
        pre_champ_hash = hash_file(champ_path) if pre_champ_exists else None

        db_path = get_database_path()
        pre_db_state = _snapshot_protected_db_state(db_path, spec.match_id)

        art_dir = get_artifacts_dir()
        pre_art_manifests: dict[str, str | None] = {}
        if art_dir.exists():
            for d in art_dir.iterdir():
                if d.is_dir():
                    m = d / "manifest.json"
                    pre_art_manifests[d.name] = hash_file(m) if m.exists() else None

        # Prepare subprocess instances with memory limit
        env_a = {"BATTLELAB_BOT_POLICY": self._policy_for_bot(bot_a)}
        env_b = {"BATTLELAB_BOT_POLICY": self._policy_for_bot(bot_b)}
        proc_a = BotSubprocess(
            entrypoint_path=entry_a,
            cwd=entry_a.parent,
            env=env_a,
            memory_limit_mb=spec.memory_limit_mb,
            cancel_event=cancel_event,
        )
        proc_b = BotSubprocess(
            entrypoint_path=entry_b,
            cwd=entry_b.parent,
            env=env_b,
            memory_limit_mb=spec.memory_limit_mb,
            cancel_event=cancel_event,
        )

        # Side assignment
        side_a = spec.side_assignment.get("A", "side_0")
        if side_a == "side_0":
            proc_0, proc_1 = proc_a, proc_b
            bot_0_is_a = True
        else:
            proc_0, proc_1 = proc_b, proc_a
            bot_0_is_a = False

        # Run engine with real subprocesses and explicit limits
        try:
            engine_res = engine.run_match_subprocesses(
                bot_proc_0=proc_0,
                bot_proc_1=proc_1,
                per_turn_limit_ms=spec.per_turn_limit_ms,
                match_wall_clock_limit_ms=spec.match_wall_clock_limit_ms,
                cancel_event=cancel_event,
            )
        except (BotStartupError, Exception) as e:
            proc_a.stop()
            proc_b.stop()
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                failure_classification=FailureClassification(
                    category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                    culprit="system",
                    evidence=f"Bot execution failure: {e}",
                ),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        if engine_res.cancelled or (cancel_event and cancel_event.is_set()):
            return MatchResult(
                match_id=spec.match_id,
                outcome=MatchOutcome.INFRASTRUCTURE_FAILURE,
                duration_ms=engine_res.duration_ms,
                failure_classification=FailureClassification(
                    category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                    culprit="system",
                    evidence="Match cancelled due to lost lease",
                ),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        # Map back to Bot A and Bot B
        score_a = engine_res.score_p0 if bot_0_is_a else engine_res.score_p1
        score_b = engine_res.score_p1 if bot_0_is_a else engine_res.score_p0

        crashed_a = engine_res.crashed_p0 if bot_0_is_a else engine_res.crashed_p1
        crashed_b = engine_res.crashed_p1 if bot_0_is_a else engine_res.crashed_p0
        timed_out_a = engine_res.timed_out_p0 if bot_0_is_a else engine_res.timed_out_p1
        timed_out_b = engine_res.timed_out_p1 if bot_0_is_a else engine_res.timed_out_p0
        invalid_a = engine_res.invalid_action_p0 if bot_0_is_a else engine_res.invalid_action_p1
        invalid_b = engine_res.invalid_action_p1 if bot_0_is_a else engine_res.invalid_action_p0
        protocol_a = (
            engine_res.protocol_violation_p0 if bot_0_is_a else engine_res.protocol_violation_p1
        )
        protocol_b = (
            engine_res.protocol_violation_p1 if bot_0_is_a else engine_res.protocol_violation_p0
        )
        protocol_reason_a = (
            engine_res.protocol_reason_p0 if bot_0_is_a else engine_res.protocol_reason_p1
        )
        protocol_reason_b = (
            engine_res.protocol_reason_p1 if bot_0_is_a else engine_res.protocol_reason_p0
        )

        stderr_a = engine_res.stderr_p0 if bot_0_is_a else engine_res.stderr_p1
        stderr_b = engine_res.stderr_p1 if bot_0_is_a else engine_res.stderr_p0

        winner: str | None = None
        outcome: MatchOutcome

        if engine_res.winner_player_id is not None:
            if (engine_res.winner_player_id == 0 and bot_0_is_a) or (
                engine_res.winner_player_id == 1 and not bot_0_is_a
            ):
                winner = "A"
                outcome = MatchOutcome.WIN_A
            else:
                winner = "B"
                outcome = MatchOutcome.WIN_B
        else:
            outcome = MatchOutcome.DRAW

        # Failure classification
        fc: FailureClassification | None = None
        if crashed_a:
            fc = FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_a",
                evidence=f"Bot A subprocess crashed:\n{stderr_a}",
            )
        elif crashed_b:
            fc = FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_b",
                evidence=f"Bot B subprocess crashed:\n{stderr_b}",
            )
        elif timed_out_a:
            fc = FailureClassification(
                category=FailureCategory.TIMEOUT,
                culprit="bot_a",
                evidence=f"Bot A exceeded turn time limit of {spec.time_limit_ms}ms",
            )
        elif timed_out_b:
            fc = FailureClassification(
                category=FailureCategory.TIMEOUT,
                culprit="bot_b",
                evidence=f"Bot B exceeded turn time limit of {spec.time_limit_ms}ms",
            )
        elif protocol_a:
            base_err = (
                f"Bot A protocol violation: {protocol_reason_a}"
                if protocol_reason_a
                else "Bot A emitted malformed non-JSON data on stdout"
            )
            evidence_a = f"{base_err}\n{stderr_a}".strip() if stderr_a else base_err
            fc = FailureClassification(
                category=FailureCategory.PROTOCOL_VIOLATION,
                culprit="bot_a",
                evidence=evidence_a,
            )
        elif protocol_b:
            base_err = (
                f"Bot B protocol violation: {protocol_reason_b}"
                if protocol_reason_b
                else "Bot B emitted malformed non-JSON data on stdout"
            )
            evidence_b = f"{base_err}\n{stderr_b}".strip() if stderr_b else base_err
            fc = FailureClassification(
                category=FailureCategory.PROTOCOL_VIOLATION,
                culprit="bot_b",
                evidence=evidence_b,
            )
        elif invalid_a:
            fc = FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_a",
                evidence="Bot A submitted illegal action rejected by rules",
            )
        elif invalid_b:
            fc = FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_b",
                evidence="Bot B submitted illegal action rejected by rules",
            )
        elif outcome in (MatchOutcome.WIN_A, MatchOutcome.WIN_B):
            fc = FailureClassification(
                category=FailureCategory.GAMEPLAY_LOSS,
                culprit="bot_b" if winner == "A" else "bot_a",
                evidence=f"Legitimate score defeat ({score_a:.1f} vs {score_b:.1f})",
            )

        # Atomic verification of laboratory state integrity after bot execution
        tamper_detected = False
        tamper_fc: FailureClassification | None = None

        # 1. Champion manifest tampering detection
        post_champ_exists = champ_path.exists()
        post_champ_hash = hash_file(champ_path) if post_champ_exists else None
        if post_champ_exists != pre_champ_exists or post_champ_hash != pre_champ_hash:
            tamper_detected = True
            tamper_fc = FailureClassification(
                category=FailureCategory.STORAGE_FAILURE,
                culprit="bot_a" if not crashed_a else "bot_b",
                evidence="Laboratory champion manifest tampering detected during match execution",
            )

        # 2. Participant artifact integrity detection
        if not tamper_detected and (Path(bot_a.source_location) / "manifest.json").exists():
            post_ok_a, post_msg_a = verify_artifact_integrity(bot_a)
            if not post_ok_a:
                tamper_detected = True
                tamper_fc = FailureClassification(
                    category=FailureCategory.STORAGE_FAILURE,
                    culprit="bot_a",
                    evidence=f"Bot A corrupted its artifact snapshot during execution: {post_msg_a}",
                )
        if not tamper_detected and (Path(bot_b.source_location) / "manifest.json").exists():
            post_ok_b, post_msg_b = verify_artifact_integrity(bot_b)
            if not post_ok_b:
                tamper_detected = True
                tamper_fc = FailureClassification(
                    category=FailureCategory.STORAGE_FAILURE,
                    culprit="bot_b",
                    evidence=f"Bot B corrupted its artifact snapshot during execution: {post_msg_b}",
                )

        # 3. Cross-artifact tampering detection
        if not tamper_detected and art_dir.exists():
            for d in art_dir.iterdir():
                if d.is_dir() and d.name not in (bot_a.artifact_id, bot_b.artifact_id):
                    m = d / "manifest.json"
                    post_hash = hash_file(m) if m.exists() else None
                    if post_hash != pre_art_manifests.get(d.name):
                        tamper_detected = True
                        tamper_fc = FailureClassification(
                            category=FailureCategory.STORAGE_FAILURE,
                            culprit="system",
                            evidence=f"Cross-artifact tampering detected in artifact: {d.name}",
                        )
                        break

        # 4. Database tampering detection
        if not tamper_detected and pre_db_state is not None:
            valid_db, db_tamper_reason = _verify_protected_db_state(
                db_path, spec.match_id, pre_db_state
            )
            if not valid_db:
                tamper_detected = True
                tamper_fc = FailureClassification(
                    category=FailureCategory.STORAGE_FAILURE,
                    culprit="system",
                    evidence=db_tamper_reason,
                )

        if tamper_detected and tamper_fc is not None:
            outcome = MatchOutcome.INFRASTRUCTURE_FAILURE
            winner = None
            fc = tamper_fc

        # Write replay
        replay_filename = f"replay_{spec.match_id}.jsonl"
        replay_path = work_dir / replay_filename

        has_fail_missing = (
            "fail:missing_replay" in bot_a.tags or "fail:missing_replay" in bot_b.tags
        )
        has_fail_corrupt = (
            "fail:corrupted_replay" in bot_a.tags or "fail:corrupted_replay" in bot_b.tags
        )

        replay_hash: str | None = None
        if not has_fail_missing:
            with open(replay_path, "w", encoding="utf-8") as f:
                if has_fail_corrupt:
                    f.write("CORRUPTED_NON_JSON_DATA_!!!\n")
                else:
                    meta = {
                        "type": "META",
                        "match_id": spec.match_id,
                        "map": spec.map_name,
                        "seed": spec.seed,
                        "bot_a": bot_a.artifact_id,
                        "bot_b": bot_b.artifact_id,
                    }
                    f.write(json.dumps(meta) + "\n")
                    for frame in engine_res.replay_frames:
                        f.write(json.dumps(frame) + "\n")
            replay_hash = hash_file(replay_path)
            replay_path_str = str(replay_path)
        else:
            replay_path_str = None

        return MatchResult(
            match_id=spec.match_id,
            outcome=outcome,
            winner=winner,
            score_a=score_a,
            score_b=score_b,
            duration_ms=engine_res.duration_ms,
            turns_played=engine_res.turns_played,
            bot_a_stats={"score": score_a, **proc_a.get_runtime_statistics(spec.per_turn_limit_ms)},
            bot_b_stats={"score": score_b, **proc_b.get_runtime_statistics(spec.per_turn_limit_ms)},
            crashed_a=crashed_a,
            crashed_b=crashed_b,
            timed_out_a=timed_out_a,
            timed_out_b=timed_out_b,
            invalid_action_a=invalid_a,
            invalid_action_b=invalid_b,
            protocol_violation_a=protocol_a,
            protocol_violation_b=protocol_b,
            replay_path=replay_path_str,
            replay_hash=replay_hash,
            adapter_metadata={"adapter": self.name, "version": self.version},
            failure_classification=fc,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    def parse_replay(self, replay_path: Path) -> Any:
        if not replay_path.is_file():
            raise FileNotFoundError(f"Replay file not found: {replay_path}")

        frames: list[dict[str, Any]] = []
        with open(replay_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    frames.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"Corrupted replay JSON on line {line_no}: {e}") from e

        if not frames:
            raise ValueError("Replay file is empty")

        meta = frames[0] if frames[0].get("type") == "META" else {}
        final_frame = frames[-1] if len(frames) > 1 else None

        from battlelab.official.models import NormalizedReplay

        outcome_str = ""
        scores_dict: dict[str, float] = {}
        if final_frame and isinstance(final_frame, dict):
            outcome_str = str(final_frame.get("outcome", ""))
            scores_dict = {
                "A": float(final_frame.get("score_a", 0.0)),
                "B": float(final_frame.get("score_b", 0.0)),
            }

        return NormalizedReplay(
            schema_version="1.0.0",
            adapter_name=self.name,
            adapter_version=self.version,
            game_version="mock-v2-isolated",
            map_id=str(meta.get("map_name", "")),
            seed=int(meta.get("seed", 0)),
            participants={
                "A": str(meta.get("bot_a_id", "")),
                "B": str(meta.get("bot_b_id", "")),
            },
            outcome=outcome_str,
            scores=scores_dict,
            turn_count=max(0, len(frames) - 1),
            events=frames[1:],
            raw_replay_hash=hash_file(replay_path),
            source_metadata={"meta": meta, "final_frame": final_frame},
        )
