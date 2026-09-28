"""Match matrix generation for paired evaluations and tournaments."""

from __future__ import annotations

from typing import Any

from battlelab.adapters import get_adapter
from battlelab.core.identifiers import generate_match_id
from battlelab.core.models import MatchSpec
from battlelab.matches.side_swap import get_paired_sides


def generate_match_matrix(
    bot_a_id: str,
    opponents: list[str],
    maps: list[str],
    seeds: list[int],
    adapter_name: str = "mock",
    paired_sides: bool = True,
    repetitions: int = 1,
    time_limit_ms: int = 10000,
    tournament_id: str | None = None,
    experiment_id: str | None = None,
) -> list[MatchSpec]:
    """Generate a deterministic matrix of MatchSpecs with paired side assignments."""
    adapter = get_adapter(adapter_name)
    adapter_ver = adapter.version

    sides = get_paired_sides() if paired_sides else [{"A": "side_0", "B": "side_1"}]
    specs: list[MatchSpec] = []
    seen_ids: set[str] = set()

    for opp_id in opponents:
        for map_name in maps:
            for seed in seeds:
                for side in sides:
                    for rep in range(repetitions):
                        raw_spec = {
                            "adapter_name": adapter_name,
                            "adapter_version": adapter_ver,
                            "bot_a_id": bot_a_id,
                            "bot_b_id": opp_id,
                            "map_name": map_name,
                            "seed": seed + (rep * 10007),
                            "side_assignment": side,
                            "time_limit_ms": time_limit_ms,
                        }
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
                            seed=raw_spec["seed"],
                            side_assignment=side,
                            time_limit_ms=time_limit_ms,
                            retry_attempt=0,
                            tournament_id=tournament_id,
                            experiment_id=experiment_id,
                        )
                        specs.append(spec)

    return specs
