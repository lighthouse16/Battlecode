from __future__ import annotations

from typing import TYPE_CHECKING, Any

from battlelab.adapters.base import GameAdapter
from battlelab.adapters.mock.adapter import MockAdapter
from battlelab.adapters.official_placeholder.adapter import OfficialPlaceholderAdapter

if TYPE_CHECKING:
    from battlelab.official.adapter import OfficialAdapter


def _get_adapters_map() -> dict[str, type[GameAdapter]]:
    from battlelab.official.adapter import OfficialAdapter

    return {
        "mock": MockAdapter,
        "official_placeholder": OfficialPlaceholderAdapter,
        "official": OfficialAdapter,
    }


def get_adapter(name: str) -> GameAdapter:
    """Instantiate adapter by name."""
    adapters = _get_adapters_map()
    if name not in adapters:
        raise KeyError(f"Unknown adapter '{name}'. Available: {list(adapters.keys())}")
    return adapters[name]()


def list_adapters() -> list[str]:
    """List registered adapter names."""
    return sorted(list(_get_adapters_map().keys()))


def __getattr__(name: str) -> Any:
    if name == "OfficialAdapter":
        from battlelab.official.adapter import OfficialAdapter

        return OfficialAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GameAdapter",
    "MockAdapter",
    "OfficialAdapter",
    "OfficialPlaceholderAdapter",
    "get_adapter",
    "list_adapters",
]
