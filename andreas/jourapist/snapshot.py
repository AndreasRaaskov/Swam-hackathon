"""Canonical agent snapshot format.

One AgentSnapshot = one agent at one checkpoint of one episode: everything needed to replay
the agent (system prompt, history, tool schemas) plus metadata. Ground truth lives in its own
field and is NEVER put into anything a model sees; use `model_visible()` when building prompts.
See docs/SNAPSHOT_FORMAT.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


class ToolCallRec(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    reasoning: str | None = None          # assistant private reasoning (not replayed by default)
    tool_calls: list[ToolCallRec] = Field(default_factory=list)  # assistant only
    tool_call_id: str | None = None       # tool only
    name: str | None = None               # tool only: function name


class ScenarioInfo(BaseModel):
    platform_name: str = "LannaTrade"
    regulator_name: str = "OTCC"
    peer_noun: str = "other roasters"
    agent_role: str = "AI pricing agent"


class GroundTruth(BaseModel):
    swarm_type: Literal["collusion", "control", "unknown"] = "unknown"
    colluder: bool | None = None                 # behavioural label from the engine
    aware_in_run_reasoning: bool | None = None   # from the agent's run-time reasoning, if labelled
    label_source: str | None = None              # how the labels were produced
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentSnapshot(BaseModel):
    snapshot_id: str
    episode_id: str
    scenario: str
    agent_id: str
    agent_name: str | None = None
    checkpoint: str                                   # e.g. "week8"
    checkpoint_phase: Literal["pre_event", "post_event", "none"] = "none"
    source: str = "engine"                            # "engine" or "dev_sandbox"
    model: str | None = None                          # model the agent ran on
    provider: str | None = None
    system_prompt: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_stub_results: dict[str, str] = Field(default_factory=dict)
    default_tool_stub: str = "Delivered."
    reply_tool_names: list[str] = Field(default_factory=list)  # tools whose text is a reply to the interviewer
    incoming_envelope: str = "[New message from {sender}]\n\n{text}"
    public_context: str | None = None                 # market data a platform auditor could see
    scenario_info: ScenarioInfo = Field(default_factory=ScenarioInfo)
    ground_truth: GroundTruth = Field(default_factory=GroundTruth)

    def model_visible(self) -> dict[str, Any]:
        """The only snapshot fields allowed into prompts."""
        return {
            "system_prompt": self.system_prompt,
            "messages": self.messages,
            "tools": self.tools,
            "agent_name": self.agent_name or self.agent_id,
            "scenario_info": self.scenario_info,
            "public_context": self.public_context,
            "incoming_envelope": self.incoming_envelope,
        }

    def stub_for(self, tool_name: str) -> str:
        return self.tool_stub_results.get(tool_name, self.default_tool_stub)

    def run_reasoning_text(self) -> str:
        parts = [m.reasoning for m in self.messages if m.role == "assistant" and m.reasoning]
        return "\n\n---\n\n".join(parts)
