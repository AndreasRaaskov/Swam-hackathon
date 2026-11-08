"""Anthropic Messages API backend (official `anthropic` SDK)."""

from __future__ import annotations

import os
import re
from typing import Any

import anthropic

from ..snapshot import Message, ToolSpec
from .base import (Backend, LLMError, LLMResult, T, ToolCall, Usage, assert_not_truncated, parse_json_model,
                   strict_json_schema)

# Models that take thinking={"type": "adaptive"} (4.6+ and the 5.x family).
_ADAPTIVE = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-(5|4-6|4-7|4-8)")
_SAMPLING_PARAMS = {"temperature", "top_p", "top_k"}  # rejected by the current API version


def _tool_id(raw: str | None) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw or "tool") or "tool"


def to_anthropic_messages(messages: list[Message]) -> tuple[list[dict], list[str]]:
    """Neutral messages -> Anthropic messages. Returns (messages, notes about format adaptations)."""
    out: list[dict] = []
    notes: list[str] = []

    def push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        if m.role == "user":
            if m.content:
                push("user", [{"type": "text", "text": m.content}])
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({"type": "tool_use", "id": _tool_id(tc.id), "name": tc.name, "input": tc.arguments})
            push("assistant", blocks)
        elif m.role == "tool":
            push(
                "user",
                [{"type": "tool_result", "tool_use_id": _tool_id(m.tool_call_id), "content": m.content or "(no output)"}],
            )
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start of conversation)"}]})
        notes.append("inserted_leading_user_turn")
    return out, notes


class AnthropicBackend(Backend):
    provider_name = "anthropic"

    def __init__(self, provider_cfg: dict, pricing: dict, name: str = "anthropic"):
        super().__init__(provider_cfg, pricing)
        self.provider_name = name
        key = os.environ.get(provider_cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
        kwargs: dict[str, Any] = {
            "max_retries": int(provider_cfg.get("max_retries", 4)),
            "timeout": float(provider_cfg.get("timeout_s", 300)),
        }
        if key:
            kwargs["api_key"] = key
        else:
            # Without a key the SDK can still use ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
            # Fail fast if none of those exist, instead of recording every call as failed.
            env_name = provider_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
            if os.environ.get(env_name) == "":
                os.environ.pop(env_name)  # an empty var would shadow other credential sources
            has_profile = os.path.isdir(os.path.expanduser("~/.config/anthropic"))
            if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or has_profile):
                raise LLMError(
                    f"Provider {name!r}: no Anthropic credentials. Set {env_name} in .env "
                    "(or switch this role to the adaption provider in config/models.yaml)"
                )
        self.client = anthropic.AsyncAnthropic(**kwargs)

    def _clean_params(self, role_params: dict | None) -> dict:
        params = dict(role_params or {})
        for k in list(params):
            if k in _SAMPLING_PARAMS:
                params.pop(k)
        return params

    async def _create(self, **kwargs) -> Any:
        try:
            return await self.client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"{self.provider_name}/{kwargs.get('model')}: {type(e).__name__}: {e}") from e

    def _result(self, resp: Any, model: str, notes: list[str]) -> LLMResult:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content or []:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                if getattr(block, "thinking", ""):
                    reasoning_parts.append(block.thinking)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {"_value": block.input}
                tool_calls.append(ToolCall(block.id, block.name, args))
        u = resp.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        usage = Usage(
            input_tokens=(u.input_tokens or 0) + cache_read + cache_write,
            output_tokens=u.output_tokens or 0,
            cached_input_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        extra: dict[str, Any] = {}
        if notes:
            extra["format_notes"] = notes
        if resp.stop_reason == "refusal" and getattr(resp, "stop_details", None):
            extra["refusal_category"] = getattr(resp.stop_details, "category", None)
        return LLMResult(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            reasoning="\n".join(reasoning_parts).strip() or None,
            usage=usage,
            model=getattr(resp, "model", model) or model,
            provider=self.provider_name,
            stop_reason=resp.stop_reason,
            refusal=resp.stop_reason == "refusal",
            response_id=getattr(resp, "id", None),
            cost_usd=self.cost(model, usage),
            extra=extra,
        )

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
    ) -> LLMResult:
        msgs, notes = to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": msgs,
            "cache_control": {"type": "ephemeral"},  # auto-cache the growing interview prefix
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        if want_reasoning and _ADAPTIVE.match(model):
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        kwargs.update(self._clean_params(role_params))
        resp = await self._create(**kwargs)
        return assert_not_truncated(self._result(resp, model, notes), max_output_tokens)

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_output_tokens: int = 3000,
        role_params: dict | None = None,
    ) -> tuple[T, LLMResult]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": strict_json_schema(schema)}},
        }
        if system:
            # Frozen rubric text is the stable prefix -> cache it.
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        kwargs.update(self._clean_params(role_params))
        resp = await self._create(**kwargs)
        res = assert_not_truncated(self._result(resp, model, []), max_output_tokens)
        if res.refusal:
            raise LLMError(f"{self.provider_name}/{model}: refusal ({res.extra.get('refusal_category')})", raw_text=res.text)
        return parse_json_model(res.text, schema), res
