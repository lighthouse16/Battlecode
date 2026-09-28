"""Comprehensive statistical evaluation, paired deltas, and segment metrics."""

from __future__ import annotations

import statistics
from typing import Any

from battlelab.analysis.confidence import paired_bootstrap_difference, wilson_score_interval


def calculate_tournament_metrics(
    matches: list[dict[str, Any]], focus_bot_id: str | None = None
) -> dict[str, Any]:
    """Calculate aggregate and segmented metrics without polluting loss counts with infra failures."""
    total_scheduled = len(matches)
    if total_scheduled == 0:
        return {"total_matches": 0, "valid_matches": 0}

    valid_matches = 0
    wins = 0
    draws = 0
    losses = 0
    infra_failures = 0
    crashes = 0
    timeouts = 0
    invalid_actions = 0
    missing_replays = 0
    durations: list[float] = []

    # Segment breakdowns
    by_map: dict[str, dict[str, Any]] = {}
    by_opponent: dict[str, dict[str, Any]] = {}
    by_side: dict[str, dict[str, Any]] = {}
    by_seed: dict[str, dict[str, Any]] = {}

    for m in matches:
        is_bot_a = (m["bot_a_id"] == focus_bot_id) if focus_bot_id else True
        opp_id = m["bot_b_id"] if is_bot_a else m["bot_a_id"]
        map_name = m["map_name"]
        seed_key = str(m["seed"])

        side_assignment = m.get("side_assignment_json", "")
        side_label = "side_0" if '"A": "side_0"' in side_assignment else "side_1"
        if not is_bot_a:
            side_label = "side_1" if side_label == "side_0" else "side_0"

        outcome = m.get("outcome")

        # Track anomalies
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

        # Check outcome validity
        if outcome == "INFRASTRUCTURE_FAILURE" or m.get("status") in (
            "TERMINAL_FAILURE",
            "RETRYABLE_FAILURE",
        ):
            infra_failures += 1
            # Infrastructure failure reported separately; do not count as gameplay loss!
            continue

        valid_matches += 1
        won = (outcome == "WIN_A" and is_bot_a) or (outcome == "WIN_B" and not is_bot_a)
        is_draw = outcome == "DRAW"

        if won:
            wins += 1
        elif is_draw:
            draws += 1
        else:
            losses += 1

        # Helper to update segmented statistics cleanly
        def _update_segment(table: dict[str, dict[str, Any]], key: str):
            if key not in table:
                table[key] = {"wins": 0, "draws": 0, "losses": 0, "valid_total": 0, "win_rate": 0.0}
            table[key]["valid_total"] += 1
            if won:
                table[key]["wins"] += 1
            elif is_draw:
                table[key]["draws"] += 1
            else:
                table[key]["losses"] += 1
            table[key]["win_rate"] = round(table[key]["wins"] / table[key]["valid_total"], 4)

        _update_segment(by_map, map_name)
        _update_segment(by_opponent, opp_id)
        _update_segment(by_side, side_label)
        _update_segment(by_seed, seed_key)

    win_rate = (wins / valid_matches) if valid_matches > 0 else 0.0
    draw_rate = (draws / valid_matches) if valid_matches > 0 else 0.0
    loss_rate = (losses / valid_matches) if valid_matches > 0 else 0.0
    ci_lower, ci_upper = wilson_score_interval(wins, valid_matches)

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
        "total_scheduled": total_scheduled,
        "valid_matches": valid_matches,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "infrastructure_failures": infra_failures,
        "win_rate": round(win_rate, 4),
        "draw_rate": round(draw_rate, 4),
        "loss_rate": round(loss_rate, 4),
        "wilson_ci_95": [round(ci_lower, 4), round(ci_upper, 4)],
        "crash_count": crashes,
        "crash_rate": round(crashes / total_scheduled, 4) if total_scheduled > 0 else 0.0,
        "timeout_count": timeouts,
        "timeout_rate": round(timeouts / total_scheduled, 4) if total_scheduled > 0 else 0.0,
        "invalid_action_count": invalid_actions,
        "invalid_action_rate": (
            round(invalid_actions / total_scheduled, 4) if total_scheduled > 0 else 0.0
        ),
        "missing_replays": missing_replays,
        "runtime_percentiles_ms": {
            "p50": round(p50, 2),
            "p90": round(p90, 2),
            "p99": round(p99, 2),
        },
        "by_map": by_map,
        "by_opponent": by_opponent,
        "by_side": by_side,
        "by_seed": by_seed,
    }


def calculate_paired_experiment_metrics(
    matches: list[dict[str, Any]],
    challenger_id: str,
    baseline_id: str,
    opponent_pool_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate paired baseline-versus-challenger metrics joined by pair_id."""
    overall = calculate_tournament_metrics(matches, focus_bot_id=challenger_id)

    # Index matches by pair_id
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    direct_matches: list[dict[str, Any]] = []

    for m in matches:
        pair_id = m.get("pair_id")
        if not pair_id:
            continue

        if pair_id.startswith("direct_"):
            direct_matches.append(m)
            continue

        if pair_id not in pairs:
            pairs[pair_id] = {}

        if m["bot_a_id"] == challenger_id:
            pairs[pair_id]["challenger"] = m
        elif m["bot_a_id"] == baseline_id:
            pairs[pair_id]["baseline"] = m

    # Compute paired score deltas and win deltas
    score_diffs: list[float] = []
    win_diffs: list[float] = []
    regressions: list[dict[str, Any]] = []

    opp_groups: dict[str, str] = {}
    opp_weights: dict[str, float] = {}
    if opponent_pool_config and "opponents" in opponent_pool_config:
        for opp in opponent_pool_config["opponents"]:
            opp_groups[opp.get("id", "")] = opp.get("group", "general")
            opp_weights[opp.get("id", "")] = float(opp.get("weight", 1.0))

    group_deltas: dict[str, list[float]] = {}

    for pair_id, pair_data in pairs.items():
        if "challenger" not in pair_data or "baseline" not in pair_data:
            continue

        m_c = pair_data["challenger"]
        m_b = pair_data["baseline"]

        # If either had an infrastructure failure, skip from paired delta
        if (
            m_c.get("outcome") == "INFRASTRUCTURE_FAILURE"
            or m_b.get("outcome") == "INFRASTRUCTURE_FAILURE"
        ):
            continue

        score_c = float(m_c.get("score_a", 0.0))
        score_b = float(m_b.get("score_a", 0.0))
        delta_score = score_c - score_b
        score_diffs.append(delta_score)

        win_c = 1.0 if m_c.get("winner") == "A" else (0.5 if m_c.get("outcome") == "DRAW" else 0.0)
        win_b = 1.0 if m_b.get("winner") == "A" else (0.5 if m_b.get("outcome") == "DRAW" else 0.0)
        win_diffs.append(win_c - win_b)

        opp_id = m_c.get("bot_b_id", "unknown")
        grp = opp_groups.get(opp_id, "general")
        if grp not in group_deltas:
            group_deltas[grp] = []
        group_deltas[grp].append(delta_score)

        if score_c < score_b:
            regressions.append(
                {
                    "pair_id": pair_id,
                    "match_id": pair_id,
                    "opponent": opp_id,
                    "opponent_group": grp,
                    "map": m_c.get("map_name"),
                    "seed": m_c.get("seed"),
                    "score_challenger": score_c,
                    "score_baseline": score_b,
                    "deficit": score_b - score_c,
                    "replay_challenger": m_c.get("replay_hash"),
                    "replay_baseline": m_b.get("replay_hash"),
                }
            )

    # Sort regressions by deficit descending
    regressions.sort(key=lambda r: r["deficit"], reverse=True)

    mean_score_diff, ci_l, ci_u = paired_bootstrap_difference(score_diffs)
    mean_win_diff, win_ci_l, win_ci_u = paired_bootstrap_difference(win_diffs)

    # Breakdown by opponent group
    by_opponent_group: dict[str, dict[str, Any]] = {}
    for grp, deltas in group_deltas.items():
        m_diff, cl, cu = paired_bootstrap_difference(deltas)
        by_opponent_group[grp] = {
            "pair_count": len(deltas),
            "mean_score_diff": round(m_diff, 4),
            "ci_95": [round(cl, 4), round(cu, 4)],
        }

    # Direct head-to-head metrics if present
    direct_metrics = (
        calculate_tournament_metrics(direct_matches, focus_bot_id=challenger_id)
        if direct_matches
        else None
    )

    return {
        "aggregate": overall,
        "paired_analysis": {
            "completed_pairs": len(score_diffs),
            "mean_score_delta": round(mean_score_diff, 4),
            "score_delta_bootstrap_ci_95": [round(ci_l, 4), round(ci_u, 4)],
            "paired_bootstrap_ci_95": [round(ci_l, 4), round(ci_u, 4)],
            "mean_win_rate_delta": round(mean_win_diff, 4),
            "win_delta_bootstrap_ci_95": [round(win_ci_l, 4), round(win_ci_u, 4)],
            "by_opponent_group": by_opponent_group,
            "worst_regressions": regressions[:5],
        },
        "direct_head_to_head": direct_metrics,
    }
