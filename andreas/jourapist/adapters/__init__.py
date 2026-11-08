"""Adapters turn a team's sandbox output into canonical AgentSnapshot objects."""

from __future__ import annotations

from .base import Adapter


def get_adapter(name: str) -> Adapter:
    if name == "json_dir":
        from .json_dir import JsonDirAdapter

        return JsonDirAdapter()
    if name == "kai_engine":
        from .kai_engine import KaiEngineAdapter

        return KaiEngineAdapter()
    raise ValueError(f"Unknown adapter {name!r} (available: json_dir, kai_engine)")
