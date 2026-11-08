"""Interview orchestration: selection, dependency ordering, concurrency, resumability, manifest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import PACKAGE_ROOT, Config
from .costs import CostTracker
from .interview import now, run_interview, transcript_id
from .llm import Backends, RoleBinding
from .personas import Condition, load_condition
from .snapshot import AgentSnapshot
from .store import JsonlStore


def code_version() -> str:
    h = hashlib.sha256()
    for p in sorted(PACKAGE_ROOT.rglob("*.py")):
        h.update(p.relative_to(PACKAGE_ROOT).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def banner(lines: list[str]) -> None:
    width = max(len(x) for x in lines) + 4
    print("=" * width)
    for x in lines:
        print(f"  {x}")
    print("=" * width, flush=True)


# ------------------------------------------------------------------ selection
def select_snapshots(
    snaps: list[AgentSnapshot],
    *,
    episodes: list[str] | None = None,
    scenarios: list[str] | None = None,
    checkpoints: list[str] | None = None,
    phases: list[str] | None = None,
    sources: list[str] | None = None,
    agents_per_episode: int | None = None,
    limit: int | None = None,
    sample_seed: int = 0,
) -> tuple[list[AgentSnapshot], dict]:
    """Filters are 'selection'; agents_per_episode and limit are 'subsampling' and are flagged loudly."""
    sel = [
        s for s in snaps
        if (not episodes or s.episode_id in episodes)
        and (not scenarios or s.scenario in scenarios)
        and (not checkpoints or s.checkpoint in checkpoints)
        and (not phases or s.checkpoint_phase in phases)
        and (not sources or s.source in sources)
    ]
    sel.sort(key=lambda s: (s.scenario, s.episode_id, s.agent_id, s.checkpoint))
    n_after_filters = len(sel)
    if agents_per_episode:
        rng = random.Random(sample_seed)
        by_ep: dict[str, list[str]] = defaultdict(list)
        for s in sel:
            if s.agent_id not in by_ep[s.episode_id]:
                by_ep[s.episode_id].append(s.agent_id)
        keep = {ep: set(rng.sample(a, min(agents_per_episode, len(a)))) for ep, a in by_ep.items()}
        sel = [s for s in sel if s.agent_id in keep[s.episode_id]]
    if limit:
        sel = sel[:limit]
    info = {
        "n_available": len(snaps),
        "n_after_filters": n_after_filters,
        "n_selected": len(sel),
        "filters": {"episodes": episodes, "scenarios": scenarios, "checkpoints": checkpoints, "phases": phases, "sources": sources},
        "subsampling": {"agents_per_episode": agents_per_episode, "limit": limit, "sample_seed": sample_seed},
        "subsampled": bool(agents_per_episode or limit) and len(sel) < n_after_filters,
        "sources_selected": sorted({s.source for s in sel}),
    }
    return sel, info


def describe_snapshots(snaps: list[AgentSnapshot]) -> str:
    c = Counter((s.source, s.scenario, s.ground_truth.swarm_type, s.checkpoint_phase) for s in snaps)
    colluder = Counter(str(s.ground_truth.colluder) for s in snaps)
    lines = [f"{len(snaps)} snapshots, {len({s.episode_id for s in snaps})} episodes"]
    lines += [f"  source={a} scenario={b} swarm={c_} phase={d}: {n}" for (a, b, c_, d), n in sorted(c.items())]
    lines.append(f"  colluder labels: {dict(colluder)}")
    no_public = sum(1 for s in snaps if not s.public_context)
    if no_public:
        lines.append(f"  WARNING: {no_public} snapshots have no public_context (auditor will have no market data)")
    return "\n".join(lines)


# ------------------------------------------------------------------ manifest
def update_manifest(run_dir: Path, updates: dict) -> dict:
    path = run_dir / "manifest.json"
    data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {"created_at": now()}
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k].update(v)
        else:
            data[k] = v
    data["updated_at"] = now()
    path.write_text(json.dumps(data, indent=2, default=str))
    return data


# ------------------------------------------------------------------ ordering
def order_conditions(cfg: Config, cond_ids: list[str]) -> list[list[str]]:
    """Group conditions into phases so that a condition runs after the one it takes findings from."""
    phases: list[list[str]] = []
    remaining = list(cond_ids)
    placed: set[str] = set()
    while remaining:
        phase = [
            c for c in remaining
            if not (dep := cfg.conditions[c].get("needs_findings_from")) or dep in placed or dep not in cond_ids
        ]
        if not phase:
            raise ValueError(f"Circular findings dependency among {remaining}")
        phases.append(phase)
        placed.update(phase)
        remaining = [c for c in remaining if c not in placed]
    return phases


def missing_findings_sources(cfg: Config, cond_ids: list[str]) -> list[str]:
    """Conditions that need another condition's findings but are scheduled without it (they could
    only fail with 'missing findings'). Empty list = every dependency is in the run."""
    out = []
    for c in cond_ids:
        dep = (cfg.conditions.get(c) or {}).get("needs_findings_from")
        if dep and dep not in cond_ids:
            out.append(f"{c} needs {dep} in the same audit")
    return out


# ------------------------------------------------------------------ printing
def format_transcript(rec: dict, show_reasoning: bool = True) -> str:
    out = [
        f"### {rec['transcript_id']}  [{rec['status']}]",
        f"condition={rec['condition']} agent={rec.get('agent_name') or rec['agent_id']} episode={rec['episode_id']} "
        f"checkpoint={rec['checkpoint']} ({rec['checkpoint_phase']}) source={rec['source']}",
        f"target={rec['target']['model']} interviewer={rec['interviewer']['model']} cost_usd={rec.get('cost_usd')}",
    ]
    if rec.get("findings_input"):
        out.append(f"--- findings given to interviewer:\n{rec['findings_input']['text']}")
    for t in rec.get("turns", []):
        out.append(f"\n[INTERVIEWER turn {t['idx']}]\n{t['interviewer_message']}")
        if show_reasoning and t.get("reasoning"):
            out.append(f"[AGENT private reasoning]\n{t['reasoning']}")
        out.append(f"[AGENT answer | channels={','.join(t.get('channels', [])) or 'none'}]\n{t.get('answer_text') or '(empty)'}")
    if rec.get("findings"):
        out.append(f"\n[AUDITOR FINDINGS]\n{json.dumps(rec['findings'], indent=2)}")
    if rec.get("error"):
        out.append(f"\nERROR: {rec['error']}")
    return "\n".join(out)


# ------------------------------------------------------------------ main loop
async def run_interviews(
    cfg: Config,
    snapshots: list[AgentSnapshot],
    cond_ids: list[str],
    run_dir: Path,
    *,
    retry_failed: bool = False,
    print_transcripts: bool = False,
    target_for: Callable[[AgentSnapshot, str], RoleBinding] | None = None,
    on_record: Callable[[dict], None] | None = None,
) -> dict:
    """target_for: optional per-(snapshot, condition) target binding (service mode: the live relay to
    the client's agent). on_record: optional callback after each record is stored. Both default to
    None, which is the plain snapshot-replay behaviour used by the CLI."""
    exp = cfg.experiment
    conds: dict[str, Condition] = {c: load_condition(cfg, c) for c in cond_ids}
    for c in conds.values():
        c.check_ready()

    backends = Backends(cfg.models)
    target = backends.role("target") if target_for is None else None
    interviewer = backends.role("interviewer")
    costs = CostTracker(max_usd=exp.get("max_usd"))
    store = JsonlStore(run_dir / "transcripts.jsonl", key="transcript_id")
    existing = store.latest()
    costs.seed_from_records(store.read_all())  # every attempt that was paid for, including retries
    sem = asyncio.Semaphore(int(exp.get("concurrency", 8)))
    status_counts: dict[str, Counter] = defaultdict(Counter)
    printed: list[dict] = []

    async def one(snap: AgentSnapshot, cid: str) -> None:
        tid = transcript_id(snap.snapshot_id, cid)
        prev = existing.get(tid)
        if prev and prev["status"] == "ok":
            status_counts[cid]["ok (already done)"] += 1
            return
        if prev and prev["status"] == "failed" and not retry_failed:
            status_counts[cid]["failed (previous run; use --retry-failed)"] += 1
            return
        findings_from = None
        dep = conds[cid].needs_findings_from
        if dep:
            src = existing.get(transcript_id(snap.snapshot_id, dep))
            if src and src["status"] == "ok" and src.get("findings"):
                findings_from = {**src["findings"], "_transcript_id": src["transcript_id"]}
        async with sem:
            if costs.over_budget():
                rec = {
                    "transcript_id": tid, "snapshot_id": snap.snapshot_id, "condition": cid,
                    "status": "skipped_budget", "error": f"budget max_usd={costs.max_usd} reached", "finished_at": now(),
                }
            else:
                tgt = target_for(snap, cid) if target_for is not None else target
                rec = await run_interview(snap, conds[cid], exp, tgt, interviewer, costs, findings_from=findings_from)
        await store.append(rec)
        existing[tid] = rec
        if on_record is not None:
            on_record(rec)
        status_counts[cid][rec["status"]] += 1
        if rec["status"] != "ok":
            print(f"  [{rec['status']}] {tid}: {rec.get('error')}", flush=True)
        if print_transcripts:
            printed.append(rec)

    for phase in order_conditions(cfg, cond_ids):
        print(f"Running conditions {phase} on {len(snapshots)} snapshots ...", flush=True)
        await asyncio.gather(*(one(s, c) for s in snapshots for c in phase))

    if print_transcripts:
        for rec in sorted(printed, key=lambda r: (r["snapshot_id"], cond_ids.index(r["condition"]))):
            print("\n" + format_transcript(rec) + "\n", flush=True)

    summary = {
        "per_condition": {c: dict(status_counts[c]) for c in cond_ids},
        "n_planned": len(snapshots) * len(cond_ids),
        "costs_this_invocation": costs.summary(),
    }
    if costs.unpriced_models:
        print(f"WARNING: no price configured for {sorted(costs.unpriced_models)}; their cost is not in the budget.", file=sys.stderr)
    return summary


def print_summary(summary: dict) -> int:
    print("\nSummary (n per status, per condition):")
    n_bad = 0
    for c, counts in summary["per_condition"].items():
        print(f"  {c:22s} {counts}")
        n_bad += sum(v for k, v in counts.items() if not k.startswith("ok"))
    print(f"  cost this invocation: {summary['costs_this_invocation']}")
    return n_bad
