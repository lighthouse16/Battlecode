import json
import random
import sys


def main():
    for line in sys.stdin:
        if random.random() < 0.5:
            act = {"type": "CLAIM"}
        else:
            act = {"type": "MOVE", "direction": "UP"}
        sys.stdout.write(json.dumps(act) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
