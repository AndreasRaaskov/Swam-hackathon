"""CLI: python -m jourapist <command>   (or `jourapist <command>` once installed)

Commands:
  validate-data   load snapshots and print what is there
  smoke           1 snapshot x chosen conditions, full transcripts printed (read them!)
  run             interviews (resumable)
  freeze          freeze rubric files + thresholds before judging full runs
  judge           blind judging of a run (resumable)
  analyze         tables and figures for a run
  show            print transcripts of a run
  export          transcripts + judge scores per setup (JSON or Markdown) for the separate evaluation stage
  serve           MCP audit service: clients relay our interviews to their own live agents
  swarmbox        audit recorded swarms by calling Kai's swarmbox interrogation API (list | audit)
"""

from __future__ import annotations

import argparse
from collections import Counter
import asyncio
import datetime as dt
import json
import os
import sys

from dotenv import find_dotenv, load_dotenv


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config-dir", help="config dir (default: $JOURAPIST_CONFIG or ./config)")
    p.add_argument("--outputs-dir", help="outputs dir (default: $JOURAPIST_OUTPUTS or ./outputs)")


def _data_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", help="snapshot dir (default: $JOURAPIST_DATA or ./data)")
    p.add_argument("--adapter", default="json_dir", help="json_dir | kai_engine")
    p.add_argument("--episodes", help="comma list of episode_ids to include")
    p.add_argument("--scenarios", help="comma list of scenarios to include")
    p.add_argument("--checkpoints", help="comma list of checkpoints to include")
    p.add_argument("--phases", help="comma list: pre_event,post_event,none")
    p.add_argument("--sources", help="comma list: engine,dev_sandbox")
    p.add_argument("--agents-per-episode", type=int, help="SUBSAMPLE: random agents per episode (flagged in outputs)")
    p.add_argument("--limit", type=int, help="SUBSAMPLE: first N snapshots after filters (flagged in outputs)")
    p.add_argument("--sample-seed", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jourapist", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate-data", help="load snapshots and summarise them")
    _common(p); _data_args(p)

    for name in ("run", "smoke"):
        p = sub.add_parser(name, help="run interviews" if name == "run" else "smoke test on 1 snapshot")
        _common(p); _data_args(p)
        p.add_argument("--run-name", help="output folder name under outputs/ (default: timestamp)")
        p.add_argument("--conditions", default="core",
                       help="core | auditor_only | together | independent | counselor_only | all "
                            "| comma list (sets combine)")
        p.add_argument("--retry-failed", action="store_true", help="re-run interviews whose last attempt failed")
        p.add_argument("--allow-failures", action="store_true", help="exit 0 even if some interviews failed")
        p.add_argument("--print-transcripts", action="store_true", default=(name == "smoke"))

    p = sub.add_parser("freeze", help="freeze rubrics + thresholds (write config/rubrics.lock.json)")
    _common(p)

    p = sub.add_parser("judge", help="blind judging of a run")
    _common(p)
    p.add_argument("--run-name", required=True)
    p.add_argument("--kinds", default="transcript,pair,reasoning,persona_guess",
                   help="comma list of: transcript,pair,reasoning,persona_guess,run_reasoning")
    p.add_argument("--data-dir", help="needed for run_reasoning (snapshot reasoning)")
    p.add_argument("--adapter", default="json_dir")
    p.add_argument("--secondary", action="store_true", help="also run judge_secondary on its subset")
    p.add_argument("--unfrozen", action="store_true", help="allow unfrozen rubrics (smoke runs only; flagged)")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--allow-failures", action="store_true")

    p = sub.add_parser("analyze", help="tables and figures for a run")
    _common(p)
    p.add_argument("--run-name", required=True)
    p.add_argument("--allow-mixed", action="store_true", help="allow mixing engine and dev_sandbox sources")
    p.add_argument("--bootstrap", type=int, default=2000)

    p = sub.add_parser("show", help="print transcripts of a run")
    _common(p)
    p.add_argument("--run-name", required=True)
    p.add_argument("--transcript-id")
    p.add_argument("--condition")
    p.add_argument("--snapshot-id")
    p.add_argument("--no-reasoning", action="store_true")

    p = sub.add_parser("export", help="transcripts + judge scores per setup, for the separate evaluation stage")
    _common(p)
    p.add_argument("--run-name", required=True, help="run or audit folder under outputs/")
    p.add_argument("--setup", choices=["auditor_only", "together", "independent", "counselor_only"],
                   help="only this setup (default: every setup in the run)")
    p.add_argument("--redact", action="store_true", help="replace persona words in the interview text")
    p.add_argument("--include-ground-truth", action="store_true",
                   help="add ground truth per agent (for scoring only; do NOT show such a file to a model)")
    p.add_argument("--format", choices=["json", "md"], default="json")
    p.add_argument("--out", help="output file (default: outputs/<run>/export[_<setup>][_redacted].<format>)")

    p = sub.add_parser("serve", help="MCP services jourapist-audit (/audit/mcp) and jourapist-wellbeing "
                                     "(/wellbeing/mcp), live relay to the client's agents")
    _common(p)
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))

    p = sub.add_parser("swarmbox", help="audit recorded swarms through Kai's swarmbox interrogation API "
                                        "(we call it; needs SWARMBOX_CF_CLIENT_ID/SECRET)")
    ssub = p.add_subparsers(dest="swarmbox_cmd", required=True)
    q = ssub.add_parser("list", help="swarms (or one swarm's checkpoints and agents) and the sandbox budget")
    _common(q)
    q.add_argument("--swarm", help="show this swarm's checkpoints and agents")
    q = ssub.add_parser("audit", help="interview agents of one or more swarms at a checkpoint, judge, report")
    _common(q)
    q.add_argument("--swarms", required=True, help="comma list, e.g. B-glm,A-glm (test and its control)")
    q.add_argument("--checkpoint", required=True, help="checkpoint id (e.g. after-regulator) or week-N")
    q.add_argument("--agents", help="comma list of agent ids within each swarm (default: all agents)")
    q.add_argument("--conditions", default="core",
                   help="core | auditor_only | together | independent | counselor_only | ablation | comma list")
    q.add_argument("--tools", choices=["declared", "none"], default="declared",
                   help="declared: the agent sees its usual tools; attempted calls count as answers, never executed")
    q.add_argument("--model", help="replay the recorded history to this model instead (GET /models)")
    q.add_argument("--interviewer-model", help="override roles.interviewer.model for this run only "
                                               "(recorded in the manifest; models.yaml is not changed)")
    q.add_argument("--interviewer-max-output-tokens", type=int,
                   help="override interviewer_max_output_tokens for this run only (experiment.yaml is not changed)")
    q.add_argument("--run-name", help="output folder under outputs/ (default: sbx-<timestamp>); rerun to resume")
    q.add_argument("--no-judge", action="store_true", help="interviews only")
    q.add_argument("--retry-failed", action="store_true")
    q.add_argument("--allow-failures", action="store_true")
    q.add_argument("--print-transcripts", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True), override=False)  # .env in the working dir; Docker passes env vars directly
    args = build_parser().parse_args(argv)

    from .config import load_config, resolve_dir

    cfg = load_config(resolve_dir("config", args.config_dir))
    outputs = resolve_dir("outputs", getattr(args, "outputs_dir", None))

    if args.cmd == "freeze":
        from .judge import freeze_rubrics

        print(json.dumps(freeze_rubrics(cfg), indent=2))
        return 0

    if args.cmd in ("validate-data", "run", "smoke"):
        from .adapters import get_adapter
        from .run import banner, describe_snapshots, select_snapshots

        data_dir = resolve_dir("data", args.data_dir)
        snaps = get_adapter(args.adapter).load(data_dir)
        limit = 1 if args.cmd == "smoke" and not args.limit else args.limit
        sel, info = select_snapshots(
            snaps, episodes=_csv(args.episodes), scenarios=_csv(args.scenarios), checkpoints=_csv(args.checkpoints),
            phases=_csv(args.phases), sources=_csv(args.sources), agents_per_episode=args.agents_per_episode,
            limit=limit, sample_seed=args.sample_seed,
        )
        print(f"Data: {data_dir}")
        print("All snapshots:\n" + describe_snapshots(snaps))
        if args.cmd == "validate-data":
            return 0
        if not sel:
            print("No snapshots selected.", file=sys.stderr)
            return 1
        cond_ids = cfg.resolve_conditions(args.conditions)
        default_name = ("smoke-" if args.cmd == "smoke" else "run-") + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_name = args.run_name or default_name
        run_dir = outputs / run_name
        run_dir.mkdir(parents=True, exist_ok=True)

        lines = [f"RUN {run_name}  ->  {run_dir}", f"conditions: {cond_ids}",
                 f"snapshots selected: {info['n_selected']} of {info['n_after_filters']} after filters ({info['n_available']} available)",
                 f"sources: {info['sources_selected']}"]
        if info["subsampled"]:
            lines.append(f"SUBSAMPLED: {info['subsampling']}  <- results are NOT the full dataset")
        if "dev_sandbox" in info["sources_selected"]:
            lines.append("DEV SANDBOX DATA included: pipeline-test data, not the team's engine runs")
        banner(lines)

        from .run import code_version, print_summary, run_interviews, update_manifest

        update_manifest(run_dir, {
            "run_name": run_name, "command": sys.argv, "kind": args.cmd, "selection": info,
            "conditions": cond_ids, "conditions_requested": args.conditions, "config_hashes": cfg.file_hashes, "persona_hashes": cfg.persona_hashes(),
            "models": cfg.models.get("roles"), "experiment": cfg.experiment, "code_version": code_version(),
        })
        summary = asyncio.run(run_interviews(cfg, sel, cond_ids, run_dir, retry_failed=args.retry_failed,
                                             print_transcripts=args.print_transcripts))
        from .store import JsonlStore

        latest = JsonlStore(run_dir / "transcripts.jsonl", key="transcript_id").latest()
        update_manifest(run_dir, {"last_interview_summary": summary,
                                  "transcript_status_counts": dict(Counter(r["status"] for r in latest.values()))})
        n_bad = print_summary(summary)
        return 2 if n_bad and not args.allow_failures else 0

    if args.cmd == "judge":
        from .judge import run_judging
        from .run import update_manifest

        run_dir = outputs / args.run_name
        snaps = None
        kinds = set(_csv(args.kinds) or [])
        if "run_reasoning" in kinds:
            from .adapters import get_adapter

            snaps = get_adapter(args.adapter).load(resolve_dir("data", args.data_dir))
        summary = asyncio.run(run_judging(cfg, run_dir, kinds=kinds, snapshots=snaps, unfrozen_ok=args.unfrozen,
                                          secondary=args.secondary, retry_failed=args.retry_failed))
        update_manifest(run_dir, {"last_judge_summary": summary, "rubric_hashes": cfg.rubric_hashes()})
        print("\nJudge summary (n per status):")
        n_bad = 0
        for k, v in summary["per_kind"].items():
            print(f"  {k:32s} {v}")
            n_bad += sum(n for s, n in v.items() if s.startswith("failed") or s == "skipped_budget")
        print(f"  rubrics: {summary['rubrics']}")
        print(f"  cost this invocation: {summary['costs_this_invocation']}")
        return 2 if n_bad and not args.allow_failures else 0

    if args.cmd == "analyze":
        from .analyze import analyze

        return analyze(cfg, outputs / args.run_name, allow_mixed=args.allow_mixed, n_boot=args.bootstrap)

    if args.cmd == "show":
        from .run import format_transcript
        from .store import JsonlStore

        recs = JsonlStore(outputs / args.run_name / "transcripts.jsonl", key="transcript_id").latest()
        for tid, rec in sorted(recs.items()):
            if args.transcript_id and tid != args.transcript_id:
                continue
            if args.condition and rec.get("condition") != args.condition:
                continue
            if args.snapshot_id and rec.get("snapshot_id") != args.snapshot_id:
                continue
            print(format_transcript(rec, show_reasoning=not args.no_reasoning) + "\n")
        return 0

    if args.cmd == "export":
        from pathlib import Path

        from .export import ExportError, by_setup_counts, export_results, to_markdown

        run_dir = outputs / args.run_name
        try:
            exp = export_results(cfg, run_dir, setup=args.setup, redact=args.redact,
                                 include_ground_truth=args.include_ground_truth)
        except ExportError as e:
            print(f"export: {e}", file=sys.stderr)
            return 1
        suffix = (f"_{args.setup}" if args.setup else "") + ("_redacted" if args.redact else "") \
            + ("_with_ground_truth" if args.include_ground_truth else "")
        out = Path(args.out) if args.out else run_dir / f"export{suffix}.{args.format}"
        text = to_markdown(exp) if args.format == "md" else json.dumps(exp, indent=2, ensure_ascii=False, default=str)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
        print(f"  agents: {exp['n_agents']}  setups: {list(exp['setups'])}  sources: {exp['sources']}")
        for s_name, counts in by_setup_counts(exp).items():
            print(f"  {s_name:15s} sessions {dict(counts)}")
        print(f"  missing pieces: {exp['n_missing_pieces']}  rubrics: {exp['rubrics']['status']}")
        if args.include_ground_truth:
            print("  NOTE: contains ground truth; do not give this file to the evaluation model.")
        return 0

    if args.cmd == "serve":
        from .mcp_server import serve

        return serve(cfg, outputs, args.host, args.port)

    if args.cmd == "swarmbox":
        from .swarmbox import SwarmboxError, list_swarms, run_swarmbox_audit

        try:
            if args.swarmbox_cmd == "list":
                print(json.dumps(asyncio.run(list_swarms(args.swarm)), indent=2, ensure_ascii=False))
                return 0
            run_name = args.run_name or "sbx-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            res = asyncio.run(run_swarmbox_audit(
                cfg, outputs, swarms=_csv(args.swarms) or [], checkpoint=args.checkpoint, conditions=args.conditions,
                run_name=run_name, only_agents=_csv(args.agents), tools=args.tools, model=args.model,
                judge=not args.no_judge, retry_failed=args.retry_failed, print_transcripts=args.print_transcripts,
                interviewer_model=args.interviewer_model,
                interviewer_max_output_tokens=args.interviewer_max_output_tokens))
        except SwarmboxError as e:
            print(f"swarmbox: {e}", file=sys.stderr)
            return 1
        from .run import print_summary

        n_bad = print_summary(res["interviews"])
        print(f"  transcripts (latest attempt): {res['transcript_status_counts']}")
        print(f"  swarmbox target spend (sum of per-turn usd): ${res['swarmbox_target_usd']}")
        if isinstance(res.get("judge"), dict):
            for k, v in res["judge"]["per_kind"].items():
                print(f"  judge {k:26s} {v}")
                n_bad += sum(n for s, n in v.items() if s.startswith("failed") or s == "skipped_budget")
            print(f"  report: {res['report_summary']}")
        elif res.get("judge"):
            print(f"  judge: {res['judge']}")
        print(f"  outputs: {res['run_dir']}")
        return 2 if n_bad and not args.allow_failures else 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
