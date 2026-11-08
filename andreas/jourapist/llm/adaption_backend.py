"""Adaption backend using Adaption's official `adaption` SDK (Responses API).

Experimental-validity safeguards, sent on EVERY call:
- agentic_memory=False: otherwise a `stack:` model may inject memories from earlier turns,
  leaking information between interviews that must be independent forks.
- tools=[...] always explicit (empty list when none): otherwise a `stack:` model attaches the
  stack's own configured tools, and interviewers/judges must have no tools.
- store=False: nothing retained server-side.
"""

from __future__ import annotations

import os
from typing import Any

import adaption

from ..snapshot import Message, ToolSpec
from .base import LLMError, LLMResult, ToolCall, Usage, assert_not_truncated
from .openai_compat import OpenAICompatBackend, _loads_args, to_responses_input


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class AdaptionBackend(OpenAICompatBackend):
    """Reuses OpenAICompatBackend.chat_json (prompt-based JSON + one repair round)."""

    def __init__(self, provider_cfg: dict, pricing: dict, name: str = "adaption"):
        # Deliberately not calling OpenAICompatBackend.__init__ (no openai client needed).
        self.cfg = provider_cfg
        self.pricing = pricing or {}
        self.provider_name = name
        self.api = "responses"
        self.structured = False  # Adaption's create() has no json_schema output option
        key_env = provider_cfg.get("api_key_env", "ADAPTION_API_KEY")
        api_key = os.environ.get(key_env, "")
        if not api_key:
            raise LLMError(f"Provider {name!r}: no API key. Set {key_env} in .env")
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": int(provider_cfg.get("max_retries", 4)),
            "timeout": float(provider_cfg.get("timeout_s", 300)),
        }
        # Only override the SDK's default endpoint when a non-empty URL is configured.
        # (An EMPTY ADAPTION_BASE_URL env var would otherwise be used as the URL.)
        base_url = os.environ.get(provider_cfg.get("base_url_env", "ADAPTION_BASE_URL"), "") or provider_cfg.get("base_url")
        if base_url:
            kwargs["base_url"] = base_url
        elif os.environ.get("ADAPTION_BASE_URL") == "":
            os.environ.pop("ADAPTION_BASE_URL")
        self.client = adaption.AsyncAdaption(**kwargs)

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
        extra_request: dict | None = None,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": to_responses_input(messages),
            "max_output_tokens": max_output_tokens,
            "tools": [
                {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
                for t in (tools or [])
            ],
            "agentic_memory": False,
            "store": False,
        }
        if system:
            kwargs["instructions"] = system
        params = dict(role_params or {})
        params.update(extra_request or {})
        for locked in ("agentic_memory", "store", "previous_response_id", "background", "stream"):
            params.pop(locked, None)  # never let config switch these safeguards off
        kwargs.update(params)
        try:
            if self.cfg.get("stream"):
                # Streaming keeps bytes flowing while a reasoning model thinks; without it, long
                # generations (pair judgments) hit the gateway's 504 before the reply is ready.
                resp = None
                async for ev in await self.client.responses.create(**kwargs, stream=True):
                    if _get(ev, "type") in ("response.completed", "response.incomplete", "response.failed"):
                        resp = _get(ev, "response")
                if resp is None:
                    raise LLMError(f"{self.provider_name}/{model}: stream ended without a final response")
            else:
                resp = await self.client.responses.create(**kwargs)
        except adaption.APIError as e:
            raise LLMError(f"{self.provider_name}/{model}: {type(e).__name__}: {e}") from e

        if resp.status == "failed":
            raise LLMError(f"{self.provider_name}/{model}: response failed: {resp.error}")

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        refusal = False
        for item in resp.output or []:
            itype = _get(item, "type")
            if itype == "message":
                for c in _get(item, "content") or []:
                    ctype = _get(c, "type")
                    if ctype == "output_text" and _get(c, "text"):
                        text_parts.append(_get(c, "text"))
                    elif ctype == "refusal":
                        refusal = True
                        text_parts.append(_get(c, "refusal") or "")
            elif itype == "function_call":
                raw = _get(item, "arguments") or ""
                tool_calls.append(ToolCall(_get(item, "call_id") or _get(item, "id") or "call", _get(item, "name") or "", _loads_args(raw), raw))
            elif itype == "reasoning":
                for s in _get(item, "summary") or []:
                    if _get(s, "text"):
                        reasoning_parts.append(_get(s, "text"))
                for c in _get(item, "content") or []:
                    if _get(c, "text"):
                        reasoning_parts.append(_get(c, "text"))
        usage = Usage()
        if resp.usage:
            usage.input_tokens = resp.usage.input_tokens or 0
            usage.output_tokens = resp.usage.output_tokens or 0
            details = resp.usage.input_tokens_details or {}
            usage.cached_input_tokens = int(_get(details, "cached_tokens", 0) or 0)
        stop = resp.status
        if resp.incomplete_details is not None:
            stop = f"{resp.status}:{resp.incomplete_details.reason}"
        return assert_not_truncated(LLMResult(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            reasoning="\n".join(reasoning_parts).strip() or None,
            usage=usage,
            model=resp.model or model,
            provider=self.provider_name,
            stop_reason=stop,
            refusal=refusal,
            response_id=resp.id,
            # Adaption reports the billed cost per call; the pricing table is the fallback.
            cost_usd=_get(resp.usage, "cost") if resp.usage and _get(resp.usage, "cost") is not None else self.cost(model, usage),
            extra={"other_output_types": sorted({_get(i, "type") for i in resp.output or []} - {"message", "function_call", "reasoning"})}
            if any(_get(i, "type") not in ("message", "function_call", "reasoning") for i in resp.output or [])
            else {},
        ), max_output_tokens)
