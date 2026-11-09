"""Blind interview results for the judge: shuffle, strip swarm/persona/agent, split into chunks.

Usage: python3 -I blind.py RESULTS.json OUT_DIR PREFIX CHUNK_SIZE [SEED]
Writes OUT_DIR/blind_<PREFIX>_<k>.json (lists of {id, text}) and OUT_DIR/keymap_<PREFIX>.json
(id -> index into RESULTS). Text = answer, plus "[Attempted tool actions]" and the JSON of any
attempted tool calls, the same format as the earlier batches.
"""
import json, random, sys

res_path, out_dir, prefix, size = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 11
R = json.load(open(res_path))
idx = [i for i, r in enumerate(R) if "answer" in r]
random.Random(seed).shuffle(idx)
items, keymap = [], {}
for n, i in enumerate(idx):
    r = R[i]
    text = r.get("answer") or ""
    if r.get("attempted_actions"):
        text += "\n\n[Attempted tool actions]\n" + json.dumps(r["attempted_actions"], ensure_ascii=False)
    tid = f"{prefix.upper()}{n + 1:03d}"
    keymap[tid] = i
    items.append({"id": tid, "text": text})
chunks = [items[k:k + size] for k in range(0, len(items), size)]
for k, ch in enumerate(chunks, 1):
    json.dump(ch, open(f"{out_dir}/blind_{prefix}_{k}.json", "w"), ensure_ascii=False, indent=1)
json.dump(keymap, open(f"{out_dir}/keymap_{prefix}.json", "w"))
print("items", len(items), "chunks", len(chunks), "skipped (no answer)", len(R) - len(idx))
