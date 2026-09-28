"""Experiment evaluator orchestrating paired matches, metrics, and report generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.analysis.feedback_packet import generate_feedback_packet
from battlelab.analysis.metrics import calculate_paired_experiment_metrics
from battlelab.analysis.report import generate_experiment_report
from battlelab.bots.registry import BotRegistry
from battlelab.config.loader import load_yaml_config
from battlelab.core.models import ResolvedOpponent
from battlelab.experiments.registry import ExperimentRegistry
from battlelab.matches.matrix import generate_paired_experiment_matrix
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.storage.database import Database
from battlelab.storage.paths import get_reports_dir


class ExperimentEvaluator:
    """Executes experiments and produces comprehensive human and AI reports."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.registry = ExperimentRegistry(self.db)
        self.bot_registry = BotRegistry(self.db)
        self.scheduler = TournamentScheduler(self.db)

    def _ensure_opponent_artifacts(self, opp_cfg: dict[str, Any]) -> list[ResolvedOpponent]:
        """Ensure all opponents from pool are registered as immutable artifacts."""
        resolved: list[ResolvedOpponent] = []
        for opp in opp_cfg.get("opponents", []):
            path_str = opp.get("path")
            if not path_str:
                continue
            src_path = Path(path_str)
            if not src_path.exists():
                raise FileNotFoundError(f"Opponent source path missing: {src_path}")
            art = self.bot_registry.register_bot(
                source_path=src_path,
                display_name=opp.get("name", opp.get("id")),
                tags=opp.get("tags", []),
                entrypoint=opp.get("entrypoint", ""),
            )
            resolved.append(
                ResolvedOpponent(
                    config_id=opp.get("id", art.artifact_id),
                    artifact_id=art.artifact_id,
                    name=opp.get("name", opp.get("id")),
                    group=opp.get("group", "general"),
                    weight=float(opp.get("weight", 1.0)),
                    tags=opp.get("tags", []),
                )
            )
        return resolved

    def run_experiment(
        self,
        experiment_id: str,
        eval_config_path: Path | str | None = None,
        opponent_pool_path: Path | str | None = None,
        max_workers: int | None = None,
    ) -> dict[str, Any]:
        """Execute paired evaluation matrix for an experiment against opponent pool."""
        exp = self.registry.get_experiment(experiment_id)
        exp.status = "RUNNING"
        self.registry.update_experiment(exp)

        try:
            # Load evaluation configuration
            cfg_path = eval_config_path or "configs/evaluation.yaml"
            cfg = load_yaml_config(cfg_path)
            workers = max_workers or cfg.get("max_workers", 4)
            self.scheduler.max_workers = workers

            adapter_name = cfg.get("adapter", "mock")
            maps = cfg.get("maps", ["grid_classic_8x8"])
            seeds = cfg.get("seeds", [42, 137])
            paired_sides = cfg.get("paired_sides", True)
            repetitions = cfg.get("repetitions", 1)
            time_limit_ms = cfg.get("time_limit_ms", 5000)

            # Load opponent pool
            opp_path = opponent_pool_path or "configs/opponent_pool.yaml"
            opp_cfg = load_yaml_config(opp_path)
            resolved_opponents = self._ensure_opponent_artifacts(opp_cfg)

            # Fallback to direct head-to-head if pool is empty
            if not resolved_opponents:
                resolved_opponents = [
                    ResolvedOpponent(
                        config_id="baseline",
                        artifact_id=exp.baseline_artifact_id,
                        name="Baseline",
                        group="baseline",
                        weight=1.0,
                    )
                ]

            # Generate paired matches: both Baseline and Challenger play identical conditions
            specs = generate_paired_experiment_matrix(
                challenger_id=exp.challenger_artifact_id,
                baseline_id=exp.baseline_artifact_id,
                opponents=resolved_opponents,
                maps=maps,
                seeds=seeds,
                adapter_name=adapter_name,
                paired_sides=paired_sides,
                repetitions=repetitions,
                time_limit_ms=time_limit_ms,
                tournament_id=f"trn_{exp.experiment_id}",
                experiment_id=exp.experiment_id,
                include_direct=True,
                per_turn_limit_ms=cfg.get("per_turn_limit_ms", time_limit_ms),
                match_wall_clock_limit_ms=cfg.get("match_wall_clock_limit_ms", 60000),
                memory_limit_mb=cfg.get("memory_limit_mb", 512),
            )

            tournament_id = f"trn_{exp.experiment_id}"
            self.scheduler.create_tournament(
                tournament_id=tournament_id,
                name=f"Tournament for {experiment_id}",
                specs=specs,
                config=cfg,
            )

            # Run matches
            self.scheduler.run_tournament(tournament_id)

            # Retrieve all matches for this experiment
            matches = self.db.list_matches_by_experiment(experiment_id)

            # Calculate paired metrics
            metrics = calculate_paired_experiment_metrics(
                matches=matches,
                challenger_id=exp.challenger_artifact_id,
                baseline_id=exp.baseline_artifact_id,
                resolved_opponents=resolved_opponents,
                opponent_pool_config=opp_cfg,
            )

            # Collect representative replays
            representative_replays: list[str] = []
            for m in matches:
                if m.get("replay_hash") and m["replay_hash"] not in representative_replays:
                    representative_replays.append(m["replay_hash"])
                    if len(representative_replays) >= 3:
                        break
            exp.representative_replays = representative_replays

            # Persist COMPLETED experiment status BEFORE gate check and report generation
            now_iso = datetime.now(timezone.utc).isoformat()
            exp.status = "COMPLETED"
            exp.completed_at = now_iso
            exp.results_summary = metrics
            self.registry.update_experiment(exp)

            # Prepare reports output dir
            report_dir = get_reports_dir() / experiment_id
            report_dir.mkdir(parents=True, exist_ok=True)

            # Late import to prevent circular dependency
            from battlelab.experiments.promotion import PromotionGate

            gate = PromotionGate(self.db)
            gate_res = gate.check_criteria(exp, metrics)

            report_md = generate_experiment_report(exp, metrics, gate_res)
            report_md_path = report_dir / "report.md"
            report_md_path.write_text(report_md, encoding="utf-8")

            failed_matches = [
                m
                for m in matches
                if m.get("outcome") == "INFRASTRUCTURE_FAILURE"
                or m.get("crashed_a")
                or m.get("invalid_action_a")
            ]
            feedback_packet = generate_feedback_packet(exp, metrics, gate_res, failed_matches)
            packet_json_path = report_dir / "analysis_packet.json"
            with open(packet_json_path, "w", encoding="utf-8") as f:
                json.dump(feedback_packet, f, indent=2)

            return {
                "experiment_id": experiment_id,
                "status": "COMPLETED",
                "matches_run": len(matches),
                "report_path": str(report_md_path),
                "packet_path": str(packet_json_path),
                "gate_results": gate_res,
            }
        except Exception:
            exp.status = "FAILED"
            self.registry.update_experiment(exp)
            raise
