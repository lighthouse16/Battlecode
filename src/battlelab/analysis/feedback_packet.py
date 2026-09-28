"""Machine-readable feedback packet generator for AI coding agents."""

from __future__ import annotations

import json
from typing import Any
from battlelab.core.models import Experiment


def generate_feedback_packet(
    exp: Experiment,
    metrics: dict[str, Any],
    gate_results: dict[str, Any],
    failed_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct structured JSON feedback packet tailored for AI coding loop."""
    agg = metrics.get("aggregate", {})
    paired = metrics.get("paired_analysis", {})

    packet = {
        "schema_version": "1.0.0",
        "experiment_id": exp.experiment_id,
        "metadata": {
            "hypothesis": exp.hypothesis,
            "intended_change": exp.intended_change,
            "baseline_artifact_id": exp.baseline_artifact_id,
            "challenger_artifact_id": exp.challenger_artifact_id,
            "created_at": exp.created_at,
            "status": exp.status,
            "promotion_decision": exp.promotion_decision,
        },
        "aggregate_metrics": agg,
        "paired_analysis": paired,
        "promotion_gates": gate_results,
        "failed_matches": failed_matches or [],
        "regression_candidates": paired.get("worst_regressions", []),
        "ai_actionable_insights": {
            "can_promote": gate_results.get("passed", False),
            "primary_blocking_issue": gate_results.get("violations", [None])[0] if not gate_results.get("passed") else None,
            "suggested_focus_area": "Fix invalid actions or crashes"
            if agg.get("crash_count", 0) > 0 or agg.get("invalid_action_count", 0) > 0
            else "Optimize opening claims on sparse maps"
            if paired.get("worst_regressions")
            else "Broaden evaluation matrix to more seeds",
        },
    }
    return packet
