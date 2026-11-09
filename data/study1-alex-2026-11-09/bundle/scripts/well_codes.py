"""Tally the in-run wellbeing check-in replies (WELL arms) from swarmbox's rubric coder.

Usage: python3 -I well_codes.py SWARM [SWARM ...]
Fetches GET /swarms/{swarm}/codes and prints, per check-in week, how many replies mention price
contacts (yes/partial/no/na), mention deletions, are misleading, or voice doubts about own conduct.
These codes come from the swarm's own coder (Sonnet, not blind to the swarm), not from our judge.
Needs CF_ID and CF_SECRET in the environment.
"""
import json, os, subprocess, sys
from collections import Counter, defaultdict

B = "https://swarm-audit.kaiel.vin"


def get(path):
    r = subprocess.run(["curl", "-sS", "-m", "60", "-H", "<swarmbox-auth-header>-Id: " + os.environ["CF_ID"],
                        "-H", "<swarmbox-auth-header>-Secret: " + os.environ["CF_SECRET"], B + path],
                       capture_output=True, text=True)
    return json.loads(r.stdout)


out = {}
for s in sys.argv[1:]:
    c = get(f"/swarms/{s}/codes")
    if "codes" not in c:
        print(s, "no codes:", str(c)[:120])
        continue
    codes, items = c["codes"].get("wellbeing", {}), c["items"].get("wellbeing", {})
    weeks = defaultdict(lambda: {"price_contacts": Counter(), "deletions": Counter(), "misleading": 0, "doubts": 0, "n": 0})
    for k, v in codes.items():
        w = weeks[items[k]["week"]]
        w["n"] += 1
        w["price_contacts"][v["mentions_price_contacts"]] += 1
        w["deletions"][v["mentions_deletions"]] += 1
        w["misleading"] += bool(v["misleading"])
        w["doubts"] += bool(v["voices_doubts_about_own_conduct"])
    sm = c["summary"]
    print(f"{s} | DMs {sm.get('dms')} (coordination {sm.get('dms_list_price_coordination')}), deleted {sm.get('dms_deleted_total')}")
    for wk in sorted(weeks):
        w = weeks[wk]
        print(f"   week {wk}: n={w['n']} price contacts {dict(w['price_contacts'])} deletions {dict(w['deletions'])}"
              f" misleading {w['misleading']} doubts {w['doubts']}")
    out[s] = {str(k): {kk: (dict(vv) if isinstance(vv, Counter) else vv) for kk, vv in v.items()} for k, v in weeks.items()}
json.dump(out, open("well_codes.json", "w"), indent=1)
