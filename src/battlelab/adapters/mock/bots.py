"""Baseline policies and controlled failure test bots for the mock engine.

NOTE: Synthetic policies for platform testing and statistical verification only.
"""

from __future__ import annotations

import random
import time
from typing import Any


def policy_random(state: dict[str, Any], seed: int = 42) -> dict[str, Any]:
    """Random baseline: chooses randomly between MOVE, CLAIM, PASS."""
    # Deterministic pseudo-random per state turn
    eff_seed = state.get("seed", seed)
    rng = random.Random(eff_seed * 1000 + state["turn"] * 31 + state["your_id"])
    choice = rng.choice(["CLAIM", "MOVE", "PASS"])
    if choice == "MOVE":
        direction = rng.choice(["UP", "DOWN", "LEFT", "RIGHT"])
        return {"type": "MOVE", "direction": direction}
    elif choice == "CLAIM":
        return {"type": "CLAIM"}
    return {"type": "PASS"}


def policy_fixed(state: dict[str, Any]) -> dict[str, Any]:
    """Fixed deterministic baseline: cycles through deterministic directions and claims."""
    turn = state["turn"]
    pattern = [
        {"type": "CLAIM"},
        {"type": "MOVE", "direction": "RIGHT"},
        {"type": "CLAIM"},
        {"type": "MOVE", "direction": "DOWN"},
        {"type": "CLAIM"},
        {"type": "MOVE", "direction": "LEFT"},
        {"type": "CLAIM"},
        {"type": "MOVE", "direction": "UP"},
    ]
    return pattern[turn % len(pattern)]


def policy_resource_greedy(state: dict[str, Any]) -> dict[str, Any]:
    """Resource greedy bot: tries to claim current tile; if already claimed, moves toward center."""
    pos = state["your_pos"]
    # Check if current pos is claimed
    already_claimed = any(t["x"] == pos[0] and t["y"] == pos[1] for t in state["claimed_tiles"])
    if not already_claimed:
        return {"type": "CLAIM"}

    # Simple navigation
    w = state["map"]["width"]
    h = state["map"]["height"]
    cx, cy = w // 2, h // 2
    if pos[0] < cx:
        return {"type": "MOVE", "direction": "RIGHT"}
    elif pos[0] > cx:
        return {"type": "MOVE", "direction": "LEFT"}
    elif pos[1] < cy:
        return {"type": "MOVE", "direction": "DOWN"}
    elif pos[1] > cy:
        return {"type": "MOVE", "direction": "UP"}

    # Fallback to alternate tile
    return {"type": "MOVE", "direction": "RIGHT" if pos[0] + 1 < w else "LEFT"}


def policy_aggressive(state: dict[str, Any]) -> dict[str, Any]:
    """Aggressive bot: moves directly towards opponent position, claiming contested tiles."""
    my_pos = state["your_pos"]
    opp_pos = state["opponent_pos"]

    already_claimed = any(
        t["x"] == my_pos[0] and t["y"] == my_pos[1] for t in state["claimed_tiles"]
    )
    if not already_claimed and state["turn"] % 2 == 0:
        return {"type": "CLAIM"}

    dx = opp_pos[0] - my_pos[0]
    dy = opp_pos[1] - my_pos[1]

    if abs(dx) >= abs(dy) and dx != 0:
        return {"type": "MOVE", "direction": "RIGHT" if dx > 0 else "LEFT"}
    elif dy != 0:
        return {"type": "MOVE", "direction": "DOWN" if dy > 0 else "UP"}
    return {"type": "CLAIM"}


def policy_defensive(state: dict[str, Any]) -> dict[str, Any]:
    """Defensive bot: locks down initial quadrant by claiming tiles around spawn."""
    my_pos = state["your_pos"]
    already_claimed = any(
        t["x"] == my_pos[0] and t["y"] == my_pos[1] for t in state["claimed_tiles"]
    )
    if not already_claimed:
        return {"type": "CLAIM"}

    # Small circular patrol
    turn = state["turn"]
    directions = ["RIGHT", "DOWN", "LEFT", "UP"]
    return {"type": "MOVE", "direction": directions[turn % 4]}


# --- Controlled Failure Policies for Testing Infrastructure ---


def policy_crash(state: dict[str, Any]) -> dict[str, Any]:
    """Injected failure: raises exception immediately."""
    raise RuntimeError("Controlled fixture bot crash triggered!")


def policy_timeout(state: dict[str, Any]) -> dict[str, Any]:
    """Injected failure: sleeps for 5 seconds to trigger timeout."""
    time.sleep(5.0)
    return {"type": "PASS"}


def policy_invalid_action(state: dict[str, Any]) -> dict[str, Any]:
    """Injected failure: returns illegal action."""
    return {"type": "TELEPORT", "target": [999, 999]}


def policy_nondeterministic(state: dict[str, Any]) -> dict[str, Any]:
    """Injected failure: uses unseeded system time for non-reproducibility test."""
    if int(time.time() * 1000) % 2 == 0:
        return {"type": "CLAIM"}
    return {"type": "MOVE", "direction": "UP"}


if __name__ == "__main__":
    import json
    import os
    import sys

    policy_name = os.environ.get("BATTLELAB_BOT_POLICY", "fixed")
    for arg in sys.argv[1:]:
        if arg.startswith("--policy="):
            policy_name = arg.split("=")[1]

    policies: dict[str, Any] = {
        "fixed": policy_fixed,
        "random": policy_random,
        "resource": policy_resource_greedy,
        "aggressive": policy_aggressive,
        "defensive": policy_defensive,
        "crash": policy_crash,
        "timeout": policy_timeout,
        "invalid_action": policy_invalid_action,
        "nondeterministic": policy_nondeterministic,
    }
    policy_fn = policies.get(policy_name, policy_fixed)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            state = json.loads(line)
        except Exception:
            continue
        if state.get("event") == "SHUTDOWN":
            break
        raw_action = policy_fn(state)
        action = dict(raw_action) if isinstance(raw_action, dict) else raw_action
        req_id = state.get("_battlelab_request_id")
        if req_id is not None and isinstance(action, dict):
            action["_battlelab_request_id"] = req_id
        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()
