"""Match matrix generation for paired evaluations, opponent pools, and tournaments."""

from __future__ import annotations

from typing import Any

from battlelab.adapters import get_adapter
from battlelab.core.hashing import hash_dict
from battlelab.core.identifiers import generate_match_id
from battlelab.core.models import MatchSpec
from battlelab.matches.side_swap import get_paired_sides


def generate_match_matrix(
    bot_a_id: str,
    opponents: list[Any],
    maps: list[str],
    seeds: list[int],
    adapter_name: str = "mock",
    paired_sides: bool = True,
    repetitions: int = 1,
    time_limit_ms: int = 5000,
    per_turn_limit_ms: int | None = None,
    match_wall_clock_limit_ms: int = 60000,
    memory_limit_mb: int = 512,
    tournament_id: str | None = None,
    experiment_id: str | None = None,
    pair_id_prefix: str = "pair",
) -> list[MatchSpec]:
    """Generate a deterministic matrix of MatchSpecs with paired side assignments."""
    adapter = get_adapter(adapter_name)
    adapter_ver = adapter.version
    turn_limit = per_turn_limit_ms or time_limit_ms

    sides = get_paired_sides() if paired_sides else [{"A": "side_0", "B": "side_1"}]
    specs: list[MatchSpec] = []
    seen_ids: set[str] = set()

    for opp in opponents:
        opp_id = getattr(opp, "artifact_id", str(opp))
        opp_label = getattr(opp, "config_id", opp_id)

        for map_name in maps:
            for seed in seeds:
                for side_idx, side in enumerate(sides):
                    for rep in range(repetitions):
                        eff_seed = seed + (rep * 10007)
                        pair_id = f"{pair_id_prefix}_{opp_label}_{map_name}_{eff_seed}_s{side_idx}"

                        raw_spec = {
                            "adapter_name": adapter_name,
                            "adapter_version": adapter_ver,
                            "bot_a_id": bot_a_id,
                            "bot_b_id": opp_id,
                            "map_name": map_name,
                            "seed": eff_seed,
                            "side_assignment": side,
                            "per_turn_limit_ms": turn_limit,
                            "match_wall_clock_limit_ms": match_wall_clock_limit_ms,
                            "memory_limit_mb": memory_limit_mb,
                        }
                        if tournament_id:
                            raw_spec["tournament_id"] = tournament_id
                        if experiment_id:
                            raw_spec["experiment_id"] = experiment_id
                        match_id = generate_match_id(raw_spec)
                        if match_id in seen_ids:
                            continue
                        seen_ids.add(match_id)

                        spec = MatchSpec(
                            match_id=match_id,
                            adapter_name=adapter_name,
                            adapter_version=adapter_ver,
                            bot_a_id=bot_a_id,
                            bot_b_id=opp_id,
                            map_name=map_name,
                            seed=eff_seed,
                            side_assignment=side,
                            per_turn_limit_ms=turn_limit,
                            match_wall_clock_limit_ms=match_wall_clock_limit_ms,
                            memory_limit_mb=memory_limit_mb,
                            time_limit_ms=turn_limit,
                            config_hash=hash_dict(raw_spec),
                            retry_attempt=0,
                            max_attempts=3,
                            pair_id=pair_id,
                            tournament_id=tournament_id,
                            experiment_id=experiment_id,
                        )
                        specs.append(spec)

    return specs


def generate_paired_experiment_matrix(
    challenger_id: str,
    baseline_id: str,
    opponents: list[Any],
    maps: list[str],
    seeds: list[int],
    adapter_name: str = "mock",
    paired_sides: bool = True,
    repetitions: int = 1,
    time_limit_ms: int = 5000,
    per_turn_limit_ms: int | None = None,
    match_wall_clock_limit_ms: int = 60000,
    memory_limit_mb: int = 512,
    tournament_id: str | None = None,
    experiment_id: str | None = None,
    include_direct: bool = True,
) -> list[MatchSpec]:
    """Generate paired matches where both Baseline and Challenger play identical opponents."""
    adapter = get_adapter(adapter_name)
    adapter_ver = adapter.version
    turn_limit = per_turn_limit_ms or time_limit_ms
    sides = get_paired_sides() if paired_sides else [{"A": "side_0", "B": "side_1"}]

    specs: list[MatchSpec] = []
    seen_ids: set[str] = set()

    # 1. Opponent pool matches (both bots face each opponent on same map, seed, side)
    for opp in opponents:
        opp_id = getattr(opp, "artifact_id", str(opp))
        opp_label = getattr(opp, "config_id", opp_id)

        for map_name in maps:
            for seed in seeds:
                for side_idx, side in enumerate(sides):
                    for rep in range(repetitions):
                        eff_seed = seed + (rep * 10007)
                        pair_id = f"pair_{opp_label}_{map_name}_{eff_seed}_s{side_idx}_r{rep}"

                        for bot_id in [baseline_id, challenger_id]:
                            raw_spec = {
                                "adapter_name": adapter_name,
                                "adapter_version": adapter_ver,
                                "bot_a_id": bot_id,
                                "bot_b_id": opp_id,
                                "map_name": map_name,
                                "seed": eff_seed,
                                "side_assignment": side,
                                "per_turn_limit_ms": turn_limit,
                                "match_wall_clock_limit_ms": match_wall_clock_limit_ms,
                                "memory_limit_mb": memory_limit_mb,
                            }
                            if tournament_id:
                                raw_spec["tournament_id"] = tournament_id
                            if experiment_id:
                                raw_spec["experiment_id"] = experiment_id
                            match_id = generate_match_id(raw_spec)
                            if match_id in seen_ids:
                                continue
                            seen_ids.add(match_id)

                            spec = MatchSpec(
                                match_id=match_id,
                                adapter_name=adapter_name,
                                adapter_version=adapter_ver,
                                bot_a_id=bot_id,
                                bot_b_id=opp_id,
                                map_name=map_name,
                                seed=eff_seed,
                                side_assignment=side,
                                per_turn_limit_ms=turn_limit,
                                match_wall_clock_limit_ms=match_wall_clock_limit_ms,
                                memory_limit_mb=memory_limit_mb,
                                time_limit_ms=turn_limit,
                                config_hash=hash_dict(raw_spec),
                                retry_attempt=0,
                                max_attempts=3,
                                pair_id=pair_id,
                                tournament_id=tournament_id,
                                experiment_id=experiment_id,
                            )
                            specs.append(spec)

    # 2. Optional direct head-to-head matches (Challenger vs Baseline)
    if include_direct:
        for map_name in maps:
            for seed in seeds:
                for side_idx, side in enumerate(sides):
                    for rep in range(repetitions):
                        eff_seed = seed + (rep * 10007)
                        pair_id = f"direct_{map_name}_{eff_seed}_s{side_idx}_r{rep}"
                        raw_spec = {
                            "adapter_name": adapter_name,
                            "adapter_version": adapter_ver,
                            "bot_a_id": challenger_id,
                            "bot_b_id": baseline_id,
                            "map_name": map_name,
                            "seed": eff_seed,
                            "side_assignment": side,
                            "per_turn_limit_ms": turn_limit,
                            "match_wall_clock_limit_ms": match_wall_clock_limit_ms,
                            "memory_limit_mb": memory_limit_mb,
                        }
                        if tournament_id:
                            raw_spec["tournament_id"] = tournament_id
                        if experiment_id:
                            raw_spec["experiment_id"] = experiment_id
                        match_id = generate_match_id(raw_spec)
                        if match_id in seen_ids:
                            continue
                        seen_ids.add(match_id)

                        spec = MatchSpec(
                            match_id=match_id,
                            adapter_name=adapter_name,
                            adapter_version=adapter_ver,
                            bot_a_id=challenger_id,
                            bot_b_id=baseline_id,
                            map_name=map_name,
                            seed=eff_seed,
                            side_assignment=side,
                            per_turn_limit_ms=turn_limit,
                            match_wall_clock_limit_ms=match_wall_clock_limit_ms,
                            memory_limit_mb=memory_limit_mb,
                            time_limit_ms=turn_limit,
                            config_hash=hash_dict(raw_spec),
                            retry_attempt=0,
                            max_attempts=3,
                            pair_id=pair_id,
                            tournament_id=tournament_id,
                            experiment_id=experiment_id,
                        )
                        specs.append(spec)

    return specs
