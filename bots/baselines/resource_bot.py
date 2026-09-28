"""Executable Resource Greedy Bot.

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""
import sys
import json

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
        claimed = state.get("claimed_tiles", [])
        already_claimed = any(t.get("x") == my_pos[0] and t.get("y") == my_pos[1] for t in claimed)
        
        if not already_claimed:
            action = {"type": "CLAIM"}
        else:
            w = state.get("map", {}).get("width", 8)
            h = state.get("map", {}).get("height", 8)
            cx, cy = w // 2, h // 2
            if my_pos[0] < cx:
                action = {"type": "MOVE", "direction": "RIGHT"}
            elif my_pos[0] > cx:
                action = {"type": "MOVE", "direction": "LEFT"}
            elif my_pos[1] < cy:
                action = {"type": "MOVE", "direction": "DOWN"}
            elif my_pos[1] > cy:
                action = {"type": "MOVE", "direction": "UP"}
            else:
                action = {"type": "MOVE", "direction": "RIGHT" if my_pos[0] + 1 < w else "LEFT"}

        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
