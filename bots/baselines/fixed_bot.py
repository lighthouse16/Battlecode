"""Executable Fixed Bot.

Protocol: Reads JSON state per turn from stdin, writes JSON action to stdout.
"""
import sys
import json

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
        action = PATTERN[turn % len(PATTERN)]
        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()
        turn_idx += 1

if __name__ == "__main__":
    main()
