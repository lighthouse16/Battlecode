"""Configuration schema validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def validate_evaluation_config(config: dict[str, Any]) -> list[str]:
    """Validate evaluation.yaml structure and bounds."""
    errors: list[str] = []
    if "adapter" not in config:
        errors.append("Missing required key: 'adapter'")
    if "maps" not in config or not isinstance(config["maps"], list) or len(config["maps"]) == 0:
        errors.append("'maps' must be a non-empty list of map names")
    if "seeds" not in config or not isinstance(config["seeds"], list) or len(config["seeds"]) == 0:
        errors.append("'seeds' must be a non-empty list of integers")
    if config.get("time_limit_ms", 0) <= 0:
        errors.append("'time_limit_ms' must be positive integer")
    if config.get("max_workers", 0) <= 0:
        errors.append("'max_workers' must be positive integer")
    return errors


def validate_promotion_config(config: dict[str, Any]) -> list[str]:
    """Validate promotion.yaml structure and bounds."""
    errors: list[str] = []
    if config.get("min_sample_size", 0) <= 0:
        errors.append("'min_sample_size' must be positive integer")
    win_rate = config.get("min_win_rate", -1.0)
    if not (0.0 <= win_rate <= 1.0):
        errors.append("'min_win_rate' must be between 0.0 and 1.0")
    for rate_key in ["max_crash_rate", "max_timeout_rate", "max_invalid_action_rate"]:
        val = config.get(rate_key, -1.0)
        if not (0.0 <= val <= 1.0):
            errors.append(f"'{rate_key}' must be between 0.0 and 1.0")
    if "min_segment_sample_size" in config:
        seg_val = config["min_segment_sample_size"]
        if isinstance(seg_val, bool) or not isinstance(seg_val, int) or seg_val < 0:
            errors.append("'min_segment_sample_size' must be a non-negative integer")
    return errors


def validate_all_configs(configs_dir: Path | str) -> dict[str, list[str]]:
    """Validate all standard configuration files in configs/."""
    from pathlib import Path

    from battlelab.config.loader import load_yaml_config

    dir_path = Path(configs_dir)
    results = {}

    eval_cfg = load_yaml_config(dir_path / "evaluation.yaml")
    results["evaluation.yaml"] = validate_evaluation_config(eval_cfg)

    prom_cfg = load_yaml_config(dir_path / "promotion.yaml")
    results["promotion.yaml"] = validate_promotion_config(prom_cfg)

    return results
