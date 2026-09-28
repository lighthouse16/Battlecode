"""Executable Defensive Bot.

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""
import sys
import json

def main():
    turn_idx = 0
    directions = ["RIGHT", "DOWN", "LEFT", "UP"]
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
        claimed = state.get("claimed_tiles", [])
        already_claimed = any(t.get("x") == my_pos[0] and t.get("y") == my_pos[1] for t in claimed)

        if not already_claimed:
            action = {"type": "CLAIM"}
        else:
            turn = state.get("turn", turn_idx)
            action = {"type": "MOVE", "direction": directions[turn % 4]}

        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()
        turn_idx += 1

if __name__ == "__main__":
    main()
