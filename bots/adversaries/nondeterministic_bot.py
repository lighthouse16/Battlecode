import json
import random
import sys


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            state = json.loads(line)
        except Exception:
            state = {}
        if state.get("event") == "SHUTDOWN":
            break
        if random.random() < 0.5:
            act = {"type": "CLAIM"}
        else:
            act = {"type": "MOVE", "direction": "UP"}
        req_id = state.get("_battlelab_request_id")
        if req_id is not None:
            act["_battlelab_request_id"] = req_id
        sys.stdout.write(json.dumps(act) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
