"""Executable Challenger v1 Bot (Greedy with Corner Navigation).

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
        claimed_set = {(t["x"], t["y"]) for t in claimed}
        already_claimed = (my_pos[0], my_pos[1]) in claimed_set

        if not already_claimed:
            action = {"type": "CLAIM"}
        else:
            w = state.get("map", {}).get("width", 8)
            h = state.get("map", {}).get("height", 8)
            dirs = [
                ("RIGHT", (my_pos[0] + 1, my_pos[1])),
                ("DOWN", (my_pos[0], my_pos[1] + 1)),
                ("LEFT", (my_pos[0] - 1, my_pos[1])),
                ("UP", (my_pos[0], my_pos[1] - 1)),
            ]
            chosen_dir = None
            for d_name, (nx, ny) in dirs:
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in claimed_set:
                    chosen_dir = d_name
                    break

            if not chosen_dir:
                unclaimed_tiles = [
                    (x, y)
                    for x in range(w)
                    for y in range(h)
                    if (x, y) not in claimed_set
                ]
                if unclaimed_tiles:
                    tx, ty = min(
                        unclaimed_tiles,
                        key=lambda p: abs(p[0] - my_pos[0]) + abs(p[1] - my_pos[1]),
                    )
                    if tx > my_pos[0]:
                        chosen_dir = "RIGHT"
                    elif tx < my_pos[0]:
                        chosen_dir = "LEFT"
                    elif ty > my_pos[1]:
                        chosen_dir = "DOWN"
                    else:
                        chosen_dir = "UP"
                else:
                    chosen_dir = "RIGHT" if my_pos[0] + 1 < w else "LEFT"

            action = {"type": "MOVE", "direction": chosen_dir}

        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
