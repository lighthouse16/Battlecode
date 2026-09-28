"""Battlelab adapters module."""

from battlelab.adapters.base import GameAdapter
from battlelab.adapters.mock.adapter import MockAdapter
from battlelab.adapters.official_placeholder.adapter import OfficialPlaceholderAdapter

_ADAPTERS: dict[str, type[GameAdapter]] = {
    "mock": MockAdapter,
    "official_placeholder": OfficialPlaceholderAdapter,
    "official": OfficialPlaceholderAdapter,
}

def get_adapter(name: str) -> GameAdapter:
    """Instantiate adapter by name."""
    if name not in _ADAPTERS:
        raise KeyError(f"Unknown adapter '{name}'. Available: {list(_ADAPTERS.keys())}")
    return _ADAPTERS[name]()

def list_adapters() -> list[str]:
    """List registered adapter names."""
    return sorted(list(_ADAPTERS.keys()))

__all__ = ["GameAdapter", "MockAdapter", "OfficialPlaceholderAdapter", "get_adapter", "list_adapters"]
