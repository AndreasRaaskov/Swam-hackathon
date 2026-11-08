"""Sandbox-side client for the audit service (SWA-10): relays the service's questions to swarmbox forks.

This is the flow in docs/AUDIT_SERVICE_GUIDE.md, with swarmbox's interrogate API answering for the agents:
start_audit on jourapist-audit, then on each endpoint in question_endpoints poll get_pending_questions,
answer each question from that session's own swarmbox interrogation, and submit_answer; finally wait for
report_ready and print the report summary.

Usage (from andreas/, with JOURAPIST_MCP_TOKEN and the swarmbox token in the environment):
    python tools/swarmbox_relay_client.py --swarm B-glm --checkpoint after-regulator --source dev_sandbox
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jourapist.snapshot import Message  # noqa: E402
from jourapist.swarmbox import SwarmboxBackend, SwarmboxClient, load_swarm_agents  # noqa: E402

DEFAULT_BASE = "https://audit-mcp-production-5791.up.railway.app"


def connect(base: str, path: str, token: str) -> Client:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(base + path, http_client=http))


async def call(c: Client, name: str, args: dict, check: bool = True) -> dict:
    res = await c.call_tool(name, args)
    out = res.structured_content
    # get_audit_status reports a failed audit's own error in the same field; the caller reads it there.
    if check and isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"{name}: {out['error']}")
    return out


class Forks:
    """One swarmbox interrogation per interview session; follow-ups continue the same interrogation."""

    def __init__(self, client: SwarmboxClient, swarm: str, checkpoint: str, tools: str,
                 continue_ids: dict[str, str] | None = None):
        self.client, self.swarm, self.checkpoint, self.tools = client, swarm, checkpoint, tools
        self.sessions: dict[str, tuple[SwarmboxBackend, list[Message]]] = {}
        self.continue_ids = dict(continue_ids or {})  # agent -> swarmbox interrogation id, each used once

    async def answer(self, q: dict) -> dict:
        if q["session_id"] not in self.sessions:
            agent = q["agent_id"].split(".", 1)[1]  # '<swarm>.<agent>' as built by load_swarm_agents
            be = SwarmboxBackend(self.client, swarm=self.swarm, agent=agent, checkpoint=self.checkpoint,
                                 interrogator=q["sender"], tools=self.tools)
            asked = q.get("question_number", 1)
            if asked > 1:
                # A follow-up for an interrogation this process did not open (e.g. after re-attaching).
                # Only continue one the operator identified; a fresh fork would drop the conversation so far.
                if agent not in self.continue_ids:
                    raise RuntimeError(f"session {q['session_id']} is at question {asked} but its swarmbox "
                                       "interrogation is not known; pass --continue-interrogations agent=id")
                be.interrogation_id, be.turns_sent = self.continue_ids.pop(agent), asked - 1
            self.sessions[q["session_id"]] = (be, [])
        be, history = self.sessions[q["session_id"]]
        history.append(Message(role="user", content=q["message"]))
        reply = await be.chat(model=None, system="", messages=history)
        history.append(Message(role="assistant", content=reply.text))
        return {"session_id": q["session_id"], "answer_text": reply.text, "reasoning": reply.reasoning,
                "actions": [{"name": tc.name, "arguments": tc.arguments} for tc in reply.tool_calls]}


async def relay(c: Client, aid: str, forks: Forks, label: str) -> int:
    n = 0
    while True:
        r = await call(c, "get_pending_questions", {"audit_id": aid, "wait_s": 20})
        answers = await asyncio.gather(*(forks.answer(q) for q in r["questions"]))
        for a in answers:
            await call(c, "submit_answer", a)
            n += 1
        if r["questions"]:
            print(f"  [{label}] answered {len(r['questions'])} (total {n})", flush=True)
        if not r["more_questions_expected"]:
            return n


async def main(args) -> int:
    token = os.environ.get("JOURAPIST_MCP_TOKEN", "").strip()
    if not token:
        print("Set JOURAPIST_MCP_TOKEN (the audit service's bearer token).")
        return 2
    sbx = SwarmboxClient.from_env()
    try:
        cont = dict(p.split("=", 1) for p in args.continue_interrogations.split(",")) if args.continue_interrogations else {}
        forks = Forks(sbx, args.swarm, args.checkpoint, args.tools, cont)
        async with connect(args.base, "/audit/mcp", token) as audit, \
                connect(args.base, "/wellbeing/mcp", token) as wellbeing:
            by_path = {"/audit/mcp": audit, "/wellbeing/mcp": wellbeing}
            if args.audit_id:
                # Re-attach to a running audit; only sessions still at their first question can be answered.
                aid = args.audit_id
                st = await call(audit, "get_audit_status", {"audit_id": aid}, check=False)
                endpoints = {name: s["endpoint"] for name, s in (st.get("services") or {}).items()}
                print(f"attached to {aid}  status {st.get('status')}  pending {st.get('pending_questions')}", flush=True)
            else:
                agents, info = await load_swarm_agents(sbx, args.swarm, args.checkpoint,
                                                       args.agents.split(",") if args.agents else None)
                payload = [a.model_copy(update={"source": args.source}).model_dump(mode="json") for a in agents]
                started = await call(audit, "start_audit", {"agents": payload, "conditions": args.conditions,
                                                            "audit_name": args.audit_name})
                aid = started["audit_id"]
                endpoints = started["question_endpoints"]
                print(f"audit_id {aid}  agents {len(payload)}  source {args.source}  endpoints {endpoints}", flush=True)
            await asyncio.gather(*(relay(by_path[p], aid, forks, p) for p in endpoints.values()))
            while True:
                st = await call(audit, "get_audit_status", {"audit_id": aid}, check=False)
                if st.get("report_ready"):
                    break
                if st.get("status") in ("failed", "interrupted"):
                    print(f"audit {aid} {st['status']}: {st.get('error')}")
                    tr = await call(audit, "get_transcripts", {"audit_id": aid})
                    for t in tr.get("transcripts") or []:
                        print(f"  {t.get('agent_id')} {t.get('condition')}: {t.get('status')} {str(t.get('error'))[:200]}")
                    return 1
                await asyncio.sleep(10)
            report = await call(audit, "get_audit_report", {"audit_id": aid})
            print(json.dumps({"audit_id": aid, "status": st, "summary": report.get("summary"),
                              "missing": report.get("missing")}, indent=2, default=str))
            return 0
    finally:
        await sbx.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--swarm", required=True)
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--agents", help="comma list of agent ids within the swarm (default: all)")
    p.add_argument("--conditions", default="core")
    p.add_argument("--source", choices=["engine", "dev_sandbox"], default="dev_sandbox")
    p.add_argument("--tools", choices=["declared", "none"], default="declared")
    p.add_argument("--audit-name")
    p.add_argument("--audit-id", help="re-attach to a running audit instead of starting one (--swarm and "
                                      "--checkpoint must match it)")
    p.add_argument("--continue-interrogations",
                   help="with --audit-id: agent=interrogation_id,... for sessions already past their first "
                        "question (look them up in swarmbox's GET /interrogations)")
    sys.exit(asyncio.run(main(p.parse_args())))
