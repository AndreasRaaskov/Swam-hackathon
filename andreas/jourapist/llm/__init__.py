"""Backend factory: role name -> (backend, model, params)."""

from __future__ import annotations

from dataclasses import dataclass

from .base import Backend, LLMError, LLMResult, ToolCall, Usage  # noqa: F401


@dataclass
class RoleBinding:
    role: str
    backend: Backend
    model: str | None       # None only for target (use the snapshot's model)
    params: dict


class Backends:
    """Creates one backend per provider lazily, so a missing key only fails roles that need it."""

    def __init__(self, models_cfg: dict):
        self.cfg = models_cfg
        self._providers: dict[str, Backend] = {}

    def provider(self, name: str) -> Backend:
        if name not in self._providers:
            pcfg = self.cfg["providers"].get(name)
            if pcfg is None:
                raise LLMError(f"Unknown provider {name!r} (see config/models.yaml)")
            pricing = self.cfg.get("pricing_usd_per_mtok") or {}
            kind = pcfg.get("kind")
            if kind == "adaption":
                from .adaption_backend import AdaptionBackend

                self._providers[name] = AdaptionBackend(pcfg, pricing, name=name)
            elif kind == "openai_compatible":
                from .openai_compat import OpenAICompatBackend

                self._providers[name] = OpenAICompatBackend(pcfg, pricing, name=name)
            elif kind == "anthropic":
                from .anthropic_backend import AnthropicBackend

                self._providers[name] = AnthropicBackend(pcfg, pricing, name=name)
            else:
                raise LLMError(f"Provider {name!r}: unsupported kind {kind!r}")
        return self._providers[name]

    def role(self, role: str) -> RoleBinding:
        rcfg = self.cfg["roles"].get(role)
        if rcfg is None:
            raise LLMError(f"Role {role!r} not configured in models.yaml")
        return RoleBinding(role, self.provider(rcfg["provider"]), rcfg.get("model"), rcfg.get("params") or {})

    def role_enabled(self, role: str) -> bool:
        rcfg = self.cfg["roles"].get(role)
        return bool(rcfg) and rcfg.get("enabled", True)

    def role_spec(self, role: str) -> dict:
        return dict(self.cfg["roles"].get(role) or {})
