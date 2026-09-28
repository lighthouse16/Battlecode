"""Configuration loader for YAML settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import yaml

from battlelab.core.errors import ConfigurationError
from battlelab.storage.paths import get_project_root


def load_yaml_config(file_path: Path | str) -> dict[str, Any]:
    """Load and parse a YAML configuration file."""
    path = Path(file_path)
    if not path.is_absolute():
        path = get_project_root() / path

    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        raise ConfigurationError(f"Error parsing YAML from {path}: {e}") from e
