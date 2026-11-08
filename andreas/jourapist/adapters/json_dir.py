"""Loads canonical AgentSnapshot files: *.json (one snapshot) and *.jsonl (one per line), recursively."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..snapshot import AgentSnapshot
from .base import Adapter


class JsonDirAdapter(Adapter):
    name = "json_dir"

    def load(self, data_dir: Path) -> list[AgentSnapshot]:
        if not data_dir.exists():
            raise FileNotFoundError(f"Data dir not found: {data_dir}")
        snaps: list[AgentSnapshot] = []
        seen: dict[str, Path] = {}
        files = sorted(p for p in data_dir.rglob("*") if p.suffix in (".json", ".jsonl") and p.is_file())
        for path in files:
            if path.suffix == ".json":
                rows = [(1, path.read_text(encoding="utf-8"))]
            else:
                rows = [(i, line) for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if line.strip()]
            for lineno, raw in rows:
                try:
                    snap = AgentSnapshot.model_validate(json.loads(raw))
                except (json.JSONDecodeError, ValidationError) as e:
                    raise ValueError(f"{path}:{lineno}: not a valid AgentSnapshot: {e}") from e
                if snap.snapshot_id in seen:
                    raise ValueError(f"Duplicate snapshot_id {snap.snapshot_id!r} in {path} and {seen[snap.snapshot_id]}")
                seen[snap.snapshot_id] = path
                snaps.append(snap)
        return snaps
