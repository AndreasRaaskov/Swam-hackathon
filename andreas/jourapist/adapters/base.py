from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..snapshot import AgentSnapshot


class Adapter(ABC):
    name = "base"

    @abstractmethod
    def load(self, data_dir: Path) -> list[AgentSnapshot]:
        """Load every snapshot under data_dir. Must raise on malformed input, never skip it silently."""
