"""Provider-neutral LLM interface used by every role (target, interviewer, judge)."""

from __future__ import annotations

import copy
import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..snapshot import Message, ToolSpec

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A call failed after the SDK's own retries. Callers record it as a failure."""

    def __init__(self, message: str, raw_text: str | None = None):
        super().__init__(message)
        self.raw_text = raw_text


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class LLMResult:
    text: str
    tool_calls: list[ToolCall]
    reasoning: str | None
    usage: Usage
    model: str
    provider: str
    stop_reason: str | None = None
    refusal: bool = False
    response_id: str | None = None
    cost_usd: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class Backend(ABC):
    provider_name: str = "base"

    def __init__(self, provider_cfg: dict, pricing: dict):
        self.cfg = provider_cfg
        self.pricing = pricing or {}

    @abstractmethod
    async def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_output_tokens: int = 4000,
        want_reasoning: bool = True,
        role_params: dict | None = None,
    ) -> LLMResult: ...

    @abstractmethod
    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_output_tokens: int = 3000,
        role_params: dict | None = None,
    ) -> tuple[T, LLMResult]: ...

    def cost(self, model: str, usage: Usage) -> float | None:
        price = self.pricing.get(model)
        if not price:
            return None
        inp = price["input"]
        cache_read = price.get("cache_read", inp)
        cache_write = price.get("cache_write", inp * 1.25)
        # input_tokens is the TOTAL prompt size; cached/cache-write tokens are subsets of it.
        uncached = max(usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens, 0)
        total = (
            uncached * inp
            + usage.cached_input_tokens * cache_read
            + usage.cache_write_tokens * cache_write
            + usage.output_tokens * price["output"]
        )
        return total / 1_000_000


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> strict JSON schema (all properties required, no extras, no titles)."""
    schema = copy.deepcopy(model.model_json_schema())

    def fix(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    if "$defs" in schema:
        raise ValueError(f"{model.__name__}: keep judge schemas flat (no nested models)")
    return schema


_TRUNCATION_STOPS = {"max_tokens", "length", "incomplete:max_output_tokens"}


def assert_not_truncated(res: LLMResult, cap: int | None) -> LLMResult:
    """A reply that hit its output cap is a failure, never data: reasoning models can spend the
    whole budget thinking and return empty text, and some providers still report 'completed'."""
    if res.stop_reason in _TRUNCATION_STOPS or (cap and res.usage.output_tokens >= cap):
        raise LLMError(
            f"{res.provider}/{res.model}: hit the output token cap ({cap}; used {res.usage.output_tokens}, "
            f"reasoning {len(res.reasoning or '')} chars) - answer missing or truncated. "
            "Raise target/interviewer/judge_max_output_tokens in experiment.yaml.",
            raw_text=res.text,
        )
    return res


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_model(text: str, schema: type[T]) -> T:
    """Parse a JSON object from model text (tolerates code fences / surrounding prose)."""
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    m = _JSON_OBJ.search(text)
    if m:
        candidates.append(m.group(0))
    last_err: Exception | None = None
    for c in candidates:
        try:
            return schema.model_validate(json.loads(c))
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = e
    raise LLMError(f"Could not parse {schema.__name__} from model output: {last_err}", raw_text=text)


def json_instruction(schema: type[BaseModel]) -> str:
    return (
        "\n\nRespond with ONLY a JSON object (no prose, no code fences) that matches this JSON schema:\n"
        + json.dumps(strict_json_schema(schema), indent=1)
    )
