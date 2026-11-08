"""Cost and token accounting with a budget guard."""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import LLMResult, Usage


@dataclass
class CostTracker:
    max_usd: float | None = None
    total_usd: float = 0.0          # prior recorded spend in this run dir + this invocation
    prior_usd: float = 0.0
    unpriced_calls: int = 0
    unpriced_models: set = field(default_factory=set)
    usage_by_model: dict = field(default_factory=dict)

    def add(self, res: LLMResult) -> None:
        u = self.usage_by_model.setdefault(res.model, Usage())
        u.add(res.usage)
        if res.cost_usd is None:
            self.unpriced_calls += 1
            self.unpriced_models.add(res.model)
        else:
            self.total_usd += res.cost_usd

    def seed_from_records(self, records) -> None:
        """Count spend already recorded in this run dir, so resumed runs respect max_usd overall."""
        for r in records:
            if r.get("cost_usd") is not None:
                self.total_usd += float(r["cost_usd"])
                self.prior_usd += float(r["cost_usd"])

    def over_budget(self) -> bool:
        return self.max_usd is not None and self.total_usd >= self.max_usd

    def summary(self) -> dict:
        return {
            "this_invocation_usd_priced": round(self.total_usd - self.prior_usd, 4),
            "run_dir_total_usd_priced": round(self.total_usd, 4),
            "unpriced_calls": self.unpriced_calls,
            "unpriced_models": sorted(self.unpriced_models),
            "tokens_by_model": {
                m: {"input": u.input_tokens, "output": u.output_tokens, "cached_input": u.cached_input_tokens}
                for m, u in self.usage_by_model.items()
            },
        }
