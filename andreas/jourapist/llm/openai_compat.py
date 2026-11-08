"""OpenAI-compatible backend (used for Adaption-hosted open-weight models).

Supports the Responses API (default; what the GLM swarms use) and chat.completions.
"""

from __future__ import annotations

import json
import os
from typing import Any

import openai

from ..snapshot import Message, ToolSpec
from .base import (
    Backend,
    LLMError,
    LLMResult,
    T,
    ToolCall,
    Usage,
    assert_not_truncated,
    json_instruction,
    parse_json_model,
    strict_json_schema,
)


def _loads_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"_value": val}
    except json.JSONDecodeError:
        return {"_raw": raw}


def to_responses_input(messages: list[Message]) -> list[dict]:
    items: list[dict] = []
    for m in messages:
        if m.role == "user":
            items.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if m.content:
                items.append({"role": "assistant", "content": m.content})
            for tc in m.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    }
                )
        elif m.role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content}
            )
    return items


def to_chat_messages(system: str, messages: list[Message]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]
            out.append(msg)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
    return out


class OpenAICompatBackend(Backend):
    provider_name = "openai_compatible"

    def __init__(self, provider_cfg: dict, pricing: dict, name: str = "adaption"):
        super().__init__(provider_cfg, pricing)
        self.provider_name = name
        base_url = os.environ.get(provider_cfg.get("base_url_env", ""), "") or provider_cfg.get(
            "base_url"
        )
        api_key = os.environ.get(provider_cfg.get("api_key_env", ""), "")
        if not base_url:
            raise LLMError(
                f"Provider {name!r}: no base URL. Set {provider_cfg.get('base_url_env')} in .env"
            )
        if not api_key:
            raise LLMError(
                f"Provider {name!r}: no API key. Set {provider_cfg.get('api_key_env')} in .env"
            )
        self.api = provider_cfg.get("api", "responses")
        self.structured = bool(provider_cfg.get("structured_output", False))
        self.client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=int(provider_cfg.get("max_retries", 4)),
            timeout=float(provider_cfg.get("timeout_s", 300)),
        )

    # ------------------------------------------------------------------ chat
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
        params = dict(role_params or {})
        params.update(extra_request or {})
        try:
            if self.api == "responses":
                res = await self._responses(model, system, messages, tools, max_output_tokens, params)
            else:
                res = await self._chat_completions(model, system, messages, tools, max_output_tokens, params)
        except openai.APIError as e:
            raise LLMError(f"{self.provider_name}/{model}: {type(e).__name__}: {e}") from e
        return assert_not_truncated(res, max_output_tokens)

    async def _responses(self, model, system, messages, tools, max_out, params) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": to_responses_input(messages),
            "max_output_tokens": max_out,
        }
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
        kwargs.update(params)
        resp = await self.client.responses.create(**kwargs)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        refusal = False
        for item in resp.output or []:
            itype = getattr(item, "type", None)
            if itype == "message":
                for c in item.content or []:
                    ctype = getattr(c, "type", None)
                    if ctype == "output_text":
                        text_parts.append(c.text)
                    elif ctype == "refusal":
                        refusal = True
                        text_parts.append(c.refusal)
            elif itype == "function_call":
                tool_calls.append(
                    ToolCall(item.call_id, item.name, _loads_args(item.arguments), item.arguments or "")
                )
            elif itype == "reasoning":
                for s in getattr(item, "summary", None) or []:
                    if getattr(s, "text", None):
                        reasoning_parts.append(s.text)
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "text", None):
                        reasoning_parts.append(c.text)
        usage = Usage()
        if resp.usage:
            usage.input_tokens = resp.usage.input_tokens or 0
            usage.output_tokens = resp.usage.output_tokens or 0
            details = getattr(resp.usage, "input_tokens_details", None)
            usage.cached_input_tokens = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        stop = resp.status
        if getattr(resp, "incomplete_details", None):
            stop = f"{resp.status}:{getattr(resp.incomplete_details, 'reason', '')}"
        return LLMResult(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            reasoning="\n".join(reasoning_parts).strip() or None,
            usage=usage,
            model=getattr(resp, "model", model) or model,
            provider=self.provider_name,
            stop_reason=stop,
            refusal=refusal,
            response_id=getattr(resp, "id", None),
            cost_usd=self.cost(model, usage),
        )

    async def _chat_completions(self, model, system, messages, tools, max_out, params) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": to_chat_messages(system, messages),
            "max_tokens": max_out,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        kwargs.update(params)
        resp = await self.client.chat.completions.create(**kwargs)
        if not resp.choices:
            raise LLMError(f"{self.provider_name}/{model}: response had no choices")
        choice = resp.choices[0]
        msg = choice.message
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = getattr(msg, "reasoning_content", None) or extra.get("reasoning_content") or extra.get("reasoning")
        tool_calls = [
            ToolCall(tc.id, tc.function.name, _loads_args(tc.function.arguments), tc.function.arguments or "")
            for tc in (msg.tool_calls or [])
        ]
        usage = Usage()
        if resp.usage:
            usage.input_tokens = resp.usage.prompt_tokens or 0
            usage.output_tokens = resp.usage.completion_tokens or 0
            details = getattr(resp.usage, "prompt_tokens_details", None)
            usage.cached_input_tokens = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        refusal_text = getattr(msg, "refusal", None)
        return LLMResult(
            text=(msg.content or refusal_text or "").strip(),
            tool_calls=tool_calls,
            reasoning=(reasoning or "").strip() or None,
            usage=usage,
            model=getattr(resp, "model", model) or model,
            provider=self.provider_name,
            stop_reason=choice.finish_reason,
            refusal=bool(refusal_text),
            response_id=getattr(resp, "id", None),
            cost_usd=self.cost(model, usage),
        )

    # ------------------------------------------------------------- chat_json
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
        extra: dict[str, Any] = {}
        sys_prompt = system
        if self.structured:
            strict = strict_json_schema(schema)
            if self.api == "responses":
                extra["text"] = {
                    "format": {"type": "json_schema", "name": schema.__name__, "schema": strict, "strict": True}
                }
            else:
                extra["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "schema": strict, "strict": True},
                }
        else:
            sys_prompt = (system or "") + json_instruction(schema)

        msgs = [Message(role="user", content=user)]
        res = await self.chat(
            model=model, system=sys_prompt, messages=msgs, max_output_tokens=max_output_tokens,
            role_params=role_params, extra_request=extra,
        )
        try:
            return parse_json_model(res.text, schema), res
        except LLMError as first_err:
            # One explicit repair round; the failure is still raised if this does not parse.
            msgs += [
                Message(role="assistant", content=res.text),
                Message(role="user", content=f"That was not valid JSON for the schema ({first_err}). Reply with only the corrected JSON object."),
            ]
            res2 = await self.chat(
                model=model, system=sys_prompt, messages=msgs, max_output_tokens=max_output_tokens,
                role_params=role_params, extra_request=extra,
            )
            res2.usage.add(res.usage)
            res2.cost_usd = self.cost(model, res2.usage)
            res2.extra["json_repair_round"] = True
            return parse_json_model(res2.text, schema), res2
