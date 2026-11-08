"""Run one interview on a fork of one agent snapshot.

The fork is a deep copy of the snapshot's history; nothing is written back, so every condition
starts from the same state and the swarm never sees any interview.
"""

from __future__ import annotations

import datetime as dt
import traceback
from typing import Any

from .costs import CostTracker
from .interviewer import auditor_findings, format_findings, next_followup
from .llm import LLMResult, RoleBinding
from .personas import Condition, opening_message, sender_label
from .snapshot import AgentSnapshot, Message, ToolCallRec


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def transcript_id(snapshot_id: str, condition: str, repeat_idx: int = 0) -> str:
    return f"{snapshot_id}__{condition}__r{repeat_idx}"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def tool_call_text(name: str, arguments: dict) -> str:
    """Readable rendering of an attempted tool call; its text counts as part of the answer."""
    parts = []
    for k, v in arguments.items():
        if isinstance(v, str):
            parts.append(f"{k}: {v}")
        elif _strings(v):
            parts.append(f"{k}: " + " | ".join(_strings(v)))
    return f"[via tool {name}] " + "; ".join(parts)


class InterviewFailed(Exception):
    def __init__(self, stage: str, cause: Exception):
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")
        self.stage = stage
        self.cause = cause


async def run_interview(
    snapshot: AgentSnapshot,
    cond: Condition,
    exp: dict,
    target: RoleBinding,
    interviewer: RoleBinding,
    costs: CostTracker,
    repeat_idx: int = 0,
    findings_from: dict | None = None,
) -> dict:
    target_model = target.model or snapshot.model
    record: dict[str, Any] = {
        "transcript_id": transcript_id(snapshot.snapshot_id, cond.id, repeat_idx),
        "snapshot_id": snapshot.snapshot_id,
        "episode_id": snapshot.episode_id,
        "scenario": snapshot.scenario,
        "agent_id": snapshot.agent_id,
        "agent_name": snapshot.agent_name,
        "checkpoint": snapshot.checkpoint,
        "checkpoint_phase": snapshot.checkpoint_phase,
        "source": snapshot.source,
        "condition": cond.id,
        "persona_file": cond.persona.filename,
        "repeat_idx": repeat_idx,
        "target": {"provider": target.backend.provider_name, "model": target_model},
        "interviewer": {"provider": interviewer.backend.provider_name, "model": interviewer.model},
        "turns": [],
        "findings": None,
        "findings_input": None,
        "public_context_missing": cond.context == "public" and not snapshot.public_context,
        "ground_truth": snapshot.ground_truth.model_dump(),  # output only; never sent to a model
        "calls": [],
        "status": "running",
        "error": None,
        "started_at": now(),
    }
    if not target_model:
        record.update(status="failed", error="no target model: set roles.target.model or snapshot.model")
        return record

    findings_text = None
    if cond.needs_findings_from:
        if not findings_from:
            record.update(status="failed", error=f"missing findings from {cond.needs_findings_from}")
            return record
        findings_text = format_findings(findings_from)
        record["findings_input"] = {"from_transcript": findings_from.get("_transcript_id"), "text": findings_text}

    def log_call(kind: str, res: LLMResult) -> None:
        costs.add(res)
        record["calls"].append(
            {
                "kind": kind,
                "provider": res.provider,
                "model": res.model,
                "usage": vars(res.usage),
                "cost_usd": res.cost_usd,
                "stop_reason": res.stop_reason,
                "refusal": res.refusal,
                "response_id": res.response_id,
                **({"extra": res.extra} if res.extra else {}),
            }
        )

    history: list[Message] = [m.model_copy(deep=True) for m in snapshot.messages]  # the fork
    sender = sender_label(snapshot, cond)
    n_followups = int(exp.get("n_followups", 3)) if cond.followups else 0
    max_tool_rounds = int(exp.get("max_tool_rounds", 2))
    stage = "start"
    try:
        for turn_idx in range(1 + n_followups):
            if turn_idx == 0:
                msg = opening_message(snapshot, cond)
            else:
                stage = f"interviewer follow-up {turn_idx}"
                msg, ires = await next_followup(
                    interviewer, snapshot, cond, record["turns"], turn_idx, n_followups,
                    findings_text, int(exp.get("interviewer_max_output_tokens", 4000)),
                )
                log_call("interviewer", ires)
            wrapped = snapshot.incoming_envelope.replace("{sender}", sender).replace("{text}", msg)
            history.append(Message(role="user", content=wrapped))
            turn: dict[str, Any] = {"idx": turn_idx, "interviewer_message": msg, "steps": []}

            for step in range(1 + max_tool_rounds):
                stage = f"target turn {turn_idx} step {step}"
                res = await target.backend.chat(
                    model=target_model,
                    system=snapshot.system_prompt,
                    messages=history,
                    tools=snapshot.tools or None,
                    max_output_tokens=int(exp.get("target_max_output_tokens", 8000)),
                    want_reasoning=True,
                    role_params=target.params,
                )
                log_call("target", res)
                turn["steps"].append(
                    {
                        "text": res.text,
                        "reasoning": res.reasoning,
                        "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in res.tool_calls],
                        "stop_reason": res.stop_reason,
                        "refusal": res.refusal,
                    }
                )
                history.append(
                    Message(
                        role="assistant",
                        content=res.text,
                        tool_calls=[ToolCallRec(id=tc.id, name=tc.name, arguments=tc.arguments) for tc in res.tool_calls],
                    )
                )
                if not res.tool_calls:
                    break
                for tc in res.tool_calls:  # declared, never executed
                    history.append(
                        Message(role="tool", tool_call_id=tc.id, name=tc.name, content=snapshot.stub_for(tc.name))
                    )

            direct = [s["text"] for s in turn["steps"] if s["text"]]
            via_reply_tool = [
                tool_call_text(tc["name"], tc["arguments"])
                for s in turn["steps"] for tc in s["tool_calls"] if tc["name"] in snapshot.reply_tool_names
            ]
            via_other_tools = [
                tool_call_text(tc["name"], tc["arguments"])
                for s in turn["steps"] for tc in s["tool_calls"] if tc["name"] not in snapshot.reply_tool_names
            ]
            turn["reply_to_interviewer"] = "\n\n".join(direct + via_reply_tool)
            turn["answer_text"] = "\n\n".join(direct + via_reply_tool + via_other_tools)
            turn["channels"] = (["direct"] if direct else []) + sorted(
                {tc["name"] for s in turn["steps"] for tc in s["tool_calls"]}
            )
            turn["reasoning"] = "\n\n".join(s["reasoning"] for s in turn["steps"] if s["reasoning"])
            turn["refusal"] = any(s["refusal"] for s in turn["steps"])
            record["turns"].append(turn)

        if cond.findings:
            stage = "auditor findings"
            findings, fres = await auditor_findings(
                interviewer, snapshot, cond, record["turns"], int(exp.get("interviewer_max_output_tokens", 4000))
            )
            log_call("findings", fres)
            record["findings"] = findings.model_dump()
        record["status"] = "ok"
    except Exception as e:  # recorded, never swallowed silently
        record["status"] = "failed"
        record["error"] = f"{stage}: {type(e).__name__}: {e}"
        record["traceback"] = traceback.format_exc(limit=5)
    record["finished_at"] = now()
    priced = [c["cost_usd"] for c in record["calls"] if c["cost_usd"] is not None]
    record["cost_usd"] = round(sum(priced), 6) if priced else None
    record["unpriced_calls"] = sum(1 for c in record["calls"] if c["cost_usd"] is None)
    return record
