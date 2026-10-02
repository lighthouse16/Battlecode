"""Executable Fixed Bot.

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""

import json
import sys

PATTERN = [
    {"type": "CLAIM"},
    {"type": "MOVE", "direction": "RIGHT"},
    {"type": "CLAIM"},
    {"type": "MOVE", "direction": "DOWN"},
    {"type": "CLAIM"},
    {"type": "MOVE", "direction": "LEFT"},
    {"type": "CLAIM"},
    {"type": "MOVE", "direction": "UP"},
]


def main():
    turn_idx = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            state = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"Error parsing state: {e}\n")
            sys.stderr.flush()
            continue

        if state.get("event") == "SHUTDOWN":
            break

        turn = state.get("turn", turn_idx)
        action = dict(PATTERN[turn % len(PATTERN)])
        if action.get("type") == "MOVE":
            pos = state.get("your_pos", [0, 0])
            m = state.get("map", {})
            w = m.get("width", 8)
            h = m.get("height", 8)
            d = action.get("direction")
            if d == "RIGHT" and pos[0] + 1 >= w:
                action = {"type": "PASS"}
            elif d == "LEFT" and pos[0] - 1 < 0:
                action = {"type": "PASS"}
            elif d == "DOWN" and pos[1] + 1 >= h:
                action = {"type": "PASS"}
            elif d == "UP" and pos[1] - 1 < 0:
                action = {"type": "PASS"}
        req_id = state.get("_battlelab_request_id")
        if req_id is not None:
            action["_battlelab_request_id"] = req_id
        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()
        turn_idx += 1


if __name__ == "__main__":
    main()
