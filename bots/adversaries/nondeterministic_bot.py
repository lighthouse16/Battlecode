"""Executable Non-deterministic Bot Fixture."""
import sys
import json
import time

def main():
    for line in sys.stdin:
        if int(time.time() * 1000) % 2 == 0:
            act = {"type": "CLAIM"}
        else:
            act = {"type": "MOVE", "direction": "UP"}
        sys.stdout.write(json.dumps(act) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
