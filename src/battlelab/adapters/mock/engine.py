"""Deterministic mock turn-based game engine with subprocess isolation support.

NOTE: Purely for infrastructure verification, not a competition strategy model.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from battlelab.adapters.mock.maps import MockMap
from battlelab.bots.process_runner import BotSubprocess

DIRECTIONS = {
    "UP": (0, -1),
    "DOWN": (0, 1),
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
}


@dataclass
class PlayerState:
    player_id: int  # 0 or 1
    pos: tuple[int, int]
    score: float = 0.0
    tiles_claimed: set[tuple[int, int]] = field(default_factory=set)
    invalid_actions: int = 0
    total_time_ms: float = 0.0


@dataclass
class EngineResult:
    winner_player_id: int | None  # 0, 1, or None for DRAW
    score_p0: float
    score_p1: float
    turns_played: int
    duration_ms: float
    replay_frames: list[dict[str, Any]]
    invalid_action_p0: bool = False
    invalid_action_p1: bool = False
    protocol_violation_p0: bool = False
    protocol_violation_p1: bool = False
    crashed_p0: bool = False
    crashed_p1: bool = False
    timed_out_p0: bool = False
    timed_out_p1: bool = False
    cancelled: bool = False
    stderr_p0: str = ""
    stderr_p1: str = ""


class MockEngine:
    """Deterministic 2-player turn-based grid capture engine with process isolation."""

    def __init__(self, game_map: MockMap, seed: int) -> None:
        self.map = game_map
        self.seed = seed
        self.rng = random.Random(seed)
        self.p0 = PlayerState(player_id=0, pos=self.map.spawn_a)
        self.p1 = PlayerState(player_id=1, pos=self.map.spawn_b)
        self.claimed: dict[tuple[int, int], int] = {}
        self.current_turn = 0
        self.max_turns = self.map.turn_limit
        self.replay_frames: list[dict[str, Any]] = []

    def get_public_state(self, for_player: int) -> dict[str, Any]:
        """Generate structured game state observable by a player."""
        return {
            "turn": self.current_turn,
            "max_turns": self.max_turns,
            "map": {
                "name": self.map.name,
                "width": self.map.width,
                "height": self.map.height,
            },
            "your_id": for_player,
            "your_pos": list(self.p0.pos if for_player == 0 else self.p1.pos),
            "opponent_pos": list(self.p1.pos if for_player == 0 else self.p0.pos),
            "your_score": self.p0.score if for_player == 0 else self.p1.score,
            "opponent_score": self.p1.score if for_player == 0 else self.p0.score,
            "seed": self.seed,
            "claimed_tiles": [
                {"x": x, "y": y, "by": p} for (x, y), p in sorted(self.claimed.items())
            ],
        }

    def run_match_subprocesses(
        self,
        bot_proc_0: BotSubprocess,
        bot_proc_1: BotSubprocess,
        per_turn_limit_ms: int = 5000,
        match_wall_clock_limit_ms: int = 60000,
        cancel_event: threading.Event | None = None,
    ) -> EngineResult:
        """Run match between two isolated bot subprocesses."""
        start_time = time.perf_counter()
        turn_timeout_sec = max(0.005, per_turn_limit_ms / 1000.0)

        # Start subprocesses
        bot_proc_0.start()
        bot_proc_1.start()

        self._record_frame("INIT", {})

        crashed_p0 = False
        crashed_p1 = False
        timed_out_p0 = False
        timed_out_p1 = False
        invalid_p0 = False
        invalid_p1 = False
        protocol_p0 = False
        protocol_p1 = False
        cancelled = False

        try:
            while self.current_turn < self.max_turns:
                # Check active cancellation
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break

                # Check match wall-clock limit
                if (time.perf_counter() - start_time) * 1000.0 > match_wall_clock_limit_ms:
                    timed_out_p0 = True
                    timed_out_p1 = True
                    break

                self.current_turn += 1

                # Player 0 Turn
                obs_0 = self.get_public_state(0)
                action_0, status_0 = bot_proc_0.send_turn(obs_0, timeout_seconds=turn_timeout_sec)
                self.p0.total_time_ms += status_0.get("elapsed_ms", 0.0)

                if status_0.get("cancelled"):
                    cancelled = True
                    break
                elif status_0["timed_out"]:
                    timed_out_p0 = True
                    self._record_frame("TIMEOUT", {"player": 0})
                    break
                elif status_0["crashed"]:
                    crashed_p0 = True
                    self._record_frame("CRASH", {"player": 0, "stderr": status_0["stderr"]})
                    break
                elif status_0["malformed"]:
                    protocol_p0 = True
                    invalid_p0 = True
                    self._record_frame(
                        "PROTOCOL_VIOLATION", {"player": 0, "raw": status_0["raw_output"]}
                    )
                    break
                else:
                    assert action_0 is not None
                    valid_0 = self._apply_action(0, action_0)
                    if not valid_0:
                        invalid_p0 = True
                        self.p0.invalid_actions += 1
                        self._record_frame("INVALID_ACTION", {"player": 0, "action": action_0})
                        break

                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break

                # Player 1 Turn
                obs_1 = self.get_public_state(1)
                action_1, status_1 = bot_proc_1.send_turn(obs_1, timeout_seconds=turn_timeout_sec)
                self.p1.total_time_ms += status_1.get("elapsed_ms", 0.0)

                if status_1.get("cancelled"):
                    cancelled = True
                    break
                elif status_1["timed_out"]:
                    timed_out_p1 = True
                    self._record_frame("TIMEOUT", {"player": 1})
                    break
                elif status_1["crashed"]:
                    crashed_p1 = True
                    self._record_frame("CRASH", {"player": 1, "stderr": status_1["stderr"]})
                    break
                elif status_1["malformed"]:
                    protocol_p1 = True
                    invalid_p1 = True
                    self._record_frame(
                        "PROTOCOL_VIOLATION", {"player": 1, "raw": status_1["raw_output"]}
                    )
                    break
                else:
                    assert action_1 is not None
                    valid_1 = self._apply_action(1, action_1)
                    if not valid_1:
                        invalid_p1 = True
                        self.p1.invalid_actions += 1
                        self._record_frame("INVALID_ACTION", {"player": 1, "action": action_1})
                        break

                self._record_frame("TURN_END", {"turn": self.current_turn})

        finally:
            # Guarantee cleanup of both processes and their children
            bot_proc_0.stop()
            bot_proc_1.stop()

        total_duration = (time.perf_counter() - start_time) * 1000.0

        # Resolve winner
        winner: int | None = None
        if crashed_p0 or timed_out_p0 or protocol_p0 or invalid_p0:
            winner = 1
        elif crashed_p1 or timed_out_p1 or protocol_p1 or invalid_p1:
            winner = 0
        elif self.p0.score > self.p1.score:
            winner = 0
        elif self.p1.score > self.p0.score:
            winner = 1
        else:
            winner = None

        return EngineResult(
            winner_player_id=winner,
            score_p0=self.p0.score,
            score_p1=self.p1.score,
            turns_played=self.current_turn,
            duration_ms=total_duration,
            replay_frames=self.replay_frames,
            invalid_action_p0=invalid_p0,
            invalid_action_p1=invalid_p1,
            protocol_violation_p0=protocol_p0,
            protocol_violation_p1=protocol_p1,
            crashed_p0=crashed_p0,
            crashed_p1=crashed_p1,
            timed_out_p0=timed_out_p0,
            timed_out_p1=timed_out_p1,
            cancelled=cancelled,
            stderr_p0="\n".join(bot_proc_0.stderr_lines),
            stderr_p1="\n".join(bot_proc_1.stderr_lines),
        )

    def _apply_action(self, player_id: int, action: dict[str, Any]) -> bool:
        if not isinstance(action, dict):
            return False

        act_type = str(action.get("type", "")).upper()
        player = self.p0 if player_id == 0 else self.p1

        if act_type == "MOVE":
            direction = str(action.get("direction", "")).upper()
            if direction not in DIRECTIONS:
                return False
            dx, dy = DIRECTIONS[direction]
            nx, ny = player.pos[0] + dx, player.pos[1] + dy
            if 0 <= nx < self.map.width and 0 <= ny < self.map.height:
                player.pos = (nx, ny)
                self._record_frame("MOVE", {"player": player_id, "to": [nx, ny]})
                return True
            return False

        elif act_type == "CLAIM":
            pos = player.pos
            if pos not in self.claimed:
                self.claimed[pos] = player_id
                tile_val = self.map.resource_grid[pos[1]][pos[0]]
                player.score += float(tile_val)
                player.tiles_claimed.add(pos)
                self._record_frame(
                    "CLAIM", {"player": player_id, "pos": list(pos), "val": tile_val}
                )
                return True
            return False

        elif act_type == "PASS":
            self._record_frame("PASS", {"player": player_id})
            return True

        return False

    def _record_frame(self, event: str, data: dict[str, Any]) -> None:
        self.replay_frames.append(
            {
                "turn": self.current_turn,
                "event": event,
                "data": data,
                "scores": [self.p0.score, self.p1.score],
                "p0_pos": list(self.p0.pos),
                "p1_pos": list(self.p1.pos),
            }
        )
