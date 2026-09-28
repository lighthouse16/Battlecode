"""Deterministic mock turn-based game engine.

NOTE: Purely for infrastructure verification, not a game strategy model.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from battlelab.adapters.mock.maps import MockMap, MOCK_MAPS


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
    crashed_p0: bool = False
    crashed_p1: bool = False
    timed_out_p0: bool = False
    timed_out_p1: bool = False


class MockEngine:
    """Deterministic 2-player turn-based grid capture engine."""

    def __init__(self, game_map: MockMap, seed: int) -> None:
        self.map = game_map
        self.seed = seed
        self.rng = random.Random(seed)
        self.p0 = PlayerState(player_id=0, pos=self.map.spawn_a)
        self.p1 = PlayerState(player_id=1, pos=self.map.spawn_b)
        self.claimed: dict[tuple[int, int], int] = {}  # (x, y) -> player_id
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
            "claimed_tiles": [{"x": x, "y": y, "by": p} for (x, y), p in self.claimed.items()],
        }

    def run_match(
        self,
        bot_func_0: Callable[[dict[str, Any]], dict[str, Any]],
        bot_func_1: Callable[[dict[str, Any]], dict[str, Any]],
        time_limit_ms: int = 10000,
    ) -> EngineResult:
        """Run the match between two bot policy functions."""
        start_time = time.perf_counter()
        
        # Record initial frame
        self._record_frame("INIT", {})

        crashed_p0 = False
        crashed_p1 = False
        timed_out_p0 = False
        timed_out_p1 = False
        invalid_p0 = False
        invalid_p1 = False

        while self.current_turn < self.max_turns:
            self.current_turn += 1

            # Player 0 Turn
            try:
                state_0 = self.get_public_state(0)
                t0 = time.perf_counter()
                action_0 = bot_func_0(state_0)
                elapsed_0 = (time.perf_counter() - t0) * 1000.0
                self.p0.total_time_ms += elapsed_0
                
                if elapsed_0 > time_limit_ms:
                    timed_out_p0 = True
                    break
                
                valid_0 = self._apply_action(0, action_0)
                if not valid_0:
                    self.p0.invalid_actions += 1
                    invalid_p0 = True
            except TimeoutError:
                timed_out_p0 = True
                break
            except Exception as e:
                crashed_p0 = True
                self._record_frame("CRASH", {"player": 0, "error": str(e)})
                break

            # Player 1 Turn
            try:
                state_1 = self.get_public_state(1)
                t1 = time.perf_counter()
                action_1 = bot_func_1(state_1)
                elapsed_1 = (time.perf_counter() - t1) * 1000.0
                self.p1.total_time_ms += elapsed_1
                
                if elapsed_1 > time_limit_ms:
                    timed_out_p1 = True
                    break
                
                valid_1 = self._apply_action(1, action_1)
                if not valid_1:
                    self.p1.invalid_actions += 1
                    invalid_p1 = True
            except TimeoutError:
                timed_out_p1 = True
                break
            except Exception as e:
                crashed_p1 = True
                self._record_frame("CRASH", {"player": 1, "error": str(e)})
                break

            self._record_frame("TURN_END", {"turn": self.current_turn})

        total_duration = (time.perf_counter() - start_time) * 1000.0

        # Determine winner
        winner: int | None = None
        if crashed_p0 or timed_out_p0:
            winner = 1
        elif crashed_p1 or timed_out_p1:
            winner = 0
        elif self.p0.score > self.p1.score:
            winner = 0
        elif self.p1.score > self.p0.score:
            winner = 1
        else:
            winner = None  # DRAW

        return EngineResult(
            winner_player_id=winner,
            score_p0=self.p0.score,
            score_p1=self.p1.score,
            turns_played=self.current_turn,
            duration_ms=total_duration,
            replay_frames=self.replay_frames,
            invalid_action_p0=invalid_p0,
            invalid_action_p1=invalid_p1,
            crashed_p0=crashed_p0,
            crashed_p1=crashed_p1,
            timed_out_p0=timed_out_p0,
            timed_out_p1=timed_out_p1,
        )

    def _apply_action(self, player_id: int, action: dict[str, Any]) -> bool:
        """Apply player action. Return False if invalid."""
        if not isinstance(action, dict):
            return False
        
        act_type = action.get("type", "").upper()
        player = self.p0 if player_id == 0 else self.p1

        if act_type == "MOVE":
            direction = action.get("direction", "").upper()
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
            # Claim current tile
            pos = player.pos
            if pos not in self.claimed:
                self.claimed[pos] = player_id
                tile_val = self.map.resource_grid[pos[1]][pos[0]]
                player.score += float(tile_val)
                player.tiles_claimed.add(pos)
                self._record_frame("CLAIM", {"player": player_id, "pos": list(pos), "val": tile_val})
                return True
            return False

        elif act_type == "PASS":
            self._record_frame("PASS", {"player": player_id})
            return True

        return False

    def _record_frame(self, event: str, data: dict[str, Any]) -> None:
        self.replay_frames.append({
            "turn": self.current_turn,
            "event": event,
            "data": data,
            "scores": [self.p0.score, self.p1.score],
            "p0_pos": list(self.p0.pos),
            "p1_pos": list(self.p1.pos),
        })
