"""Executable Infinite Loop Bot Fixture."""

import json
import sys


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            state = json.loads(line)
            if state.get("event") == "SHUTDOWN":
                break
        except Exception:
            pass
        # Enter tight CPU infinite loop without emitting action
        while True:
            pass


if __name__ == "__main__":
    main()
