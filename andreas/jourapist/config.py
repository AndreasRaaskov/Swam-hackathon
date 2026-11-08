"""Path resolution and config loading.

Paths resolve in this order: CLI flag > environment variable > default.
Defaults are ./data, ./outputs and ./config relative to the working directory; if ./config
does not exist, the config bundled next to the package is used. In Docker the env vars point
at the mount points /data, /outputs and /config.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_CONFIG = PACKAGE_ROOT.parent / "config"


def resolve_dir(kind: str, cli_value: str | None = None) -> Path:
    """kind is one of data, outputs, config."""
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env = os.environ.get(f"JOURAPIST_{kind.upper()}")
    if env:
        return Path(env).expanduser().resolve()
    local = Path.cwd() / kind
    if kind == "config" and not local.exists():
        return REPO_CONFIG
    return local.resolve()


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Config:
    config_dir: Path
    models: dict
    experiment: dict
    conditions: dict
    file_hashes: dict = field(default_factory=dict)

    @property
    def personas_dir(self) -> Path:
        return self.config_dir / "personas"

    @property
    def rubrics_dir(self) -> Path:
        return self.config_dir / "rubrics"

    def rubric(self, name: str) -> str:
        return (self.rubrics_dir / f"{name}.md").read_text(encoding="utf-8")

    def rubric_hashes(self) -> dict[str, str]:
        return {p.name: sha256_file(p) for p in sorted(self.rubrics_dir.glob("*.md"))}

    def persona_hashes(self) -> dict[str, str]:
        return {p.name: sha256_file(p) for p in sorted(self.personas_dir.glob("*.md"))}

    def resolve_conditions(self, spec: str) -> list[str]:
        """spec: a condition-set name (core, or a setup such as together), 'all', or a comma list;
        sets can be combined."""
        sets = self.experiment.get("condition_sets", {})
        out: list[str] = []
        for part in [p.strip() for p in spec.split(",") if p.strip()]:
            if part == "all":
                names = [c for s in ("core", "ablation") for c in sets.get(s, [])]
            elif part in sets:
                names = sets[part]
            elif part in self.conditions:
                names = [part]
            else:
                raise ValueError(f"Unknown condition or condition set: {part!r}")
            for n in names:
                if n not in out:
                    out.append(n)
        return out


def load_config(config_dir: Path) -> Config:
    if not config_dir.exists():
        raise FileNotFoundError(f"Config dir not found: {config_dir}")
    models = load_yaml(config_dir / "models.yaml")
    experiment = load_yaml(config_dir / "experiment.yaml")
    conditions = load_yaml(config_dir / "conditions.yaml")
    hashes = {
        name: sha256_file(config_dir / name)
        for name in ("models.yaml", "experiment.yaml", "conditions.yaml")
    }
    return Config(config_dir, models, experiment, conditions, hashes)
