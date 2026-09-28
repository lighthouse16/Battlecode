"""Stable and deterministic identifier generation."""

from __future__ import annotations

import time
import uuid
from typing import Any

from battlelab.core.hashing import hash_bytes, hash_dict


def generate_match_id(normalized_spec: dict[str, Any]) -> str:
    """Generate a deterministic match ID from canonical MatchSpec fields."""
    spec_copy = {
        "adapter_name": normalized_spec.get("adapter_name"),
        "adapter_version": normalized_spec.get("adapter_version"),
        "bot_a_id": normalized_spec.get("bot_a_id"),
        "bot_b_id": normalized_spec.get("bot_b_id"),
        "map_name": normalized_spec.get("map_name"),
        "seed": normalized_spec.get("seed"),
        "side_assignment": normalized_spec.get("side_assignment"),
        "time_limit_ms": normalized_spec.get("time_limit_ms"),
    }
    digest = hash_dict(spec_copy)
    return f"m_{digest[:16]}"


def generate_artifact_id(source_hash: str, build_config_hash: str = "") -> str:
    """Generate a stable bot artifact ID based on its content hashes."""
    combined = f"{source_hash}:{build_config_hash}"
    digest = hash_bytes(combined.encode("utf-8"))
    return f"art_{digest[:16]}"


def generate_experiment_id(prefix: str = "exp") -> str:
    """Generate a unique experiment ID with timestamp and entropy."""
    ts = int(time.time())
    rand_part = uuid.uuid4().hex[:8]
    return f"{prefix}_{ts}_{rand_part}"


def generate_tournament_id(prefix: str = "trn") -> str:
    """Generate a unique tournament ID with timestamp and entropy."""
    ts = int(time.time())
    rand_part = uuid.uuid4().hex[:8]
    return f"{prefix}_{ts}_{rand_part}"
