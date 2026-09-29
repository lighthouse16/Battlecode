"""Comprehensive statistical evaluation, paired deltas, and segment metrics."""

from __future__ import annotations

import json
import math
import statistics
from typing import Any

from battlelab.analysis.confidence import (
    paired_bootstrap_difference,
    weighted_paired_bootstrap_difference,
    wilson_score_interval,
)


def calculate_tournament_metrics(
    matches: list[dict[str, Any]], focus_bot_id: str | None = None
) -> dict[str, Any]:
    """Calculate aggregate and segmented metrics without polluting loss counts with infra failures."""
    if focus_bot_id:
        matches = [
            m
            for m in matches
            if m.get("bot_a_id") == focus_bot_id or m.get("bot_b_id") == focus_bot_id
        ]
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
    opponent_pool_config: dict[str, Any] | list[Any] | None = None,
    resolved_opponents: list[Any] | dict[str, Any] | None = None,
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

    # Build opponent lookup
    opp_lookup: dict[str, dict[str, Any]] = {}

    def _register_opp(c_id: str, a_id: str, grp: str, w: float, nm: str):
        info = {
            "config_id": c_id,
            "artifact_id": a_id,
            "group": grp,
            "weight": float(w),
            "name": nm,
        }
        if a_id:
            opp_lookup[a_id] = info
        if c_id:
            opp_lookup[c_id] = info

    if resolved_opponents:
        items = (
            resolved_opponents.values()
            if isinstance(resolved_opponents, dict)
            else resolved_opponents
        )
        for ro in items:
            c_id = getattr(ro, "config_id", None) or (
                ro.get("config_id") if isinstance(ro, dict) else ""
            )
            a_id = getattr(ro, "artifact_id", None) or (
                ro.get("artifact_id") if isinstance(ro, dict) else ""
            )
            grp = getattr(ro, "group", None) or (
                ro.get("group") if isinstance(ro, dict) else "general"
            )
            w = (
                getattr(ro, "weight", None)
                if hasattr(ro, "weight")
                else (ro.get("weight", 1.0) if isinstance(ro, dict) else 1.0)
            )
            nm = getattr(ro, "name", None) or (ro.get("name") if isinstance(ro, dict) else c_id)
            _register_opp(str(c_id), str(a_id), str(grp), float(w or 1.0), str(nm))

    if opponent_pool_config:
        opp_list = (
            opponent_pool_config.get("opponents", [])
            if isinstance(opponent_pool_config, dict)
            else opponent_pool_config
        )
        for opp in opp_list:
            if hasattr(opp, "config_id"):
                _register_opp(opp.config_id, opp.artifact_id, opp.group, opp.weight, opp.name)
            elif isinstance(opp, dict):
                c_id = opp.get("id") or opp.get("config_id") or ""
                a_id = opp.get("artifact_id") or c_id
                grp = opp.get("group", "general")
                w = float(opp.get("weight", 1.0))
                nm = opp.get("name", c_id)
                _register_opp(c_id, a_id, grp, w, nm)

    # Compute paired score deltas and win deltas
    score_diffs: list[float] = []
    win_diffs: list[float] = []
    pair_weights: list[float] = []
    regressions: list[dict[str, Any]] = []

    group_score_deltas: dict[str, list[float]] = {}
    group_win_deltas: dict[str, list[float]] = {}
    group_weights: dict[str, list[float]] = {}

    opp_score_deltas: dict[str, list[float]] = {}
    opp_win_deltas: dict[str, list[float]] = {}

    map_score_deltas: dict[str, list[float]] = {}
    map_win_deltas: dict[str, list[float]] = {}

    side_score_deltas: dict[str, list[float]] = {}
    side_win_deltas: dict[str, list[float]] = {}

    seed_score_deltas: dict[str, list[float]] = {}
    seed_win_deltas: dict[str, list[float]] = {}

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
        delta_win = win_c - win_b
        win_diffs.append(delta_win)

        opp_raw_id = m_c.get("bot_b_id", "unknown")
        opp_info = opp_lookup.get(
            opp_raw_id,
            {
                "config_id": opp_raw_id,
                "artifact_id": opp_raw_id,
                "group": "general",
                "weight": 1.0,
                "name": opp_raw_id,
            },
        )
        opp_config_id = opp_info["config_id"]
        grp = opp_info["group"]
        weight = opp_info["weight"]
        pair_weights.append(weight)

        if grp not in group_score_deltas:
            group_score_deltas[grp] = []
            group_win_deltas[grp] = []
            group_weights[grp] = []
        group_score_deltas[grp].append(delta_score)
        group_win_deltas[grp].append(delta_win)
        group_weights[grp].append(weight)

        if opp_config_id not in opp_score_deltas:
            opp_score_deltas[opp_config_id] = []
            opp_win_deltas[opp_config_id] = []
        opp_score_deltas[opp_config_id].append(delta_score)
        opp_win_deltas[opp_config_id].append(delta_win)

        map_name = m_c.get("map_name", "unknown")
        if map_name not in map_score_deltas:
            map_score_deltas[map_name] = []
            map_win_deltas[map_name] = []
        map_score_deltas[map_name].append(delta_score)
        map_win_deltas[map_name].append(delta_win)

        side_assignment = m_c.get("side_assignment_json", "")
        side_label = "side_0" if '"A": "side_0"' in side_assignment else "side_1"
        if side_label not in side_score_deltas:
            side_score_deltas[side_label] = []
            side_win_deltas[side_label] = []
        side_score_deltas[side_label].append(delta_score)
        side_win_deltas[side_label].append(delta_win)

        seed_str = str(m_c.get("seed", "unknown"))
        if seed_str not in seed_score_deltas:
            seed_score_deltas[seed_str] = []
            seed_win_deltas[seed_str] = []
        seed_score_deltas[seed_str].append(delta_score)
        seed_win_deltas[seed_str].append(delta_win)

        if score_c < score_b:
            regressions.append(
                {
                    "pair_id": pair_id,
                    "match_id": pair_id,
                    "opponent": opp_config_id,
                    "opponent_group": grp,
                    "map": map_name,
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

    # Calculate unweighted bootstrap
    unweighted_mean_score_diff, unweighted_score_ci_l, unweighted_score_ci_u = (
        paired_bootstrap_difference(score_diffs)
    )
    unweighted_mean_win_diff, unweighted_win_ci_l, unweighted_win_ci_u = (
        paired_bootstrap_difference(win_diffs)
    )

    # Calculate weighted bootstrap
    weighted_mean_score_diff, weighted_score_ci_l, weighted_score_ci_u = (
        weighted_paired_bootstrap_difference(score_diffs, pair_weights)
    )
    weighted_mean_win_diff, weighted_win_ci_l, weighted_win_ci_u = (
        weighted_paired_bootstrap_difference(win_diffs, pair_weights)
    )

    # Breakdown by opponent group
    by_opponent_group: dict[str, dict[str, Any]] = {}
    for grp, s_deltas in group_score_deltas.items():
        w_deltas = group_win_deltas[grp]
        m_score, s_cl, s_cu = paired_bootstrap_difference(s_deltas)
        m_win, w_cl, w_cu = paired_bootstrap_difference(w_deltas)
        by_opponent_group[grp] = {
            "pair_count": len(s_deltas),
            "mean_score_diff": round(m_score, 4),
            "score_ci_95": [round(s_cl, 4), round(s_cu, 4)],
            "ci_95": [round(s_cl, 4), round(s_cu, 4)],
            "mean_win_diff": round(m_win, 4),
            "win_ci_95": [round(w_cl, 4), round(w_cu, 4)],
        }

    # Breakdown by opponent
    by_opponent: dict[str, dict[str, Any]] = {}
    for op, s_deltas in opp_score_deltas.items():
        w_deltas = opp_win_deltas[op]
        m_score, s_cl, s_cu = paired_bootstrap_difference(s_deltas)
        m_win, w_cl, w_cu = paired_bootstrap_difference(w_deltas)
        by_opponent[op] = {
            "pair_count": len(s_deltas),
            "mean_score_diff": round(m_score, 4),
            "score_ci_95": [round(s_cl, 4), round(s_cu, 4)],
            "mean_win_diff": round(m_win, 4),
            "win_ci_95": [round(w_cl, 4), round(w_cu, 4)],
        }

    # Breakdown by map
    by_map_paired: dict[str, dict[str, Any]] = {}
    for mp, s_deltas in map_score_deltas.items():
        w_deltas = map_win_deltas[mp]
        m_score, _, _ = paired_bootstrap_difference(s_deltas)
        m_win, _, _ = paired_bootstrap_difference(w_deltas)
        by_map_paired[mp] = {
            "pair_count": len(s_deltas),
            "mean_score_diff": round(m_score, 4),
            "mean_win_diff": round(m_win, 4),
        }

    # Breakdown by side
    by_side_paired: dict[str, dict[str, Any]] = {}
    for sd, s_deltas in side_score_deltas.items():
        w_deltas = side_win_deltas[sd]
        m_score, _, _ = paired_bootstrap_difference(s_deltas)
        m_win, _, _ = paired_bootstrap_difference(w_deltas)
        by_side_paired[sd] = {
            "pair_count": len(s_deltas),
            "mean_score_diff": round(m_score, 4),
            "mean_win_diff": round(m_win, 4),
        }

    # Breakdown by seed
    by_seed_paired: dict[str, dict[str, Any]] = {}
    for sd, s_deltas in seed_score_deltas.items():
        w_deltas = seed_win_deltas[sd]
        m_score, _, _ = paired_bootstrap_difference(s_deltas)
        m_win, _, _ = paired_bootstrap_difference(w_deltas)
        by_seed_paired[sd] = {
            "pair_count": len(s_deltas),
            "mean_score_diff": round(m_score, 4),
            "mean_win_diff": round(m_win, 4),
        }

    # Runtime headroom: strictly from challenger per-turn bot measurements
    challenger_match_count = 0
    challenger_telemetry_match_count = 0
    missing_telemetry_count = 0
    challenger_turn_durations: list[float] = []

    for m in matches:
        is_challenger_a = m.get("bot_a_id") == challenger_id
        is_challenger_b = m.get("bot_b_id") == challenger_id
        if not is_challenger_a and not is_challenger_b:
            continue

        challenger_match_count += 1
        stats_key = "bot_a_stats" if is_challenger_a else "bot_b_stats"
        turns_key = "bot_a_turn_durations_ms" if is_challenger_a else "bot_b_turn_durations_ms"
        stats = m.get(stats_key)
        if not stats or not isinstance(stats, dict):
            rj = m.get("result_json")
            if rj:
                if isinstance(rj, str):
                    try:
                        rj = json.loads(rj)
                    except Exception:
                        rj = {}
                if isinstance(rj, dict) and stats_key in rj and isinstance(rj[stats_key], dict):
                    stats = rj[stats_key]

        valid_turns: list[float] = []
        is_match_telemetry_valid = False
        raw_turns = None
        if isinstance(stats, dict):
            raw_turns = stats.get("turn_durations_ms")
        if raw_turns is None and turns_key in m:
            raw_turns = m[turns_key]

        if raw_turns and isinstance(raw_turns, list):
            has_invalid = False
            for x in raw_turns:
                if isinstance(x, bool) or not isinstance(x, (int, float)):
                    has_invalid = True
                    break
                try:
                    val = float(x)
                except (ValueError, TypeError):
                    has_invalid = True
                    break
                if math.isnan(val) or math.isinf(val) or val < 0:
                    has_invalid = True
                    break
                valid_turns.append(val)
            if not has_invalid and len(valid_turns) > 0:
                is_match_telemetry_valid = True
        elif isinstance(stats, dict) and (max_turn := stats.get("max_turn_ms")) is not None:
            if not isinstance(max_turn, bool) and isinstance(max_turn, (int, float)):
                try:
                    val = float(max_turn)
                    if not (math.isnan(val) or math.isinf(val) or val < 0):
                        valid_turns.append(val)
                        is_match_telemetry_valid = True
                except (ValueError, TypeError):
                    pass

        if is_match_telemetry_valid:
            challenger_telemetry_match_count += 1
            challenger_turn_durations.extend(valid_turns)
        else:
            missing_telemetry_count += 1

    runtime_telemetry_complete = challenger_match_count > 0 and missing_telemetry_count == 0

    per_turn_limit = 5000.0
    for m in matches:
        if limit := (m.get("per_turn_limit_ms") or m.get("time_limit_ms")):
            try:
                per_turn_limit = float(limit)
                break
            except (ValueError, TypeError):
                pass

    overall["runtime_challenger_match_count"] = challenger_match_count
    overall["runtime_telemetry_match_count"] = challenger_telemetry_match_count
    overall["runtime_telemetry_missing_count"] = missing_telemetry_count
    overall["runtime_telemetry_complete"] = runtime_telemetry_complete
    overall["per_turn_limit_ms"] = per_turn_limit

    if runtime_telemetry_complete and challenger_turn_durations:
        challenger_turn_durations.sort()
        p50 = statistics.median(challenger_turn_durations)
        p90_idx = int(0.90 * len(challenger_turn_durations))
        p99_idx = int(0.99 * len(challenger_turn_durations))
        p90 = challenger_turn_durations[min(p90_idx, len(challenger_turn_durations) - 1)]
        p99 = challenger_turn_durations[min(p99_idx, len(challenger_turn_durations) - 1)]
        headroom = 1.0 - (p99 / per_turn_limit) if per_turn_limit > 0 else 1.0
        overall["runtime_percentiles_ms"] = {
            "p50": round(p50, 2),
            "p90": round(p90, 2),
            "p99": round(p99, 2),
        }
        overall["runtime_headroom"] = round(max(0.0, min(1.0, headroom)), 4)
    else:
        overall["runtime_percentiles_ms"] = None
        overall["runtime_headroom"] = None

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
            "mean_score_delta": round(weighted_mean_score_diff, 4),
            "score_delta_bootstrap_ci_95": [
                round(weighted_score_ci_l, 4),
                round(weighted_score_ci_u, 4),
            ],
            "paired_bootstrap_ci_95": [
                round(weighted_score_ci_l, 4),
                round(weighted_score_ci_u, 4),
            ],
            "mean_win_rate_delta": round(weighted_mean_win_diff, 4),
            "win_delta_bootstrap_ci_95": [
                round(weighted_win_ci_l, 4),
                round(weighted_win_ci_u, 4),
            ],
            "weighted_mean_score_delta": round(weighted_mean_score_diff, 4),
            "weighted_score_delta_ci_95": [
                round(weighted_score_ci_l, 4),
                round(weighted_score_ci_u, 4),
            ],
            "weighted_mean_win_delta": round(weighted_mean_win_diff, 4),
            "weighted_win_delta_ci_95": [
                round(weighted_win_ci_l, 4),
                round(weighted_win_ci_u, 4),
            ],
            "unweighted_mean_score_delta": round(unweighted_mean_score_diff, 4),
            "unweighted_score_delta_ci_95": [
                round(unweighted_score_ci_l, 4),
                round(unweighted_score_ci_u, 4),
            ],
            "unweighted_mean_win_delta": round(unweighted_mean_win_diff, 4),
            "unweighted_win_delta_ci_95": [
                round(unweighted_win_ci_l, 4),
                round(unweighted_win_ci_u, 4),
            ],
            "by_opponent": by_opponent,
            "by_opponent_group": by_opponent_group,
            "by_map": by_map_paired,
            "by_side": by_side_paired,
            "by_seed": by_seed_paired,
            "worst_regressions": regressions[:5],
        },
        "direct_head_to_head": direct_metrics,
    }
