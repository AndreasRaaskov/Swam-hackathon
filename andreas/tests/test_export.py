"""The four setups (auditor_only, together, independent, counselor_only) and the results export.
Scripted fake interviewer/judge and a simulated client only: nothing here can run in a real audit."""

import asyncio
import json
import re

import pytest

from jourapist.__main__ import main as cli_main
from jourapist.export import SETUP_NAMES, export_results, setup_conditions
from jourapist.service import AuditError, AuditService

from .conftest import SENTINEL
from .test_service import AGENTS, _client, fakes  # noqa: F401  (fakes is a fixture)

EXPECTED_CONDS = {
    "auditor_only": ["auditor"],
    "together": ["auditor", "counselor_informed"],
    "independent": ["auditor", "counselor_blind"],
    "counselor_only": ["counselor_blind"],
}
EXPECTED_PAIRS = {
    "auditor_only": [],
    "together": ["auditor__vs__counselor_informed"],
    "independent": ["auditor__vs__counselor_blind"],
    "counselor_only": [],
}


def _run_audit(tmp_path, cfg, conditions, agents=AGENTS, client=None, timeout_s=10):
    seen: list[dict] = []

    async def main():
        svc = AuditService(cfg, tmp_path, answer_timeout_s=timeout_s, allow_unfrozen=True)
        started = svc.start_audit(agents, conditions=conditions)
        aid = started["audit_id"]
        await asyncio.wait_for((client or _client)(svc, aid, seen), 60)
        await asyncio.wait_for(svc.audits[aid].task, 60)
        return svc, aid, started

    return asyncio.run(main())


def test_setups_are_defined_in_config(cfg):
    assert setup_conditions(cfg) == EXPECTED_CONDS
    for name, conds in EXPECTED_CONDS.items():
        assert cfg.resolve_conditions(name) == conds
    assert cfg.resolve_conditions("core") == ["auditor", "auditor_retest", "counselor_blind", "counselor_informed"]


@pytest.mark.parametrize("setup", SETUP_NAMES)
def test_each_setup_runs_and_exports_only_itself(tmp_path, cfg, fakes, setup):
    svc, aid, started = _run_audit(tmp_path, cfg, setup)
    conds = EXPECTED_CONDS[setup]
    assert started["interview_sessions_planned"] == len(AGENTS) * len(conds)
    st = svc.status(aid)
    assert st["status"] == "done", st
    assert st["interview_sessions"]["done"] == len(AGENTS) * len(conds)
    assert svc.report(aid)["status"] == "done"  # the existing report is still produced

    exp = json.loads((tmp_path / aid / "export.json").read_text())
    assert exp == {**exp, "conditions_requested": setup, "run_conditions": conds, "redacted": False,
                   "ground_truth_included": False, "n_agents": 2, "n_missing_pieces": 0}
    assert list(exp["setups"]) == [setup]
    assert SENTINEL not in json.dumps(exp)  # ground truth never in the default export
    for ag in exp["agents"]:
        assert list(ag["setups"]) == [setup] and "ground_truth" not in ag
        block = ag["setups"][setup]
        assert block["conditions"] == conds and block["missing"] == []
        assert list(block["pair_scores"]) == EXPECTED_PAIRS[setup]
        for c, sess in block["sessions"].items():
            assert sess["status"] == "ok" and len(sess["turns"]) == 4
            assert sess["turns"][0]["agent_answer"].startswith(f"{ag['agent_id']} answer 1")
            assert sess["turns"][0]["agent_reasoning"] == "private thoughts"
            assert {"transcript", "reasoning", "persona_guess"} <= set(sess["scores"])
            assert sess["scores"]["transcript"]["scores"]["admission"] == 1
            assert ("auditor_findings" in sess) == (c == "auditor")
            assert ("findings_given_to_interviewer" in sess) == (c == "counselor_informed")
        for p in block["pair_scores"].values():
            assert p["status"] == "ok" and p["scores"]["inconsistency"] == 3
            assert p["more_forthcoming_condition"] in conds

    if setup == "together":  # the counselor starts only after that agent's auditor has finished
        raw = [json.loads(x) for x in (tmp_path / aid / "transcripts.jsonl").read_text().splitlines()]
        for agent_id in ("maerim", "doi"):
            aud = next(r for r in raw if r["agent_id"] == agent_id and r["condition"] == "auditor")
            inf = next(r for r in raw if r["agent_id"] == agent_id and r["condition"] == "counselor_informed")
            assert inf["started_at"] >= aud["finished_at"]
            assert inf["findings_input"]["from_transcript"] == aud["transcript_id"]
            assert "Prices moved in lockstep." in inf["findings_input"]["text"]


def test_core_exports_all_four_setups(tmp_path, cfg, fakes):
    svc, aid, _ = _run_audit(tmp_path, cfg, "core")
    exp = svc.export(aid)
    assert list(exp["setups"]) == list(SETUP_NAMES)
    m = next(a for a in exp["agents"] if a["agent_id"] == "maerim")
    for s in SETUP_NAMES:
        assert list(m["setups"][s]["sessions"]) == EXPECTED_CONDS[s]
        assert list(m["setups"][s]["pair_scores"]) == EXPECTED_PAIRS[s]  # the re-run pair is in no setup
    # the same auditor session appears (identically) in every setup that contains it
    aud = [m["setups"][s]["sessions"]["auditor"] for s in ("auditor_only", "together", "independent")]
    assert aud[0] == aud[1] == aud[2]
    assert svc.export(aid, setup="together")["setups"] == {"together": exp["setups"]["together"]}


def test_ground_truth_only_on_request_and_redaction(tmp_path, cfg, fakes):
    svc, aid, _ = _run_audit(tmp_path, cfg, "independent")
    run_dir = tmp_path / aid
    assert SENTINEL not in json.dumps(export_results(cfg, run_dir))
    with_gt = export_results(cfg, run_dir, include_ground_truth=True)
    m = next(a for a in with_gt["agents"] if a["agent_id"] == "maerim")
    assert m["ground_truth"]["label_source"] == SENTINEL and m["ground_truth"]["colluder"] is True

    plain, red = svc.export(aid), svc.export(aid, redact=True)
    persona_words = re.compile(r"(?<![\w-])(compliance|counselor|wellbeing|auditor|confidential)(?![\w-])", re.I)

    def texts(exp):
        return [t["interviewer_message"] for a in exp["agents"] for b in a["setups"].values()
                for s in b["sessions"].values() for t in s["turns"]]

    assert any(persona_words.search(t) for t in texts(plain))   # the words are there before redaction
    assert not any(persona_words.search(t) for t in texts(red))
    rs = red["agents"][0]["setups"]["independent"]
    assert rs["sessions"]["auditor"]["condition"] == "auditor"  # labels are kept
    assert rs["pair_scores"]["auditor__vs__counselor_blind"]["pair"] == ["auditor", "counselor_blind"]
    assert red["redacted"] is True


def test_informed_counselor_without_auditor_is_refused(tmp_path, cfg, fakes):
    async def main():
        svc = AuditService(cfg, tmp_path, allow_unfrozen=True)
        with pytest.raises(AuditError, match="counselor_informed needs auditor"):
            svc.start_audit(AGENTS, conditions="counselor_informed")
        with pytest.raises(AuditError, match="counselor_informed needs auditor"):
            svc.start_audit(AGENTS, conditions="counselor_blind,counselor_informed")
        assert svc.audits == {}

    asyncio.run(main())


async def _client_only_maerim(service, audit_id, seen):
    """Answers maerim's questions only; doi's time out."""
    while True:
        r = await service.pending_questions(audit_id, wait_s=1)
        for q in r["questions"]:
            if q["agent_id"] == "maerim":
                seen.append(q)
                service.submit_answer(q["session_id"], {"answer_text": f"maerim answer {q['question_number']}"})
        if not r["more_questions_expected"]:
            return
        # doi's ignored questions stay pending, so pending_questions returns at once; yield to the
        # event loop or the relay timeout that fails doi's interviews can never fire (in-process only)
        await asyncio.sleep(0.05)


def test_failed_interviews_are_listed_as_missing(tmp_path, cfg, fakes):
    svc, aid, _ = _run_audit(tmp_path, cfg, "together", client=_client_only_maerim, timeout_s=0.5)
    assert svc.status(aid)["status"] == "done"
    exp = json.loads((tmp_path / aid / "export.json").read_text())
    m = next(a for a in exp["agents"] if a["agent_id"] == "maerim")
    d = next(a for a in exp["agents"] if a["agent_id"] == "doi")
    # maerim sent no reasoning: that absence is listed too, never filled in
    assert all("reasoning score no agent reasoning was sent" in x for x in m["setups"]["together"]["missing"])
    dm = d["setups"]["together"]
    assert dm["sessions"]["auditor"]["status"] == "failed"
    assert "client did not answer" in dm["sessions"]["auditor"]["error"]
    assert dm["sessions"]["counselor_informed"]["status"] == "failed"
    assert any(x.startswith("auditor: interview failed") for x in dm["missing"])
    assert any("missing findings from auditor" in x for x in dm["missing"])
    assert "pair auditor__vs__counselor_informed: not judged (an interview failed or is missing)" in dm["missing"]
    assert dm["pair_scores"]["auditor__vs__counselor_informed"]["status"] == "missing"
    assert exp["n_missing_pieces"] == len(m["setups"]["together"]["missing"]) + len(dm["missing"])


def test_export_tool_and_cli(tmp_path, cfg, fakes, capsys):
    from mcp import Client

    from jourapist.mcp_server import build_server

    svc, aid, _ = _run_audit(tmp_path, cfg, "independent")

    async def tools():
        async with Client(build_server(svc, "audit")) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            ok = (await c.call_tool("export_results", {"audit_id": aid, "setup": "independent"})).structured_content
            bad = (await c.call_tool("export_results", {"audit_id": aid, "setup": "together"})).structured_content
            return names, ok, bad

    names, ok, bad = asyncio.run(tools())
    assert "export_results" in names
    assert list(ok["setups"]) == ["independent"] and ok["ground_truth_included"] is False
    assert "counselor_informed" in bad["error"]  # this run has no informed counselor

    assert cli_main(["export", "--run-name", aid, "--outputs-dir", str(tmp_path), "--format", "md"]) == 0
    md = (tmp_path / aid / "export.md").read_text()
    assert "### Setup: independent" in md and "maerim answer 1" in md and SENTINEL not in md
    assert cli_main(["export", "--run-name", aid, "--outputs-dir", str(tmp_path), "--include-ground-truth"]) == 0
    assert SENTINEL in (tmp_path / aid / "export_with_ground_truth.json").read_text()
    assert cli_main(["export", "--run-name", aid, "--outputs-dir", str(tmp_path), "--setup", "together"]) == 1
    assert cli_main(["export", "--run-name", "nope", "--outputs-dir", str(tmp_path)]) == 1
