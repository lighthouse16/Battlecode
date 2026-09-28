"""Executable Random Bot.

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""
import sys
import json
import random

def main():
    rng = random.Random(42)
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

        turn = state.get("turn", 0)
        seed = state.get("seed", 42)
        p_rng = random.Random(seed * 1000 + turn * 31 + state.get("your_id", 0))
        choice = p_rng.choice(["CLAIM", "MOVE", "PASS"])
        if choice == "MOVE":
            direction = p_rng.choice(["UP", "DOWN", "LEFT", "RIGHT"])
            action = {"type": "MOVE", "direction": direction}
        elif choice == "CLAIM":
            action = {"type": "CLAIM"}
        else:
            action = {"type": "PASS"}

        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
