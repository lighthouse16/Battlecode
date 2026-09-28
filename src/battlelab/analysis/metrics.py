"""Evaluation metrics calculation and statistical breakdowns."""

from __future__ import annotations

import statistics
from typing import Any

from battlelab.analysis.confidence import paired_bootstrap_difference, wilson_score_interval


def calculate_tournament_metrics(matches: list[dict[str, Any]], focus_bot_id: str | None = None) -> dict[str, Any]:
    """Calculate aggregate and segmented tournament metrics."""
    total_matches = len(matches)
    if total_matches == 0:
        return {"total_matches": 0}

    wins = 0
    draws = 0
    losses = 0
    infra_failures = 0
    crashes = 0
    timeouts = 0
    invalid_actions = 0
    missing_replays = 0
    durations: list[float] = []

    # Segmentations
    by_map: dict[str, dict[str, int]] = {}
    by_opponent: dict[str, dict[str, int]] = {}
    by_side: dict[str, dict[str, int]] = {}

    for m in matches:
        is_bot_a = (m["bot_a_id"] == focus_bot_id) if focus_bot_id else True
        opp_id = m["bot_b_id"] if is_bot_a else m["bot_a_id"]
        map_name = m["map_name"]
        
        # Side assignment
        side_assignment = m.get("side_assignment_json", "")
        side_label = "side_0" if '"A": "side_0"' in side_assignment else "side_1"
        if not is_bot_a:
            side_label = "side_1" if side_label == "side_0" else "side_0"

        outcome = m.get("outcome")
        if outcome == "INFRASTRUCTURE_FAILURE":
            infra_failures += 1
        elif outcome == "DRAW":
            draws += 1
        elif (outcome == "WIN_A" and is_bot_a) or (outcome == "WIN_B" and not is_bot_a):
            wins += 1
        else:
            losses += 1

        # Anomalies
        if (is_bot_a and m.get("crashed_a")) or (not is_bot_a and m.get("crashed_b")):
            crashes += 1
        if (is_bot_a and m.get("timed_out_a")) or (not is_bot_a and m.get("timed_out_b")):
            timeouts += 1
        if (is_bot_a and m.get("invalid_action_a")) or (not is_bot_a and m.get("invalid_action_b")):
            invalid_actions += 1
        if not m.get("replay_path") or not m.get("replay_hash"):
            missing_replays += 1

        if dur := m.get("duration_ms"):
            durations.append(float(dur))

        # Update breakdown helper
        def _update_breakdown(table: dict[str, dict[str, int]], key: str, is_win: bool, is_draw: bool):
            if key not in table:
                table[key] = {"wins": 0, "draws": 0, "losses": 0, "total": 0}
            table[key]["total"] += 1
            if is_win:
                table[key]["wins"] += 1
            elif is_draw:
                table[key]["draws"] += 1
            else:
                table[key]["losses"] += 1

        won = (outcome == "WIN_A" and is_bot_a) or (outcome == "WIN_B" and not is_bot_a)
        is_dr = (outcome == "DRAW")
        _update_breakdown(by_map, map_name, won, is_dr)
        _update_breakdown(by_opponent, opp_id, won, is_dr)
        _update_breakdown(by_side, side_label, won, is_dr)

    # Rates & Intervals
    valid_games = total_matches - infra_failures
    win_rate = (wins / valid_games) if valid_games > 0 else 0.0
    loss_rate = (losses / valid_games) if valid_games > 0 else 0.0
    draw_rate = (draws / valid_games) if valid_games > 0 else 0.0
    ci_lower, ci_upper = wilson_score_interval(wins, valid_games)

    # Runtime percentiles
    if durations:
        durations.sort()
        p50 = statistics.median(durations)
        p90_idx = int(0.90 * len(durations))
        p99_idx = int(0.99 * len(durations))
        p90 = durations[min(p90_idx, len(durations) - 1)]
        p99 = durations[min(p99_idx, len(durations) - 1)]
    else:
        p50, p90, p99 = 0.0, 0.0, 0.0

    return {
        "total_matches": total_matches,
        "valid_matches": valid_games,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "infrastructure_failures": infra_failures,
        "win_rate": round(win_rate, 4),
        "draw_rate": round(draw_rate, 4),
        "loss_rate": round(loss_rate, 4),
        "wilson_ci_95": [round(ci_lower, 4), round(ci_upper, 4)],
        "crash_count": crashes,
        "crash_rate": round(crashes / total_matches, 4),
        "timeout_count": timeouts,
        "timeout_rate": round(timeouts / total_matches, 4),
        "invalid_action_count": invalid_actions,
        "invalid_action_rate": round(invalid_actions / total_matches, 4),
        "missing_replays": missing_replays,
        "runtime_percentiles_ms": {
            "p50": round(p50, 2),
            "p90": round(p90, 2),
            "p99": round(p99, 2),
        },
        "by_map": by_map,
        "by_opponent": by_opponent,
        "by_side": by_side,
    }


def calculate_paired_experiment_metrics(
    matches: list[dict[str, Any]],
    challenger_id: str,
    baseline_id: str,
) -> dict[str, Any]:
    """Calculate paired comparison between challenger and baseline."""
    overall = calculate_tournament_metrics(matches, focus_bot_id=challenger_id)

    # Calculate paired differences
    # In direct head-to-head or paired evaluation against third parties
    # Score diff: score_challenger - score_baseline
    diffs: list[float] = []
    regressions: list[dict[str, Any]] = []

    for m in matches:
        is_chal_a = (m["bot_a_id"] == challenger_id)
        is_base_b = (m["bot_b_id"] == baseline_id)

        if is_chal_a and is_base_b:
            score_chal = float(m.get("score_a", 0.0))
            score_base = float(m.get("score_b", 0.0))
            diff = score_chal - score_base
            diffs.append(diff)
            if score_chal < score_base:
                regressions.append({
                    "match_id": m["match_id"],
                    "map": m["map_name"],
                    "seed": m["seed"],
                    "score_challenger": score_chal,
                    "score_baseline": score_base,
                    "deficit": score_base - score_chal,
                    "replay_hash": m.get("replay_hash"),
                })
        elif not is_chal_a and (m["bot_a_id"] == baseline_id and m["bot_b_id"] == challenger_id):
            score_chal = float(m.get("score_b", 0.0))
            score_base = float(m.get("score_a", 0.0))
            diff = score_chal - score_base
            diffs.append(diff)
            if score_chal < score_base:
                regressions.append({
                    "match_id": m["match_id"],
                    "map": m["map_name"],
                    "seed": m["seed"],
                    "score_challenger": score_chal,
                    "score_baseline": score_base,
                    "deficit": score_base - score_chal,
                    "replay_hash": m.get("replay_hash"),
                })

    regressions.sort(key=lambda r: r["deficit"], reverse=True)
    mean_diff, ci_l, ci_u = paired_bootstrap_difference(diffs)

    return {
        "aggregate": overall,
        "paired_analysis": {
            "paired_matches_count": len(diffs),
            "mean_score_diff": round(mean_diff, 4),
            "paired_bootstrap_ci_95": [round(ci_l, 4), round(ci_u, 4)],
            "worst_regressions": regressions[:5],
        },
    }
