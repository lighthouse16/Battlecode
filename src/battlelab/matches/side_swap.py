"""Side-swapping utilities for paired evaluation matrices."""

from __future__ import annotations

from typing import Any

SIDE_NORMAL = {"A": "side_0", "B": "side_1"}
SIDE_SWAPPED = {"A": "side_1", "B": "side_0"}


def get_paired_sides() -> list[dict[str, str]]:
    """Return both side assignments for a fair paired evaluation."""
    return [
        {"A": "side_0", "B": "side_1"},
        {"A": "side_1", "B": "side_0"},
    ]


def is_swapped(side_assignment: dict[str, str]) -> bool:
    """Return True if Bot A is assigned to side_1."""
    return side_assignment.get("A") == "side_1"
