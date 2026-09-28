"""Mock game maps for infrastructure testing.

NOTE: These maps and mechanics are synthetic infrastructure fixtures
and carry zero assumptions about official competition rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class MockMap:
    name: str
    width: int
    height: int
    spawn_a: tuple[int, int]
    spawn_b: tuple[int, int]
    resource_grid: tuple[tuple[int, ...], ...]
    turn_limit: int = 40

def generate_grid_map(name: str, width: int, height: int, turn_limit: int = 40) -> MockMap:
    """Generate a symmetric synthetic grid map."""
    rows: list[tuple[int, ...]] = []
    for y in range(height):
        row: list[int] = []
        for x in range(width):
            # Deterministic synthetic value
            val = ((x * 3 + y * 7 + 1) % 5) + 1
            row.append(val)
        rows.append(tuple(row))
    
    spawn_a = (0, 0)
    spawn_b = (width - 1, height - 1)
    return MockMap(
        name=name,
        width=width,
        height=height,
        spawn_a=spawn_a,
        spawn_b=spawn_b,
        resource_grid=tuple(rows),
        turn_limit=turn_limit,
    )

MOCK_MAPS: dict[str, MockMap] = {
    "grid_tiny_4x4": generate_grid_map("grid_tiny_4x4", 4, 4, turn_limit=20),
    "grid_classic_8x8": generate_grid_map("grid_classic_8x8", 8, 8, turn_limit=40),
    "grid_sparse_12x12": generate_grid_map("grid_sparse_12x12", 12, 12, turn_limit=60),
}
