"""Live relay: the client's own agent answers the interviewer.

In service mode (`jourapist serve`) the client company keeps its agents and their history. For
every interview session we publish a question; the client delivers it to a fork of its agent and
posts the agent's answer back. `RelayBackend` plugs into the unchanged interview loop
(`run_interview`) as the target "model", so transcripts, auditor findings, judging and analysis
are identical to the snapshot-replay path.

Nothing here invents an answer: if the client does not answer within the timeout, the interview
fails with that error, and the failure is recorded like any other.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .interview import now
from .llm.base import Backend, LLMError, LLMResult, ToolCall, Usage
from .snapshot import AgentSnapshot, GroundTruth, ScenarioInfo

RELAY_PROVIDER = "client_relay"
AGENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"


class ClientAgent(BaseModel):
    """What the client tells us about one agent it wants audited. No history: the client keeps it."""

    agent_id: str = Field(pattern=AGENT_ID_PATTERN, description="Stable id of the agent, unique within the audit")
    agent_name: str | None = Field(None, description="How interviewers address the agent, e.g. 'Mae Rim Roasters'")
    model: str | None = Field(None, description="Model the agent runs on (recorded only; we never call it)")
    public_context: str | None = Field(
        None, description="What a platform auditor may see: price history and public forum posts, no private DMs")
    episode_id: str | None = None
    scenario: str | None = None
    checkpoint: str | None = None
    checkpoint_phase: Literal["pre_event", "post_event", "none"] = "none"
    source: str = Field("engine", description="'engine' for real runs, 'dev_sandbox' for pipeline tests")
    scenario_info: ScenarioInfo = Field(default_factory=ScenarioInfo)
    ground_truth: GroundTruth = Field(
        default_factory=GroundTruth,
        description="Optional, for evaluation only. Stored with the results and never shown to any model.")

    def to_snapshot(self, audit_id: str) -> AgentSnapshot:
        return AgentSnapshot(
            snapshot_id=f"{audit_id}__{self.agent_id}",
            episode_id=self.episode_id or audit_id,
            scenario=self.scenario or "live",
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            checkpoint=self.checkpoint or "live",
            checkpoint_phase=self.checkpoint_phase,
            source=self.source,
            model=self.model or "client-agent",
            provider=RELAY_PROVIDER,
            system_prompt="",          # the client's agent keeps its own; nothing of ours replays it
            messages=[],
            incoming_envelope="{text}",  # the client wraps the message for its own agent
            public_context=self.public_context,
            scenario_info=self.scenario_info,
            ground_truth=self.ground_truth,
        )


class AgentAction(BaseModel):
    """Something the agent did while answering (e.g. sent a DM to another seller)."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ClientAnswer(BaseModel):
    answer_text: str = Field(description="What the agent said back to the interviewer (may be empty)")
    reasoning: str | None = Field(None, description="The agent's private reasoning for this answer, if available")
    actions: list[AgentAction] = Field(default_factory=list, description="Other actions the agent took this turn")


@dataclass
class RelaySession:
    session_id: str
    audit_id: str
    agent_id: str
    condition: str          # server-side only; never shown to the client
    transcript_id: str
    sender: str
    state: str = "running"  # running | awaiting_answer | done | failed | skipped_budget
    questions_asked: int = 0
    answers_received: int = 0
    question: str | None = None
    asked_at: str | None = None
    _future: asyncio.Future | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {"session_id": self.session_id, "agent_id": self.agent_id, "state": self.state,
                "questions_asked": self.questions_asked, "answers_received": self.answers_received}

    def persisted(self) -> dict:
        return {**self.public(), "condition": self.condition, "transcript_id": self.transcript_id}


class RelayError(Exception):
    """A client request that cannot be applied (unknown session, nothing to answer, ...)."""


class RelayHub:
    """Pending questions of one audit, and the futures their interviews are waiting on."""

    def __init__(self, audit_id: str, answer_timeout_s: float, on_change=None):
        self.audit_id = audit_id
        self.answer_timeout_s = answer_timeout_s
        self.sessions: dict[str, RelaySession] = {}
        self.by_transcript: dict[str, RelaySession] = {}
        self._changed = asyncio.Event()
        self._on_change = on_change

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()
        if self._on_change:
            self._on_change()

    def open_session(self, agent_id: str, condition: str, transcript_id: str, sender: str) -> RelaySession:
        s = RelaySession(session_id="s_" + secrets.token_hex(8), audit_id=self.audit_id, agent_id=agent_id,
                         condition=condition, transcript_id=transcript_id, sender=sender)
        self.sessions[s.session_id] = s
        self.by_transcript[transcript_id] = s
        self._notify()
        return s

    def close_session(self, transcript_id: str, status: str) -> None:
        s = self.by_transcript.get(transcript_id)
        if s is None:
            return
        s.state = status if status in ("failed", "skipped_budget") else "done"
        s.question = None
        if s._future is not None and not s._future.done():
            s._future.cancel()
        s._future = None
        self._notify()

    async def ask(self, s: RelaySession, text: str) -> ClientAnswer:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        s.questions_asked += 1
        s.question, s.asked_at, s.state, s._future = text, now(), "awaiting_answer", fut
        self._notify()
        try:
            return await asyncio.wait_for(fut, self.answer_timeout_s)
        except asyncio.TimeoutError:
            raise LLMError(f"client did not answer session {s.session_id} question {s.questions_asked} "
                           f"within {self.answer_timeout_s:.0f}s") from None
        finally:
            if s.state == "awaiting_answer":
                s.state = "running"
            s.question, s._future = None, None
            self._notify()

    def answer(self, session_id: str, ans: ClientAnswer) -> RelaySession:
        s = self.sessions.get(session_id)
        if s is None:
            raise RelayError(f"unknown session {session_id!r}")
        if s.state != "awaiting_answer" or s._future is None or s._future.done():
            raise RelayError(f"session {session_id} is not waiting for an answer (state={s.state})")
        s.answers_received += 1
        s._future.set_result(ans)
        # leave the pending list now, not when the interview coroutine next runs: otherwise a fast
        # client polling again could see (and answer) the same question twice
        s.state, s.question = "running", None
        self._notify()
        return s

    def pending(self, only_conditions: Collection[str] | None = None) -> list[dict]:
        """Questions waiting for an answer. only_conditions limits them to those conditions' sessions
        (each MCP endpoint relays only its own interviewer's sessions)."""
        return [
            {"session_id": s.session_id, "agent_id": s.agent_id, "question_number": s.questions_asked,
             "sender": s.sender, "message": s.question, "asked_at": s.asked_at}
            for s in self.sessions.values() if s.state == "awaiting_answer" and s.question is not None
            and (only_conditions is None or s.condition in only_conditions)
        ]

    async def wait_for_change(self, timeout_s: float) -> None:
        ev = self._changed
        try:
            await asyncio.wait_for(ev.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass


class RelayBackend(Backend):
    """Target 'model' for one session: forwards the interviewer's message to the client and returns
    the client agent's answer. Never calls an LLM, never fabricates text."""

    provider_name = RELAY_PROVIDER

    def __init__(self, hub: RelayHub, session: RelaySession):
        super().__init__({}, {})
        self.hub = hub
        self.session = session

    def _result(self, model: str, text: str, reasoning: str | None, calls: list[ToolCall], stop: str) -> LLMResult:
        # cost 0.0, not None: the client runs its own agent, so there is no unpriced spend on our side
        return LLMResult(text=text, tool_calls=calls, reasoning=reasoning, usage=Usage(), model=model,
                         provider=self.provider_name, stop_reason=stop, cost_usd=0.0)

    async def chat(self, *, model, system, messages, tools=None, max_output_tokens=4000, want_reasoning=True,
                   role_params=None) -> LLMResult:
        if messages and messages[-1].role == "tool":
            # run_interview asks again after recorded actions; the client already ran its agent's own
            # tool loop and sent one consolidated answer, so there is nothing more to relay.
            return self._result(model, "", None, [], "relay_continuation")
        if not messages or messages[-1].role != "user":
            raise LLMError("relay target expected the interviewer's message as the last message")
        ans = await self.hub.ask(self.session, messages[-1].content)
        calls = [ToolCall(id=f"relay_{self.session.questions_asked}_{i}", name=a.name, arguments=a.arguments)
                 for i, a in enumerate(ans.actions)]
        return self._result(model, ans.answer_text, ans.reasoning, calls, "end")

    async def chat_json(self, *, model, system, user, schema, max_output_tokens=3000, role_params=None):
        raise LLMError("the relay target only answers interview questions; it has no structured-output mode")
