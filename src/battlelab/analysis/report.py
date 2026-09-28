"""Markdown report generator for experiments."""

from __future__ import annotations

from typing import Any

from battlelab.core.models import Experiment


def generate_experiment_report(
    exp: Experiment,
    metrics: dict[str, Any],
    gate_results: dict[str, Any] | None = None,
) -> str:
    """Generate human-readable report.md for an experiment."""
    agg = metrics.get("aggregate", {})
    paired = metrics.get("paired_analysis", {})
    worst_reg = paired.get("worst_regressions", [])

    win_rate = agg.get("win_rate", 0.0) * 100
    ci = agg.get("wilson_ci_95", [0.0, 0.0])
    ci_pct = [round(c * 100, 1) for c in ci]

    passed_gates = gate_results.get("passed", False) if gate_results else False
    violations = gate_results.get("violations", []) if gate_results else []

    lines = [
        f"# Experiment Evaluation Report: {exp.experiment_id}",
        "",
        "## 1. Executive Summary",
        f"- **Hypothesis**: {exp.hypothesis}",
        f"- **Intended Change**: {exp.intended_change}",
        f"- **Status**: {exp.status}",
        f"- **Decision**: {exp.promotion_decision}",
        f"- **Main Outcome**: Win Rate {win_rate:.1f}% (95% CI: [{ci_pct[0]}%, {ci_pct[1]}%])",
        "",
        "## 2. Artifact Provenance",
        f"- **Challenger Artifact**: `{exp.challenger_artifact_id}`",
        f"- **Baseline Artifact**: `{exp.baseline_artifact_id}`",
        f"- **Representative Replays**: {', '.join(f'`{r}`' for r in exp.representative_replays) if exp.representative_replays else 'None'}",
        "",
        "## 3. Aggregate Performance",
        f"- **Total Scheduled**: {agg.get('total_scheduled', agg.get('total_matches', 0))}",
        f"- **Valid Matches**: {agg.get('valid_matches', 0)}",
        f"- **Record (W / D / L)**: {agg.get('wins', 0)} / {agg.get('draws', 0)} / {agg.get('losses', 0)}",
        f"- **Mean Score Difference**: {paired.get('mean_score_delta', paired.get('mean_score_diff', 0.0)):+.2f}",
        f"- **Paired Win Rate Delta**: {paired.get('mean_win_rate_delta', 0.0):+.2%}",
        f"- **Weighted Win Delta**: {paired.get('weighted_mean_win_delta', 0.0):+.2%}",
        f"- **Paired Bootstrap CI (95%)**: {paired.get('paired_bootstrap_ci_95', [0.0, 0.0])}",
        "",
        "## 4. Reliability & Runtime",
        f"- **Crashes**: {agg.get('crash_count', 0)} (Rate: {agg.get('crash_rate', 0.0) * 100:.1f}%)",
        f"- **Timeouts**: {agg.get('timeout_count', 0)} (Rate: {agg.get('timeout_rate', 0.0) * 100:.1f}%)",
        f"- **Invalid Actions**: {agg.get('invalid_action_count', 0)}",
        f"- **Missing Replays**: {agg.get('missing_replays', 0)}",
        f"- **Runtime Headroom**: {agg.get('runtime_headroom', 1.0):.1%}",
        f"- **Runtime Percentiles (ms)**: p50={agg.get('runtime_percentiles_ms', {}).get('p50', 0)}, "
        f"p90={agg.get('runtime_percentiles_ms', {}).get('p90', 0)}, "
        f"p99={agg.get('runtime_percentiles_ms', {}).get('p99', 0)}",
        "",
        "## 5. Segment Breakdown",
        "### By Map",
    ]

    for map_name, stats in agg.get("by_map", {}).items():
        total_m = stats.get("valid_total", stats.get("total", 0))
        lines.append(
            f"- **{map_name}**: {stats.get('wins', 0)}W - {stats.get('losses', 0)}L ({total_m} matches)"
        )

    lines.extend(
        [
            "",
            "### By Opponent Group",
        ]
    )
    for grp_name, grp_stats in paired.get("by_opponent_group", {}).items():
        lines.append(
            f"- **{grp_name}**: {grp_stats.get('pair_count', 0)} pairs, "
            f"score_delta={grp_stats.get('mean_score_diff', 0.0):+.2f}, "
            f"win_delta={grp_stats.get('mean_win_diff', 0.0):+.2%}"
        )

    lines.extend(
        [
            "",
            "### By Side",
        ]
    )
    for side_name, stats in agg.get("by_side", {}).items():
        lines.append(f"- **{side_name}**: {stats.get('wins', 0)}W - {stats.get('losses', 0)}L")

    lines.extend(
        [
            "",
            "## 6. Worst Regressions",
        ]
    )
    if worst_reg:
        for reg in worst_reg:
            id_str = reg.get("pair_id") or reg.get("match_id", "unknown")
            lines.append(
                f"- Pair/Match `{id_str}` on `{reg.get('map', 'unknown')}` (seed {reg.get('seed', 0)}): Deficit {reg.get('deficit', 0.0):.1f}"
            )
    else:
        lines.append("No regressions observed against baseline.")

    lines.extend(
        [
            "",
            "## 7. Promotion Gates & Decision",
            f"- **Promotion Passed**: {'YES' if passed_gates else 'NO'}",
        ]
    )
    if violations:
        lines.append("### Gate Violations:")
        for v in violations:
            lines.append(f"- [FAIL] {v}")
    elif passed_gates:
        lines.append("All promotion criteria satisfied.")

    lines.extend(
        [
            "",
            "## 8. Recommended Next Experiment",
            "Investigate worst regression matchups or test broader opponent pool diversity.",
        ]
    )

    return "\n".join(lines)
