"""SYNTHETIC TEST SDK — NOT AN OFFICIAL COMPETITION INTERFACE.

This synthetic executable is strictly for testing generic adapter orchestration,
timeouts, process cleanup, and replay contracts in isolation.
It contains zero real Battlecode rules, units, or official competition interfaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SYNTHETIC TEST SDK — NOT AN OFFICIAL COMPETITION INTERFACE"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # probe
    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--json", action="store_true")

    # maps
    p_maps = sub.add_parser("maps")
    p_maps.add_argument("--json", action="store_true")

    # build
    p_build = sub.add_parser("build")
    p_build.add_argument("source_path")
    p_build.add_argument("output_dir")
    p_build.add_argument("--fail", action="store_true")

    # run-match
    p_match = sub.add_parser("run-match")
    p_match.add_argument("--map", required=True)
    p_match.add_argument("--seed", type=int, required=True)
    p_match.add_argument("--bot-a", required=True)
    p_match.add_argument("--bot-b", required=True)
    p_match.add_argument("--output", required=True)
    p_match.add_argument("--timeout-sim", type=float, default=0.0)
    p_match.add_argument("--fail-crash", action="store_true")
    p_match.add_argument("--malformed-result", action="store_true")
    p_match.add_argument("--malformed-replay", action="store_true")

    args = parser.parse_args()

    if args.cmd == "probe":
        data = {
            "sdk_name": "SyntheticTestEngine",
            "sdk_version": "1.0.0-synthetic",
            "executable_exists": True,
            "executable_runnable": True,
            "supported_modes": ["local"],
            "can_run_local": True,
            "can_submit": False,
            "supported_languages": ["python"],
            "build_verified": True,
            "match_verified": True,
            "replay_verified": True,
            "determinism_verified": True,
        }
        print(json.dumps(data))
        return 0

    elif args.cmd == "maps":
        maps = ["synth_grid_8x8", "synth_arena_16x16", "synth_maze_32x32"]
        print(json.dumps(maps))
        return 0

    elif args.cmd == "build":
        if args.fail:
            sys.stderr.write("Synthetic build failure triggered.\n")
            return 1
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "built_bot.txt").write_text("synthetic_build_artifact", encoding="utf-8")
        print(json.dumps({"status": "SUCCESS", "output_dir": str(out)}))
        return 0

    elif args.cmd == "run-match":
        if args.timeout_sim > 0:
            time.sleep(args.timeout_sim)

        if args.fail_crash:
            sys.stderr.write("FATAL: Synthetic engine crashed with segfault simulation.\n")
            return 139

        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Generate deterministic synthetic match outcome
        seed_hash = hashlib.sha256(
            f"{args.map}:{args.seed}:{args.bot_a}:{args.bot_b}".encode()
        ).hexdigest()
        winner = "A" if int(seed_hash[:2], 16) % 2 == 0 else "B"
        score_a = 100.0 if winner == "A" else 45.0
        score_b = 45.0 if winner == "A" else 100.0
        turns = 10 + (int(seed_hash[2:4], 16) % 20)

        # Replay generation
        replay_file = out_dir / "synthetic_replay.json"
        if args.malformed_replay:
            replay_file.write_text("{this is corrupted json", encoding="utf-8")
        else:
            replay_data = {
                "schema_version": "1.0.0",
                "adapter_name": "official_synthetic_test",
                "adapter_version": "1.0.0-synthetic",
                "game_version": "synth-2026",
                "map_id": args.map,
                "seed": args.seed,
                "participants": {"A": args.bot_a, "B": args.bot_b},
                "outcome": f"WIN_{winner}",
                "scores": {"A": score_a, "B": score_b},
                "turn_count": turns,
                "events": [{"turn": t, "event": f"synth_action_{t}"} for t in range(turns)],
                "raw_replay_hash": "",
                "source_metadata": {"synthetic": True, "seed_hash": seed_hash},
            }
            # Hash replay data excluding raw_replay_hash
            raw_bytes = json.dumps(replay_data, sort_keys=True).encode("utf-8")
            raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            replay_data["raw_replay_hash"] = raw_hash
            replay_file.write_text(json.dumps(replay_data, indent=2), encoding="utf-8")

        # Result output
        if args.malformed_result:
            print("NON_JSON_CORRUPTED_RESULT_STRING")
            return 0

        result_data = {
            "winner": winner,
            "score_a": score_a,
            "score_b": score_b,
            "turns_played": turns,
            "outcome": f"WIN_{winner}",
            "replay_file": str(replay_file),
        }
        print(json.dumps(result_data))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
