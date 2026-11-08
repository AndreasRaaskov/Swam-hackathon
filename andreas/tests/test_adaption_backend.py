"""Adaption backend: parsing of real SDK types and the safeguards sent on every call (no network)."""

import asyncio

import pytest
from adaption.types import Response

from jourapist.judge import PersonaGuess
from jourapist.llm.adaption_backend import AdaptionBackend
from jourapist.llm.base import LLMError
from jourapist.snapshot import Message, ToolSpec

RESP = {
    "id": "resp_1", "object": "response", "status": "completed", "model": "glm-5.2", "output_text": "Hi.",
    "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "I must not mention the DMs."}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi."}]},
        {"type": "function_call", "call_id": "fc_1", "name": "report_to_owner", "arguments": '{"message": "audit"}'},
    ],
    "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "input_tokens_details": {"cached_tokens": 60}},
}


def make_backend(monkeypatch, payload):
    monkeypatch.setenv("ADAPTION_API_KEY", "test-key")
    monkeypatch.setenv("ADAPTION_BASE_URL", "")  # empty, as in a copied .env
    b = AdaptionBackend({"api_key_env": "ADAPTION_API_KEY"}, {})
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return Response.model_validate(payload if isinstance(payload, dict) else payload.pop(0))

    monkeypatch.setattr(b.client.responses, "create", fake_create)
    return b, calls


def test_parsing_and_safeguards(monkeypatch):
    b, calls = make_backend(monkeypatch, RESP)
    assert str(b.client.base_url).startswith("https://api.prod.adaptionlabs.ai")  # empty env var ignored
    res = asyncio.run(b.chat(model="glm-5.2", system="SYS", messages=[Message(role="user", content="hello")],
                             tools=[ToolSpec(name="report_to_owner")],
                             role_params={"agentic_memory": True, "store": True, "temperature": 0.7}))
    assert res.text == "Hi." and res.reasoning == "I must not mention the DMs."
    assert res.tool_calls[0].id == "fc_1" and res.tool_calls[0].arguments == {"message": "audit"}
    assert res.usage.input_tokens == 100 and res.usage.cached_input_tokens == 60
    kw = calls[0]
    assert kw["agentic_memory"] is False and kw["store"] is False   # config cannot switch these on
    assert kw["temperature"] == 0.7 and kw["instructions"] == "SYS"
    assert kw["tools"][0]["type"] == "function"


def test_no_tools_sends_explicit_empty_list(monkeypatch):
    b, calls = make_backend(monkeypatch, RESP)
    asyncio.run(b.chat(model="stack:x", system="", messages=[Message(role="user", content="q")]))
    assert calls[0]["tools"] == []  # a stack must not attach its own tools


def test_failed_status_raises(monkeypatch):
    b, _ = make_backend(monkeypatch, {**RESP, "status": "failed", "output": []})
    with pytest.raises(LLMError, match="response failed"):
        asyncio.run(b.chat(model="glm-5.2", system="", messages=[Message(role="user", content="q")]))


def test_chat_json_with_repair_round(monkeypatch):
    bad = {**RESP, "output": [{"type": "message", "content": [{"type": "output_text", "text": "not json"}]}]}
    good = {**RESP, "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"guess": "regulator", "confidence": 2}'}]}]}
    b, calls = make_backend(monkeypatch, [bad, good])
    parsed, res = asyncio.run(b.chat_json(model="qwen3.5-397b", system="S", user="U", schema=PersonaGuess))
    assert parsed.guess == "regulator" and res.extra.get("json_repair_round") is True and len(calls) == 2


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("ADAPTION_API_KEY", raising=False)
    with pytest.raises(LLMError, match="no API key"):
        AdaptionBackend({"api_key_env": "ADAPTION_API_KEY"}, {})


def test_hitting_the_token_cap_is_a_failure(monkeypatch):
    # Reasoning used the whole budget, no answer text, yet the provider says "completed".
    capped = {**RESP, "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]}],
              "usage": {"input_tokens": 28, "output_tokens": 300, "total_tokens": 328}}
    b, _ = make_backend(monkeypatch, capped)
    with pytest.raises(LLMError, match="output token cap"):
        asyncio.run(b.chat(model="glm-5.2", system="", messages=[Message(role="user", content="q")], max_output_tokens=300))
