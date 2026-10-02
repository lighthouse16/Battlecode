"""Executable Invalid Action Bot Fixture."""

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
        # Emit valid JSON with invalid action type
        act = {"type": "INVALID_ACTION_TEST"}
        if isinstance(state, dict):
            req_id = state.get("_battlelab_request_id")
            if req_id is not None:
                act["_battlelab_request_id"] = req_id
        sys.stdout.write(json.dumps(act) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
