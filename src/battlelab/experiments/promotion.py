"""Controlled champion promotion gates, multi-seed determinism, and audited rollback."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.adapters import get_adapter
from battlelab.bots.registry import BotRegistry
from battlelab.config.loader import load_yaml_config
from battlelab.core.errors import PromotionGateError
from battlelab.core.models import Experiment, MatchSpec
from battlelab.storage.database import Database
from battlelab.storage.paths import get_data_dir

REQUIRED_OVERRIDE_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_STATISTICAL_RISK"


class PromotionGate:
    """Enforces rigorous statistical, integrity, and reliability requirements before champion promotion."""

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
        min_win_rate = cfg.get("min_win_rate", 0.50)
        max_crash = cfg.get("max_crash_rate", 0.0)
        max_timeout = cfg.get("max_timeout_rate", 0.0)
        max_invalid = cfg.get("max_invalid_action_rate", 0.0)
        require_pos_lower_ci = cfg.get("require_positive_lower_ci", True)
        min_paired_delta = cfg.get("min_paired_mean_delta", 0.0)
        min_weighted_win_delta = cfg.get("min_weighted_win_delta", 0.0)
        min_paired_win_delta_lower_bound = cfg.get("min_paired_win_delta_lower_bound", 0.0)
        min_runtime_headroom = cfg.get("min_runtime_headroom", 0.10)
        max_group_regression_delta = cfg.get("max_group_regression_delta", -0.15)
        max_map_regression_delta = cfg.get("max_map_regression_delta", -0.20)
        max_side_regression_delta = cfg.get("max_side_regression_delta", -0.25)
        check_det = cfg.get("require_determinism_pass", True)

        agg = metrics.get("aggregate", {})
        total_scheduled = agg.get("total_scheduled", 0)
        valid_matches = agg.get("valid_matches", 0)
        infra_failures = agg.get("infrastructure_failures", 0)
        win_rate = agg.get("win_rate", 0.0)
        crash_rate = agg.get("crash_rate", 0.0)
        timeout_rate = agg.get("timeout_rate", 0.0)
        invalid_rate = agg.get("invalid_action_rate", 0.0)
        headroom = agg.get("runtime_headroom", 1.0)

        paired = metrics.get("paired_analysis", {})
        mean_score_delta = paired.get("mean_score_delta", 0.0)
        weighted_win_delta = paired.get(
            "weighted_mean_win_delta", paired.get("mean_win_rate_delta", 0.0)
        )
        score_ci_lower = paired.get("score_delta_bootstrap_ci_95", [-1.0, 1.0])[0]
        win_ci_lower = paired.get("win_delta_bootstrap_ci_95", [-1.0, 1.0])[0]

        violations: list[str] = []

        # 1. Unresolved infrastructure failures
        if infra_failures > 0:
            violations.append(
                f"Evaluation contains {infra_failures} unresolved infrastructure failures."
            )

        # 2. Sample size
        if valid_matches < min_sample:
            violations.append(
                f"Valid sample size {valid_matches} is below minimum requirement of {min_sample} matches."
            )

        # 3. Overall Win Rate threshold
        if win_rate < min_win_rate:
            violations.append(f"Win rate {win_rate:.2%} is below threshold of {min_win_rate:.2%}.")

        # 4. Paired mean improvement
        if mean_score_delta < min_paired_delta:
            violations.append(
                f"Mean score delta {mean_score_delta:+.2f} is below minimum threshold of {min_paired_delta:+.2f}."
            )

        if weighted_win_delta < min_weighted_win_delta:
            violations.append(
                f"Weighted win delta {weighted_win_delta:+.2%} is below minimum threshold of {min_weighted_win_delta:+.2%}."
            )

        # 5. Statistical confidence bound
        if require_pos_lower_ci:
            if win_ci_lower <= min_paired_win_delta_lower_bound:
                violations.append(
                    f"Paired win delta 95% CI lower bound {win_ci_lower:+.4f} must be positive (> {min_paired_win_delta_lower_bound}) for promotion."
                )
            if score_ci_lower <= 0.0:
                violations.append(
                    f"Paired score delta 95% CI lower bound {score_ci_lower:+.2f} must be positive (> 0.0) for promotion."
                )

        # 6. Runtime headroom
        if headroom < min_runtime_headroom:
            violations.append(
                f"Runtime headroom {headroom:.1%} is below minimum requirement of {min_runtime_headroom:.1%}."
            )

        # 7. Reliability constraints
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

        # 8. Opponent group regressions
        by_grp = paired.get("by_opponent_group", {})
        for grp_name, grp_stats in by_grp.items():
            grp_win_d = grp_stats.get("mean_win_diff", 0.0)
            if grp_win_d < max_group_regression_delta:
                violations.append(
                    f"Opponent group '{grp_name}' paired win delta {grp_win_d:+.2%} regressed beyond tolerance ({max_group_regression_delta:+.2%})."
                )

        # 9. Map regressions
        by_map = paired.get("by_map", {})
        for map_name, map_stats in by_map.items():
            m_win_d = map_stats.get("mean_win_diff", 0.0)
            if m_win_d < max_map_regression_delta:
                violations.append(
                    f"Map '{map_name}' paired win delta {m_win_d:+.2%} regressed beyond tolerance ({max_map_regression_delta:+.2%})."
                )

        # 10. Side regressions
        by_side = paired.get("by_side", {})
        for side_name, side_stats in by_side.items():
            s_win_d = side_stats.get("mean_win_diff", 0.0)
            if s_win_d < max_side_regression_delta:
                violations.append(
                    f"Side '{side_name}' paired win delta {s_win_d:+.2%} regressed beyond tolerance ({max_side_regression_delta:+.2%})."
                )

        # 11. Multi-seed determinism check
        if check_det:
            det_ok, det_err = self._verify_multi_seed_determinism(exp.challenger_artifact_id)
            if not det_ok:
                violations.append(f"Determinism verification failed: {det_err}")

        passed = len(violations) == 0
        return {
            "passed": passed,
            "violations": violations,
            "config_applied": cfg,
            "metrics_evaluated": {
                "total_scheduled": total_scheduled,
                "valid_matches": valid_matches,
                "infra_failures": infra_failures,
                "win_rate": win_rate,
                "mean_score_delta": mean_score_delta,
                "weighted_win_delta": weighted_win_delta,
                "paired_ci_lower": score_ci_lower,
                "win_ci_lower": win_ci_lower,
                "runtime_headroom": headroom,
                "crash_rate": crash_rate,
                "timeout_rate": timeout_rate,
                "invalid_action_rate": invalid_rate,
            },
        }

    def _verify_multi_seed_determinism(self, artifact_id: str) -> tuple[bool, str]:
        """Perform multi-seed, side-swapped repeated determinism check with normalized frames."""
        try:
            bot = self.registry.get_artifact(artifact_id)
            adapter = get_adapter("mock")
            seeds = [101, 202, 303]
            sides = [{"A": "side_0", "B": "side_1"}, {"A": "side_1", "B": "side_0"}]

            for seed in seeds:
                for side_idx, side in enumerate(sides):
                    spec = MatchSpec(
                        match_id=f"det_check_{seed}_s{side_idx}",
                        adapter_name="mock",
                        adapter_version=adapter.version,
                        bot_a_id=artifact_id,
                        bot_b_id=artifact_id,
                        map_name="grid_classic_8x8",
                        seed=seed,
                        side_assignment=side,
                    )
                    work_dir1 = get_data_dir() / "scratch" / f"det_{seed}_s{side_idx}_1"
                    work_dir2 = get_data_dir() / "scratch" / f"det_{seed}_s{side_idx}_2"

                    res1 = adapter.run_local_match(spec, bot, bot, work_dir1)
                    res2 = adapter.run_local_match(spec, bot, bot, work_dir2)

                    if (
                        res1.outcome != res2.outcome
                        or res1.score_a != res2.score_a
                        or res1.score_b != res2.score_b
                    ):
                        return (
                            False,
                            f"Outcome discrepancy on seed {seed} (side {side_idx}): {res1.outcome} vs {res2.outcome}",
                        )

                    # Normalize and compare frame events excluding volatile timings
                    if res1.replay_path and res2.replay_path:
                        frames1 = self._load_normalized_frames(Path(res1.replay_path))
                        frames2 = self._load_normalized_frames(Path(res2.replay_path))
                        if len(frames1) != len(frames2):
                            return (
                                False,
                                f"Frame count mismatch on seed {seed}: {len(frames1)} vs {len(frames2)}",
                            )

                        for idx, (f1, f2) in enumerate(zip(frames1, frames2)):
                            if f1 != f2:
                                return (
                                    False,
                                    f"Frame divergence discrepancy on seed {seed} at frame {idx}: {f1} != {f2}",
                                )

            return True, "Multi-seed determinism verified"
        except Exception as e:
            return False, f"Determinism check error: {e}"

    def _load_normalized_frames(self, replay_path: Path) -> list[dict[str, Any]]:
        """Extract gameplay frames stripping volatile timing fields."""
        frames: list[dict[str, Any]] = []
        if not replay_path.is_file():
            return frames
        with open(replay_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # Exclude META and volatile fields
                    if obj.get("type") == "META":
                        continue
                    norm = {
                        "turn": obj.get("turn"),
                        "event": obj.get("event"),
                        "data": obj.get("data"),
                        "scores": obj.get("scores"),
                        "p0_pos": obj.get("p0_pos"),
                        "p1_pos": obj.get("p1_pos"),
                    }
                    frames.append(norm)
                except Exception:
                    pass
        return frames

    def _verify_artifact_integrity(self, artifact_id: str) -> tuple[bool, str]:
        """Verify that files in the immutable snapshot have not been tampered with."""
        from battlelab.bots.artifacts import verify_artifact_integrity

        try:
            bot = self.registry.get_artifact(artifact_id)
            return verify_artifact_integrity(bot)
        except Exception as e:
            return False, str(e)

    def promote(
        self,
        experiment_id: str,
        dry_run: bool = False,
        actor: str = "",
        override_reason: str | None = None,
        acknowledge_risk: str | None = None,
    ) -> dict[str, Any]:
        """Promote experiment challenger to champion status with strict safety gates."""
        exp = self.db.get_experiment(experiment_id)
        if not exp:
            raise KeyError(f"Experiment not found: {experiment_id}")

        if exp.status != "COMPLETED":
            raise PromotionGateError(
                f"Experiment {experiment_id} status is '{exp.status}' (must be COMPLETED)."
            )

        if not exp.results_summary:
            raise PromotionGateError(
                f"Experiment {experiment_id} has no completed results summary."
            )

        # 1. Verify Artifact Integrity
        ok_chal, err_chal = self._verify_artifact_integrity(exp.challenger_artifact_id)
        if not ok_chal:
            raise PromotionGateError(f"Challenger integrity check failed: {err_chal}")

        # 2. Check Criteria
        check_res = self.check_criteria(exp, exp.results_summary)
        is_override = False

        if not check_res["passed"]:
            actor_clean = (actor or "").strip().lower()
            disallowed_actors = {"human", "unknown", "system", "admin", "root", ""}
            banned_substrings = ["bot", "agent", "ai", "copilot", "auto", "model"]
            is_actor_valid = (
                bool(actor_clean)
                and actor_clean not in disallowed_actors
                and not any(b in actor_clean for b in banned_substrings)
            )
            is_reason_valid = bool(override_reason and len(override_reason.strip()) >= 15)
            is_ack_valid = acknowledge_risk == REQUIRED_OVERRIDE_ACKNOWLEDGEMENT

            if is_actor_valid and is_reason_valid and is_ack_valid:
                is_override = True
            else:
                exp.promotion_decision = "REJECTED"
                exp.rejection_reason = "; ".join(check_res["violations"])
                self.db.save_experiment(exp)
                raise PromotionGateError(
                    f"Candidate artifact {exp.challenger_artifact_id} failed promotion gates: "
                    f"{'; '.join(check_res['violations'])}. "
                    "To override, an identifiable individual human actor, reason (>= 15 chars), "
                    f"and explicit acknowledgement (--acknowledge-risk {REQUIRED_OVERRIDE_ACKNOWLEDGEMENT}) are required.",
                    violations=check_res["violations"],
                )

        if dry_run:
            return {
                "dry_run": True,
                "passed": check_res["passed"],
                "violations": check_res["violations"],
                "would_promote": exp.challenger_artifact_id,
                "current_champion": getattr(
                    self.registry.get_champion_artifact(), "artifact_id", None
                ),
                "is_override": is_override,
            }

        now_iso = datetime.now(timezone.utc).isoformat()
        current_champ = self.registry.get_champion_artifact()
        previous_champion_id = current_champ.artifact_id if current_champ else None

        reason = (
            f"Promoted via experiment {experiment_id}: {exp.hypothesis}"
            if not is_override
            else f"OVERRIDE PROMOTION by {actor}: {override_reason}"
        )

        manifest = self.registry.update_champion_manifest(
            artifact_id=exp.challenger_artifact_id,
            experiment_id=experiment_id,
            updated_at=now_iso,
            reason=reason,
            previous_champion_id=previous_champion_id,
        )

        # Collision-resistant promotion ID
        promotion_id = f"prom_{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:8]}"
        mode = "MANUAL_OVERRIDE" if is_override else "MANUAL_VERIFIED"
        challenger_art = self.registry.get_artifact(exp.challenger_artifact_id)

        self.db.save_promotion(
            promotion_id=promotion_id,
            experiment_id=experiment_id,
            artifact_id=exp.challenger_artifact_id,
            promoted_at=now_iso,
            manifest_snapshot=manifest,
            reason=reason,
            mode=mode,
            promoted_by=actor if actor else "evaluator",
            override_acknowledgement=acknowledge_risk if is_override else None,
            previous_champion_id=previous_champion_id,
            gate_violations_json=json.dumps(check_res["violations"])
            if check_res["violations"]
            else None,
            artifact_manifest_hash=challenger_art.manifest_hash if challenger_art else None,
            config_hash=getattr(exp, "config_hash", None),
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
            "mode": mode,
            "promoted_by": actor,
        }

    def rollback(
        self, historical_artifact_id: str, reason: str = "", actor: str = "human"
    ) -> dict[str, Any]:
        """Roll back champion to a historical artifact with full audit record."""
        art = self.registry.get_artifact(historical_artifact_id)
        from battlelab.bots.artifacts import verify_artifact_integrity

        ok, err = verify_artifact_integrity(art)
        if not ok:
            raise PromotionGateError(f"Historical artifact integrity verification failed: {err}")

        current_champ = self.registry.get_champion_artifact()
        previous_champion_id = current_champ.artifact_id if current_champ else None

        now_iso = datetime.now(timezone.utc).isoformat()
        rollback_reason = f"Rollback: {reason}" if reason else "Manual rollback"

        manifest = self.registry.update_champion_manifest(
            artifact_id=art.artifact_id,
            experiment_id="rollback",
            updated_at=now_iso,
            reason=rollback_reason,
            previous_champion_id=previous_champion_id,
        )

        promotion_id = (
            f"prom_rb_{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:8]}"
        )
        self.db.save_promotion(
            promotion_id=promotion_id,
            experiment_id=None,
            artifact_id=art.artifact_id,
            promoted_at=now_iso,
            manifest_snapshot=manifest,
            reason=rollback_reason,
            mode="ROLLBACK",
            promoted_by=actor,
            previous_champion_id=previous_champion_id,
            artifact_manifest_hash=art.manifest_hash,
        )

        return {
            "promotion_id": promotion_id,
            "status": "ROLLED_BACK",
            "champion_artifact_id": art.artifact_id,
            "updated_at": now_iso,
            "reason": rollback_reason,
            "mode": "ROLLBACK",
            "promoted_by": actor,
        }
