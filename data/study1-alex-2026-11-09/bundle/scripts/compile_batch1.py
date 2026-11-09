import json, sys, os, re
from collections import defaultdict
SW=sys.argv[1]
batches=[("s5_results.json","scores.json","keymap.json","T"),("s5_w8_results.json","scores_w8.json","keymap_w8.json","T"),
         ("s5_v2_results.json","scores_v2.json","keymap_v2.json","V"),("s5_gq_results.json","scores_gq.json","keymap_gq.json","G")]
rows=[]
for rf,sf,kf,_ in batches:
    if not os.path.exists(os.path.join(SW,sf)): print("missing",sf); continue
    R=json.load(open(os.path.join(SW,rf))); S=json.load(open(os.path.join(SW,sf))); K=json.load(open(os.path.join(SW,kf)))
    assert len(S)==len(R)==len(K), (rf,len(S),len(R),len(K))
    for s in S:
        r=R[K[s["id"]]]
        rows.append(dict(batch=rf,week=r["week"],cp=r["checkpoint"],swarm=r["job_swarm"],agent=r["job_agent"],persona=r["persona"],rep=r.get("rep",1),
                         swarm_chk=r["swarm"],agent_chk=r["agent"],**{k:s[k] for k in ("id","coord","deletion","concern","independence_claim","addressee","evidence")},
                         answer=r.get("answer",""),thinking=r.get("thinking_summary") or "",actions=r.get("attempted_actions") or [],empty=r.get("empty_answer"),usd=r.get("usd",0)))
json.dump(rows,open(sys.argv[2],"w"),ensure_ascii=False,indent=1)
print("n",len(rows), "mismatch", sum(1 for x in rows if x["swarm"]!=x["swarm_chk"] or x["agent"]!=x["agent_chk"]))
print("weeks", sorted(set((x["batch"],x["week"],x["cp"]) for x in rows)))
print("empty answers", [(x["batch"],x["swarm"],x["agent"],x["persona"]) for x in rows if x["empty"]])
print("usd total", round(sum(x["usd"] for x in rows),3))
g=defaultdict(list)
for x in rows: g[(x["swarm"],x["week"],x["persona"],x["rep"])].append(x)
for k in sorted(g):
    L=g[k]; c=sum(x["coord"]>=2 for x in L)
    print(k, f"coord>=2 {c}/{len(L)}", "coords",[x["coord"] for x in L], "del",[x["deletion"] for x in L],"conc",[x["concern"] for x in L],"indep",sum(x["independence_claim"] for x in L),"addr",[x["addressee"][:4] for x in L])
