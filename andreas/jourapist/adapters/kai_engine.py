"""Adapter for Kai's market engine.

Not written yet: the engine's export format is not known. Once a run export is available,
convert each agent at each checkpoint into an AgentSnapshot (see docs/SNAPSHOT_FORMAT.md).
Until then, export to the canonical format and use the json_dir adapter.
"""

from __future__ import annotations

from pathlib import Path

from ..snapshot import AgentSnapshot
from .base import Adapter


class KaiEngineAdapter(Adapter):
    name = "kai_engine"

    def load(self, data_dir: Path) -> list[AgentSnapshot]:
        raise NotImplementedError(
            "kai_engine adapter is not implemented yet: the engine's export format is unknown. "
            "Export snapshots in the canonical format (docs/SNAPSHOT_FORMAT.md) and use --adapter json_dir."
        )
