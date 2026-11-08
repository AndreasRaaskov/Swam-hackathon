"""Append-only JSONL store. The latest record per id wins, so runs are resumable."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


class JsonlStore:
    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = key
        self._lock = asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        raise ValueError(f"{self.path}:{lineno}: corrupt JSONL line: {e}") from e
        return out

    def latest(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for rec in self.read_all():
            latest[rec[self.key]] = rec
        return latest

    async def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        async with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
