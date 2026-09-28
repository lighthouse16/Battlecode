"""Executable Challenger v1 Bot (Safe Greedy Harvester).

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
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

        my_pos = state.get("your_pos", [0, 0])
        px, py = int(my_pos[0]), int(my_pos[1])
        claimed = state.get("claimed_tiles", [])
        claimed_set = {(int(t["x"]), int(t["y"])) for t in claimed if "x" in t and "y" in t}

        w = int(state.get("map", {}).get("width", 8))
        h = int(state.get("map", {}).get("height", 8))

        if (px, py) not in claimed_set:
            action = {"type": "CLAIM"}
        else:
            unclaimed = [
                (x, y)
                for x in range(w)
                for y in range(h)
                if (x, y) not in claimed_set
            ]
            if not unclaimed:
                action = {"type": "PASS"}
            else:
                tx, ty = min(
                    unclaimed,
                    key=lambda p: abs(p[0] - px) + abs(p[1] - py),
                )
                candidates: list[tuple[str, int]] = []
                if tx > px and px + 1 < w:
                    candidates.append(("RIGHT", abs(tx - (px + 1)) + abs(ty - py)))
                if tx < px and px - 1 >= 0:
                    candidates.append(("LEFT", abs(tx - (px - 1)) + abs(ty - py)))
                if ty > py and py + 1 < h:
                    candidates.append(("DOWN", abs(tx - px) + abs(ty - (py + 1))))
                if ty < py and py - 1 >= 0:
                    candidates.append(("UP", abs(tx - px) + abs(ty - (py - 1))))

                if candidates:
                    candidates.sort(key=lambda c: c[1])
                    action = {"type": "MOVE", "direction": candidates[0][0]}
                else:
                    fallbacks: list[str] = []
                    if px + 1 < w:
                        fallbacks.append("RIGHT")
                    if py + 1 < h:
                        fallbacks.append("DOWN")
                    if px - 1 >= 0:
                        fallbacks.append("LEFT")
                    if py - 1 >= 0:
                        fallbacks.append("UP")
                    if fallbacks:
                        action = {"type": "MOVE", "direction": fallbacks[0]}
                    else:
                        action = {"type": "PASS"}

        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

