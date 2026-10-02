"""Executable Aggressive Bot.

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""

import json
import sys


def main():
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
        opp_pos = state.get("opponent_pos", [0, 0])
        claimed = state.get("claimed_tiles", [])
        already_claimed = any(t.get("x") == my_pos[0] and t.get("y") == my_pos[1] for t in claimed)

        if not already_claimed and state.get("turn", 0) % 2 == 0:
            action = {"type": "CLAIM"}
        else:
            dx = opp_pos[0] - my_pos[0]
            dy = opp_pos[1] - my_pos[1]
            if abs(dx) >= abs(dy) and dx != 0:
                action = {"type": "MOVE", "direction": "RIGHT" if dx > 0 else "LEFT"}
            elif dy != 0:
                action = {"type": "MOVE", "direction": "DOWN" if dy > 0 else "UP"}
            else:
                action = {"type": "CLAIM"}

        req_id = state.get("_battlelab_request_id")
        if req_id is not None:
            action["_battlelab_request_id"] = req_id

        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
