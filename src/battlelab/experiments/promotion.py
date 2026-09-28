"""Controlled champion promotion gates and rollback mechanisms."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.adapters import get_adapter
from battlelab.bots.registry import BotRegistry
from battlelab.config.loader import load_yaml_config
from battlelab.core.errors import PromotionGateError
from battlelab.core.identifiers import generate_match_id
from battlelab.core.models import Experiment, MatchSpec
from battlelab.storage.database import Database
from battlelab.storage.paths import get_data_dir


class PromotionGate:
    """Enforces rigorous statistical and reliability requirements before champion promotion."""

    def __init__(self, db: Database | None = None, config_path: Path | str | None = None) -> None:
        self.db = db or Database()
        self.registry = BotRegistry(self.db)
        self.config_path = config_path or "configs/promotion.yaml"

    def check_criteria(
        self,
        exp: Experiment,
        metrics: dict[str, Any],
        config_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Check whether experiment results satisfy promotion criteria."""
        cfg = config_override or load_yaml_config(self.config_path)

        min_sample = cfg.get("min_sample_size", 6)
        min_win_rate = cfg.get("min_win_rate", 0.55)
        max_crash = cfg.get("max_crash_rate", 0.0)
        max_timeout = cfg.get("max_timeout_rate", 0.0)
        max_invalid = cfg.get("max_invalid_action_rate", 0.0)
        check_det = cfg.get("require_determinism_pass", True)

        agg = metrics.get("aggregate", {})
        total_matches = agg.get("total_matches", 0)
        win_rate = agg.get("win_rate", 0.0)
        crash_rate = agg.get("crash_rate", 0.0)
        timeout_rate = agg.get("timeout_rate", 0.0)
        invalid_rate = agg.get("invalid_action_rate", 0.0)

        violations: list[str] = []

        if total_matches < min_sample:
            violations.append(
                f"Sample size {total_matches} is below minimum requirement of {min_sample} matches."
            )

        if win_rate < min_win_rate:
            violations.append(
                f"Win rate {win_rate:.2%} is below threshold of {min_win_rate:.2%}."
            )

        if crash_rate > max_crash:
            violations.append(
                f"Crash rate {crash_rate:.2%} exceeds maximum allowable {max_crash:.2%}."
            )

        if timeout_rate > max_timeout:
            violations.append(
                f"Timeout rate {timeout_rate:.2%} exceeds maximum allowable {max_timeout:.2%}."
            )

        if invalid_rate > max_invalid:
            violations.append(
                f"Invalid action rate {invalid_rate:.2%} exceeds maximum allowable {max_invalid:.2%}."
            )

        # Segment regressions check
        max_tol = cfg.get("max_map_regression_tolerance", 0.25)
        by_map = agg.get("by_map", {})
        for map_name, map_stats in by_map.items():
            tot = map_stats.get("total", 0)
            if tot > 0:
                m_wr = map_stats.get("wins", 0) / tot
                if m_wr < (min_win_rate - max_tol):
                    violations.append(
                        f"Map '{map_name}' win rate {m_wr:.2%} regressed beyond tolerance ({min_win_rate - max_tol:.2%})."
                    )

        # Determinism check
        if check_det:
            det_ok, det_err = self._verify_bot_determinism(exp.challenger_artifact_id)
            if not det_ok:
                violations.append(f"Determinism verification failed: {det_err}")

        passed = len(violations) == 0
        return {
            "passed": passed,
            "violations": violations,
            "config_applied": cfg,
            "metrics_evaluated": {
                "total_matches": total_matches,
                "win_rate": win_rate,
                "crash_rate": crash_rate,
                "timeout_rate": timeout_rate,
                "invalid_action_rate": invalid_rate,
            },
        }

    def _verify_bot_determinism(self, artifact_id: str) -> tuple[bool, str]:
        """Run identical match twice to verify reproducibility."""
        try:
            bot = self.registry.get_artifact(artifact_id)
            adapter = get_adapter("mock")
            spec = MatchSpec(
                match_id="det_check",
                adapter_name="mock",
                adapter_version=adapter.version,
                bot_a_id=artifact_id,
                bot_b_id=artifact_id,
                map_name="grid_classic_8x8",
                seed=77777,
            )
            work_dir1 = get_data_dir() / "scratch" / "det1"
            work_dir2 = get_data_dir() / "scratch" / "det2"
            res1 = adapter.run_local_match(spec, bot, bot, work_dir1)
            res2 = adapter.run_local_match(spec, bot, bot, work_dir2)
            if res1.replay_hash != res2.replay_hash or res1.outcome != res2.outcome:
                return False, f"Replay hash mismatch under identical seed ({res1.replay_hash} vs {res2.replay_hash})"
            return True, "Determinism verified"
        except Exception as e:
            return False, f"Determinism test encountered error: {e}"

    def promote(
        self,
        experiment_id: str,
        dry_run: bool = False,
        force: bool = False,
        promoted_by: str = "ai_agent",
    ) -> dict[str, Any]:
        """Promote experiment challenger to champion status."""
        exp = self.db.get_experiment(experiment_id)
        if not exp:
            raise KeyError(f"Experiment not found: {experiment_id}")

        if not exp.results_summary:
            raise PromotionGateError(f"Experiment {experiment_id} has no completed results summary.")

        check_res = self.check_criteria(exp, exp.results_summary)
        if not check_res["passed"] and not force:
            exp.promotion_decision = "REJECTED"
            exp.rejection_reason = "; ".join(check_res["violations"])
            self.db.save_experiment(exp)
            raise PromotionGateError(
                f"Candidate artifact {exp.challenger_artifact_id} failed promotion gates.",
                violations=check_res["violations"],
            )

        if dry_run:
            return {
                "dry_run": True,
                "passed": check_res["passed"],
                "violations": check_res["violations"],
                "would_promote": exp.challenger_artifact_id,
                "current_champion": getattr(self.registry.get_champion_artifact(), "artifact_id", None),
            }

        now_iso = datetime.now(timezone.utc).isoformat()
        reason = f"Promoted via experiment {experiment_id}: {exp.hypothesis}"

        manifest = self.registry.update_champion_manifest(
            artifact_id=exp.challenger_artifact_id,
            experiment_id=experiment_id,
            updated_at=now_iso,
            reason=reason,
        )

        promotion_id = f"prom_{int(datetime.now(timezone.utc).timestamp())}"
        self.db.save_promotion(
            promotion_id=promotion_id,
            experiment_id=experiment_id,
            artifact_id=exp.challenger_artifact_id,
            promoted_at=now_iso,
            manifest_snapshot=manifest,
            reason=reason,
            mode="MANUAL" if force else "AUTOMATIC",
        )

        exp.promotion_decision = "PROMOTED"
        exp.rejection_reason = None
        self.db.save_experiment(exp)

        return {
            "promotion_id": promotion_id,
            "status": "PROMOTED",
            "champion_artifact_id": exp.challenger_artifact_id,
            "experiment_id": experiment_id,
            "promoted_at": now_iso,
        }

    def rollback(self, historical_artifact_id: str, reason: str = "") -> dict[str, Any]:
        """Roll back champion to a historical artifact."""
        # Ensure artifact exists
        art = self.registry.get_artifact(historical_artifact_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        manifest = self.registry.update_champion_manifest(
            artifact_id=art.artifact_id,
            experiment_id="rollback",
            updated_at=now_iso,
            reason=f"Rollback: {reason}",
        )
        return {
            "status": "ROLLED_BACK",
            "champion_artifact_id": art.artifact_id,
            "updated_at": now_iso,
            "reason": reason,
        }
