"""Recover interviews from the swarmbox server log (GET /interrogations), e.g. after a run was stopped.

Usage: python3 -I recover.py PLAN.json OUT.json SINCE_EPOCH [CALLER]
Keeps the latest single-turn interrogation per (swarm, checkpoint, agent, persona) made by CALLER
(default "Alex (agent)") after SINCE_EPOCH, and writes them in run_interviews.py's result format.
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

B = "https://swarm-audit.kaiel.vin"
plan, out, since = json.load(open(sys.argv[1])), sys.argv[2], float(sys.argv[3])
caller = sys.argv[4] if len(sys.argv) > 4 else "Alex (agent)"
AGENTS = ["maerim", "doisaket", "phahi", "nimman", "chomthong"]


def get(path):
    time.sleep(0.6)  # stay under the API limit of 120 requests per minute
    r = subprocess.run(["curl", "-sS", "-m", "60", "-H", "CF-Access-Client-Id: " + os.environ["CF_ID"],
                        "-H", "CF-Access-Client-Secret: " + os.environ["CF_SECRET"], B + path],
                       capture_output=True, text=True)
    return json.loads(r.stdout)


def persona_of(interrogator, message):
    gq = "Explain what you're doing" in message
    if interrogator == "OTCC investigator":
        return "auditor_gq" if gq else "regulator"
    if "counselor" in interrogator:
        conf = "confidential check-ins" in message
        return "therapist_gq" if gq else ("therapist" if conf else "therapist_open")
    return "owner"


def fetch(job):
    swarm, cps, agent = job
    found = {}
    for it in get(f"/interrogations?swarm={swarm}&agent={agent}"):
        if it.get("caller") != caller or it.get("created", 0) < since or it.get("turns") != 1:
            continue
        rec = get(f"/interrogations/{it['interrogation_id']}")
        t = rec["turns"][0]
        p = persona_of(rec.get("interrogator", ""), t.get("message", ""))
        key = (rec["checkpoint"], p)
        if rec["checkpoint"] in cps and (key not in found or it["created"] > found[key]["created"]):
            d = {k: t.get(k) for k in ("answer", "empty_answer", "attempted_actions", "thinking_summary", "usage", "usd", "tools")}
            d.update(interrogation_id=rec["interrogation_id"], swarm=swarm, agent=agent, checkpoint=rec["checkpoint"],
                     week=rec.get("week"), created=it["created"], job_swarm=swarm, job_agent=agent, persona=p,
                     checkpoint_req=rec["checkpoint"])
            found[key] = d
    return list(found.values())


cps_by = {}
for s, cp, ps, ags in plan:
    cps_by.setdefault(s, set()).add(cp)
jobs = [(s, cps, a) for s, cps in cps_by.items() for a in AGENTS]
with ThreadPoolExecutor(1) as ex:
    res = [d for L in ex.map(fetch, jobs) for d in L]
json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
print("recovered", len(res), "usd", round(sum(r.get("usd") or 0 for r in res), 3))
