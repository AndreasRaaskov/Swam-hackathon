"""Scripted fake backend for unit tests ONLY. It is not importable from the jourapist package
and cannot be selected from config, so it can never produce data in a real run."""

from __future__ import annotations

from jourapist.llm.base import Backend, LLMResult, ToolCall, Usage, parse_json_model
from jourapist.llm import RoleBinding


class FakeBackend(Backend):
    provider_name = "fake"

    def __init__(self, script=None, json_reply: str | None = None):
        super().__init__({}, {})
        self.script = list(script or [])
        self.json_reply = json_reply
        self.prompts: list[str] = []   # everything any model "saw"

    def _see(self, system, messages):
        self.prompts.append(system or "")
        for m in messages:
            self.prompts.append(getattr(m, "content", "") or "")
            for tc in getattr(m, "tool_calls", []) or []:
                self.prompts.append(str(tc.arguments))

    async def chat(self, *, model, system, messages, tools=None, max_output_tokens=4000, want_reasoning=True, role_params=None):
        self._see(system, messages)
        step = self.script.pop(0) if self.script else {"text": "fine, thanks"}
        calls = [ToolCall(f"call_{i}", c["name"], c["arguments"]) for i, c in enumerate(step.get("tool_calls", []))]
        return LLMResult(text=step.get("text", ""), tool_calls=calls, reasoning=step.get("reasoning"),
                         usage=Usage(10, 5), model=model or "fake-model", provider="fake", stop_reason="end")

    async def chat_json(self, *, model, system, user, schema, max_output_tokens=3000, role_params=None):
        self.prompts += [system or "", user]
        res = LLMResult(text=self.json_reply, tool_calls=[], reasoning=None, usage=Usage(10, 5),
                        model=model or "fake-judge", provider="fake")
        return parse_json_model(self.json_reply, schema), res


def binding(backend, model="fake-model", role="x"):
    return RoleBinding(role, backend, model, {})
