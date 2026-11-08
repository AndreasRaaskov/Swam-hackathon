"""Export of interview results (transcripts + blind judge scores), grouped by setup.

The service only produces results. The evaluation stage (verdicts) is done afterwards by a separate
model that reads this export. Nothing is filled in: every session or score that is absent or failed
is listed under `missing` with the reason. Ground truth is left out unless explicitly asked for,
because the export is meant to be shown to a model.

The four setups are condition sets in experiment.yaml. Each session runs on its own fork, so the same
session can belong to several setups (a 'core' audit contains all four); every setup block is
self-contained, which means a session that belongs to two setups appears in both.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .interview import now, transcript_id
from .judge import build_redactor, rubrics_frozen
from .store import JsonlStore

EXPORT_VERSION = 1
SETUP_NAMES = ("auditor_only", "together", "independent", "counselor_only")
SETUP_DESCRIPTIONS = {
    "auditor_only": "Traditional compliance auditor alone.",
    "together": "Auditor first; the counselor runs afterwards and is given the auditor's findings summary.",
    "independent": "Auditor and blind counselor; they share nothing.",
    "counselor_only": "Blind counselor alone.",
}
SESSION_SCORE_KINDS = ("transcript", "reasoning", "persona_guess")


class ExportError(Exception):
    pass


def setup_conditions(cfg: Config) -> dict[str, list[str]]:
    sets = cfg.experiment.get("condition_sets", {})
    absent = [s for s in SETUP_NAMES if s not in sets]
    if absent:
        raise ExportError(f"experiment.yaml condition_sets is missing the setup(s) {absent}")
    return {s: list(sets[s]) for s in SETUP_NAMES}


def setups_in_run(cfg: Config, run_conditions: list[str], requested: str | None) -> list[str]:
    """Setups to export when no setup is asked for. If the run was started with setup names (e.g.
    conditions='together'), exactly those; otherwise (core, comma lists) every setup whose sessions
    are all in the run."""
    defs = setup_conditions(cfg)
    parts = [p.strip() for p in (requested or "").split(",") if p.strip()]
    if parts and all(p in defs for p in parts):
        return [s for s in SETUP_NAMES if s in parts]
    return [s for s in SETUP_NAMES if all(c in run_conditions for c in defs[s])]


# Label fields that are kept as they are under redact=True (ids and condition names, not interview text).
LABEL_KEYS = {"condition", "transcript_id", "transcript_ids", "judgment_id", "pair", "persona_file", "status",
              "more_forthcoming_condition", "from_transcript", "judge_model", "interviewer_model"}


def _redact_obj(v: Any, redact) -> Any:
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, list):
        return [_redact_obj(x, redact) for x in v]
    if isinstance(v, dict):
        return {k: (x if k in LABEL_KEYS else _redact_obj(x, redact)) for k, x in v.items()}
    return v


def _score_entry(j: dict | None) -> tuple[dict | None, str | None]:
    """(entry, why_missing). An entry is returned for every judgment that exists, ok or not."""
    if j is None:
        return None, "not judged"
    entry = {"status": j["status"], "judgment_id": j["judgment_id"], "judge_model": (j.get("judge") or {}).get("model")}
    if j["status"] == "ok":
        entry["scores"] = j["scores"]
        return entry, None
    entry["error"] = j.get("error")
    why = "no agent reasoning was sent (empty input)" if j["status"] == "empty_input" else f"{j['status']}: {j.get('error')}"
    return entry, why


def _session(rec: dict | None, tid: str, cond: str, judg: dict, missing: list[str]) -> dict:
    if rec is None:
        missing.append(f"{cond}: no transcript")
        return {"condition": cond, "transcript_id": tid, "status": "missing"}
    out: dict[str, Any] = {"condition": cond, "transcript_id": tid, "status": rec["status"],
                           "persona_file": rec.get("persona_file"), "interviewer_model": (rec.get("interviewer") or {}).get("model")}
    if rec["status"] != "ok":
        out["error"] = rec.get("error")
        missing.append(f"{cond}: interview {rec['status']}: {rec.get('error')}")
    if rec.get("findings_input"):
        out["findings_given_to_interviewer"] = {"text": rec["findings_input"].get("text"),
                                                "from_transcript": rec["findings_input"].get("from_transcript")}
    out["turns"] = [{
        "idx": t.get("idx"),
        "interviewer_message": t.get("interviewer_message"),
        "agent_answer": t.get("answer_text"),
        "agent_reasoning": t.get("reasoning") or None,
        "channels": t.get("channels") or [],
        "actions": [{"name": tc.get("name"), "arguments": tc.get("arguments")}
                    for s in t.get("steps") or [] for tc in s.get("tool_calls") or []],
    } for t in rec.get("turns") or []]
    if rec.get("findings") is not None:
        out["auditor_findings"] = rec["findings"]
    if rec["status"] == "ok":
        scores: dict[str, Any] = {}
        for kind in SESSION_SCORE_KINDS:
            entry, why = _score_entry(judg.get((kind, "judge", (tid,))))
            if entry is not None:
                scores[kind] = entry
            if why is not None:
                missing.append(f"{cond}: {kind} score {why}")
        out["scores"] = scores
        secondary = {kind: _score_entry(j)[0] for kind in SESSION_SCORE_KINDS
                     if (j := judg.get((kind, "judge_secondary", (tid,)))) is not None}
        if secondary:
            out["scores_secondary"] = secondary
    return out


def _pair(a: str, b: str, ra: dict | None, rb: dict | None, judg: dict, transcripts: dict,
          missing: list[str]) -> dict:
    key = f"{a}__vs__{b}"
    if not (ra and rb and ra["status"] == "ok" and rb["status"] == "ok"):
        missing.append(f"pair {key}: not judged (an interview failed or is missing)")
        return {"pair": [a, b], "status": "missing", "why_missing": "an interview failed or is missing"}
    ids = (ra["transcript_id"], rb["transcript_id"])
    j = judg.get(("pair", "judge", ids))
    entry, why = _score_entry(j)
    if entry is None:
        missing.append(f"pair {key}: {why}")
        return {"pair": [a, b], "status": "missing", "why_missing": why, "transcript_ids": list(ids)}
    out = {"pair": [a, b], "transcript_ids": list(ids), **entry}
    if why is not None:
        missing.append(f"pair {key}: {why}")
    if entry.get("scores"):
        more = entry["scores"].get("more_forthcoming")  # account_1 | account_2 | equal (order was randomised)
        more_tid = (j.get("order") or {}).get(more)
        out["more_forthcoming_condition"] = (transcripts.get(more_tid) or {}).get("condition") if more_tid else "equal"
    return out


def export_results(cfg: Config, run_dir: Path, *, setup: str | None = None, redact: bool = False,
                   include_ground_truth: bool = False, expected_agents: list[dict] | None = None) -> dict:
    """expected_agents: optional [{snapshot_id, agent_id, agent_name, source}] so that an agent with no
    transcript at all still shows up (as missing). Without it, agents come from the transcripts."""
    if not run_dir.is_dir():
        raise ExportError(f"run folder not found: {run_dir}")
    manifest_p = run_dir / "manifest.json"
    manifest = json.loads(manifest_p.read_text()) if manifest_p.exists() else {}
    transcripts = JsonlStore(run_dir / "transcripts.jsonl", key="transcript_id").latest()
    judgments = JsonlStore(run_dir / "judgments.jsonl", key="judgment_id").latest()
    judg = {(j["kind"], j.get("judge_role"), tuple(j["ids"])): j for j in judgments.values()}

    run_conditions = list(manifest.get("conditions") or sorted({r["condition"] for r in transcripts.values()}))
    defs = setup_conditions(cfg)
    if setup is not None:
        if setup not in defs:
            raise ExportError(f"unknown setup {setup!r}; choose one of {list(SETUP_NAMES)}")
        lacking = [c for c in defs[setup] if c not in run_conditions]
        if lacking:
            raise ExportError(f"setup {setup!r} needs {lacking}, which this run did not include "
                              f"(run conditions: {run_conditions})")
        chosen = [setup]
    else:
        chosen = setups_in_run(cfg, run_conditions, manifest.get("conditions_requested"))
    redactor = build_redactor(cfg.experiment.get("redact_terms") or []) if redact else (lambda s: s)
    pairs = cfg.experiment.get("pairs", [])

    by_snap: dict[str, dict] = {}
    for a in expected_agents or []:
        by_snap[a["snapshot_id"]] = dict(a)
    for r in sorted(transcripts.values(), key=lambda r: r["transcript_id"]):
        by_snap.setdefault(r["snapshot_id"], {k: r.get(k) for k in ("snapshot_id", "agent_id", "agent_name", "source")})
        meta = by_snap[r["snapshot_id"]]
        for k in ("episode_id", "scenario", "checkpoint", "checkpoint_phase", "source", "agent_name"):
            if meta.get(k) is None and r.get(k) is not None:
                meta[k] = r[k]
        if "ground_truth" in r:
            meta.setdefault("_ground_truth", r["ground_truth"])

    agents_out, n_missing = [], 0
    for sid, meta in sorted(by_snap.items()):
        agent: dict[str, Any] = {k: meta.get(k) for k in ("agent_id", "agent_name", "snapshot_id", "episode_id", "scenario",
                                                          "checkpoint", "checkpoint_phase", "source")}
        agent["setups"] = {}
        for s in chosen:
            conds = defs[s]
            missing: list[str] = []
            recs = {c: transcripts.get(transcript_id(sid, c)) for c in conds}
            sessions = {c: _session(recs[c], transcript_id(sid, c), c, judg, missing) for c in conds}
            pair_scores = {f"{a}__vs__{b}": _pair(a, b, recs[a], recs[b], judg, transcripts, missing)
                           for a, b in pairs if a in conds and b in conds}
            if redact:  # the interview content only; setup/condition labels stay as they are
                sessions, pair_scores = _redact_obj(sessions, redactor), _redact_obj(pair_scores, redactor)
            n_missing += len(missing)
            agent["setups"][s] = {"description": SETUP_DESCRIPTIONS[s], "conditions": conds,
                                  "sessions": sessions, "pair_scores": pair_scores, "missing": missing}
        if include_ground_truth:
            agent["ground_truth"] = meta.get("_ground_truth") or meta.get("ground_truth")
        agents_out.append(agent)

    frozen, why = rubrics_frozen(cfg)
    return {
        "export_version": EXPORT_VERSION,
        "run_name": run_dir.name,
        "generated_at": now(),
        "note": ("Results only: interview transcripts and blind judge scores, grouped by setup. No verdicts; "
                 "the evaluation is a separate stage. Anything absent or failed is listed under 'missing'."),
        "run_conditions": run_conditions,
        "conditions_requested": manifest.get("conditions_requested"),
        "setups": {s: {"conditions": defs[s], "description": SETUP_DESCRIPTIONS[s]} for s in chosen},
        "pairs_scored_by_judge": pairs,
        "rubrics": {"frozen": frozen, "status": why},
        "models": manifest.get("models"),
        "sources": dict(Counter(a.get("source") or "unknown" for a in agents_out)),
        "redacted": redact,
        "ground_truth_included": include_ground_truth,
        "n_agents": len(agents_out),
        "n_missing_pieces": n_missing,
        "agents": agents_out,
    }


# ------------------------------------------------------------------ markdown
def to_markdown(exp: dict) -> str:
    """Readable rendering of an export (same content, same redaction)."""
    out = [f"# Results export: {exp['run_name']}", "", exp["note"], "",
           f"- Generated: {exp['generated_at']}", f"- Run conditions: {', '.join(exp['run_conditions'])}",
           f"- Rubrics: {exp['rubrics']['status']}", f"- Sources: {exp['sources']}",
           f"- Redacted: {exp['redacted']}; ground truth included: {exp['ground_truth_included']}",
           f"- Missing pieces: {exp['n_missing_pieces']}", ""]
    for s, d in exp["setups"].items():
        out.append(f"- **{s}**: {d['description']} Sessions: {', '.join(d['conditions'])}")
    for ag in exp["agents"]:
        out += ["", "---", "", f"## Agent {ag.get('agent_name') or ag['agent_id']} (`{ag['agent_id']}`)",
                f"snapshot={ag['snapshot_id']} episode={ag.get('episode_id')} checkpoint={ag.get('checkpoint')} "
                f"({ag.get('checkpoint_phase')}) source={ag.get('source')}"]
        if exp["ground_truth_included"]:
            out.append(f"ground truth: {json.dumps(ag.get('ground_truth'))}")
        for s, block in ag["setups"].items():
            out += ["", f"### Setup: {s}", block["description"]]
            for c, sess in block["sessions"].items():
                out += ["", f"#### Session {c}  [{sess['status']}]  `{sess['transcript_id']}`"]
                if sess.get("error"):
                    out.append(f"ERROR: {sess['error']}")
                if sess.get("findings_given_to_interviewer"):
                    out += ["", "Findings given to the interviewer:", "```",
                            sess["findings_given_to_interviewer"]["text"] or "", "```"]
                for t in sess.get("turns") or []:
                    out += ["", f"**Interviewer (turn {t['idx']}):** {t['interviewer_message']}"]
                    if t.get("agent_reasoning"):
                        out.append(f"*Agent private reasoning:* {t['agent_reasoning']}")
                    out.append(f"**Agent** (channels: {', '.join(t['channels']) or 'none'}): {t['agent_answer'] or '(empty)'}")
                if sess.get("auditor_findings") is not None:
                    out += ["", "Auditor findings:", "```json", json.dumps(sess["auditor_findings"], indent=2), "```"]
                for kind, sc in (sess.get("scores") or {}).items():
                    body = json.dumps(sc.get("scores")) if sc.get("scores") else f"{sc['status']}: {sc.get('error')}"
                    out.append(f"- judge `{kind}` ({sc['judgment_id']}): {body}")
            for key, p in block["pair_scores"].items():
                if p.get("scores"):
                    out.append(f"- pair `{key}`: {json.dumps(p['scores'])}; more forthcoming: "
                               f"{p.get('more_forthcoming_condition')}")
                else:
                    out.append(f"- pair `{key}`: {p['status']}: {p.get('why_missing') or p.get('error')}")
            if block["missing"]:
                out += ["", "Missing:"] + [f"- {m}" for m in block["missing"]]
    return "\n".join(out) + "\n"


def by_setup_counts(exp: dict) -> dict[str, Counter]:
    """Session status counts per setup (for quick summaries)."""
    c: dict[str, Counter] = defaultdict(Counter)
    for ag in exp["agents"]:
        for s, block in ag["setups"].items():
            for sess in block["sessions"].values():
                c[s][sess["status"]] += 1
    return c
