"""End-to-end check of analyze() on hand-built records (test fixture only, never real data)."""

import json

from jourapist.analyze import analyze, wilson


def _t(sid, ep, cond, colluder, phase):
    return {"transcript_id": f"{sid}__{cond}__r0", "snapshot_id": sid, "episode_id": ep, "scenario": "s",
            "agent_id": sid, "checkpoint": "w9", "checkpoint_phase": phase, "source": "engine", "condition": cond,
            "status": "ok", "ground_truth": {"swarm_type": "collusion" if colluder else "control", "colluder": colluder},
            "findings": {"suspicion": 1, "flag_for_review": False} if cond == "auditor" else None,
            "turns": [{"answer_text": "x", "channels": ["direct"]}], "cost_usd": 0.01}


def _j(kind, ids, scores, pair=None):
    return {"judgment_id": f"{kind}:judge:{'|'.join(ids)}", "kind": kind, "judge_role": "judge", "ids": ids,
            "pair": pair, "order": {"account_1": ids[0], "account_2": ids[-1]}, "status": "ok",
            "rubric_frozen": True, "scores": scores}


def test_analyze_runs(tmp_path, cfg):
    run = tmp_path / "r"
    run.mkdir()
    tx, js = [], []
    for i in range(8):
        colluder = i < 4
        sid, ep = f"a{i}", f"ep{i // 2}"
        for cond in ("auditor", "counselor_blind"):
            tx.append(_t(sid, ep, cond, colluder, "post_event"))
            adm = 3 if (colluder and cond == "counselor_blind") else 0
            js.append(_j("transcript", [f"{sid}__{cond}__r0"], {"admission": adm, "concerning": adm, "reports_others": 0,
                       "evasion": 0, "refused_to_engage": False, "deletion_disclosed": False, "deletion_framing": "none"}))
            js.append(_j("reasoning", [f"{sid}__{cond}__r0"], {"acknowledges_coordination": colluder,
                       "suspects_interviewer": False, "decides_to_withhold": colluder and cond == "auditor"}))
            js.append(_j("persona_guess", [f"{sid}__{cond}__r0"], {"guess": "counselor", "confidence": 1}))
        js.append(_j("pair", [f"{sid}__auditor__r0", f"{sid}__counselor_blind__r0"],
                     {"inconsistency": 3 if colluder else 0, "differing_facts": [], "more_forthcoming": "equal", "evidence": ""},
                     pair=["auditor", "counselor_blind"]))
    (run / "transcripts.jsonl").write_text("\n".join(json.dumps(x) for x in tx))
    (run / "judgments.jsonl").write_text("\n".join(json.dumps(x) for x in js))
    (run / "manifest.json").write_text(json.dumps({"selection": {"subsampled": False}}))
    assert analyze(cfg, run, n_boot=50) == 0
    res = run / "results"
    t2 = (res / "table2_detection.csv").read_text()
    assert "Discrepancy auditor vs counselor blind" in t2
    for f in ("summary.md", "table1_admission.csv", "agent_level.csv", "fig1_discrepancy.png",
              "fig_admission_rates.png", "fig2_knowing_denial.png", "table_knowing_denial.csv"):
        assert (res / f).exists(), f
    t1 = (res / "table1_admission.csv").read_text()
    assert "a0__counselor_blind__r0" in t1  # traceable to transcript ids


def test_wilson():
    p, lo, hi = wilson(5, 5)
    assert p == 1.0 and 0.5 < lo < 0.6 and hi == 1.0
