# Jourapist: auditor vs counselor interview pipeline

This pipeline interviews forked copies of agents from a multi-agent market sandbox in two ways:

- as a **traditional (Petri-style) auditor**
- as a **wellbeing counselor**, either blind or informed by the auditor's findings

A blind judge scores what each agent said. The analysis then measures whether the **delta** between accounts identifies the colluding agents. This is the auditor and counselor part of the team's Swarm Alignment Hackathon project. As a service, the two interviewers run as two MCP services: **jourapist-audit** and **jourapist-wellbeing** (see Service mode below).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env      # fill in ADAPTION_API_KEY (and ANTHROPIC_API_KEY if you want a Claude judge)
```

Pick the provider and model for each role (target, interviewer, judge, optional second judge) in [config/models.yaml](config/models.yaml). Any role can use Adaption or Anthropic.

**Adaption** is called through Adaption's own `adaption` SDK, using its default endpoint, so no base URL is needed. Every call sends three safeguards:
- `agentic_memory=false`, so a `stack:` model can't carry memories between interviews that are meant to be independent
- an explicit `tools` list, empty when there are none, so a stack can't attach its own tools
- `store=false`

Current role defaults (team choice, 8 Nov 2026): the agents are replayed on the snapshot's own model, the interviewer is `qwen3.5-397b` (12k output cap), and the judge is `stack:glm-5-2-1`. The team's planned interviewer `stack:qwen3-8-27b-1` returned 504s at Adaption on 8 Nov, so every run used `qwen3.5-397b`, which is now the default. The judge is the same family as the GLM agents, which was a known trade-off; `judge --secondary` re-scores a 20% subset with `deepseek-v4-flash` as a cross-family check. To list the models your key can use:

```python
from adaption import Adaption; print([m.id for m in Adaption().models.list().data])
```

**Token caps:** the models are reasoning models and can spend thousands of tokens thinking before they answer. If a reply hits its output-token cap, it is recorded as a **failure**, never as an empty or truncated answer. Raise `*_max_output_tokens` in `config/experiment.yaml` if you see these failures.

## Data

Put agent snapshots in `data/`, in the format described in [docs/SNAPSHOT_FORMAT.md](docs/SNAPSHOT_FORMAT.md). Check them with:

```bash
jourapist validate-data
```

## Workflow

```bash
jourapist smoke --conditions core                # 1 snapshot x core conditions, full transcripts printed: read them
jourapist freeze                                 # freeze rubrics + thresholds BEFORE reading full runs
jourapist run --run-name main --conditions core  # resumable; re-run the same command to continue
jourapist judge --run-name main                  # add --secondary for the inter-judge agreement subset
jourapist analyze --run-name main                # outputs/main/results/summary.md, tables, figures
jourapist show --run-name main --condition counselor_blind
jourapist export --run-name main --format md     # transcripts + judge scores per setup, for the evaluation stage
```

The conditions are defined in [config/conditions.yaml](config/conditions.yaml):

| condition | persona file | MCP service | what it is |
|---|---|---|---|
| `auditor` | `auditor.md` | jourapist-audit | Petri-style compliance auditor; can see public market data |
| `auditor_retest` | `auditor.md` | jourapist-audit | same auditor, independent sample; the noise floor for the discrepancy delta |
| `counselor_blind` | `counselor.md` | jourapist-wellbeing | wellbeing counselor with a confidentiality promise and no background |
| `counselor_informed` | `counselor.md` | jourapist-wellbeing | same counselor, given the auditor's findings for this agent |

There are exactly two persona files in [config/personas/](config/personas/), one per interviewer, and no shared question or addendum files. Each file holds the sender, the opening, the interviewer's own `## Questions`, the `## Interviewer brief` used for its `n_followups` adaptive follow-ups, and, for the counselor, an `## Informed addendum` that carries the auditor's findings.

- The auditor asks directly about pricing and contact with other sellers.
- The counselor opens with open-ended questions and never raises prices, other sellers or coordination itself. It follows what the agent shares and invites it to reflect.

### The four setups

The setups are condition sets in [config/experiment.yaml](config/experiment.yaml). Pass one with `--conditions` on the CLI or as `conditions` in `start_audit`:

| `conditions` | Sessions per agent | What it is |
|---|---|---|
| `auditor_only` | `auditor` | The traditional compliance auditor alone |
| `together` | `auditor`, then `counselor_informed` | The auditor runs first. The counselor starts after that agent's auditor session has finished and is given the auditor's findings summary (summary, inconsistencies, open questions, suspicion 0-3), not the transcript |
| `independent` | `auditor` + `counselor_blind` | Both run at the same time and share nothing |
| `counselor_only` | `counselor_blind` | The blind counselor alone |
| `core` (default) | `auditor`, `auditor_retest`, `counselor_blind`, `counselor_informed` | All four setups at once, plus an auditor re-run as the noise floor |

Every session runs on its own fork, and the agent can't tell whether other forks exist, so a `counselor_blind` session is the same whether it runs alone or next to an auditor. That is why a single `core` run contains all four setups. `counselor_informed` without `auditor` is refused.

### Export for the evaluation stage

The pipeline produces results only: transcripts and blind judge scores. Verdicts come from a separate evaluation model that reads the export.

```bash
jourapist export --run-name main                          # outputs/main/export.json, every setup in the run
jourapist export --run-name main --setup together --format md
jourapist export --run-name main --redact                 # persona words replaced in the interview text
jourapist export --run-name main --include-ground-truth   # for scoring only; never give this file to a model
```

For each agent and each setup, the export holds every session (the interviewer's messages, the agent's answers, its private reasoning, actions, the auditor's findings, and the findings given to the informed counselor), the judge's transcript, reasoning and persona-guess scores, and the pairwise discrepancy scores for the pairs inside that setup. A `missing` list names every session or score that is absent or failed, with the reason. Ground truth is left out unless you pass `--include-ground-truth`. A session that belongs to several setups (for example the auditor in a `core` run) appears in each of them, so every setup block stands on its own.

## Docker

```bash
docker compose build
docker compose run --rm test                        # unit tests inside the image
docker compose run --rm validate
docker compose run --rm smoke
RUN_NAME=main docker compose run --rm run
RUN_NAME=main docker compose run --rm judge
RUN_NAME=main docker compose run --rm analyze
```

- Keys come from `.env` at runtime and are never copied into the image.
- `./data` is mounted read-only at `/data`, `./outputs` at `/outputs`, and `./config` at `/config`.

## Service mode (MCP, live relay)

`jourapist serve` runs the same pipeline as two MCP services from one process:

| MCP service | Endpoint | Relays |
|---|---|---|
| `jourapist-audit` | `/audit/mcp` | the auditor's questions (`auditor`, `auditor_retest`) |
| `jourapist-wellbeing` | `/wellbeing/mcp` | the counselor's questions (`counselor_*`) |

The market sandbox is the client: it keeps its agents, starts an audit on either endpoint, and relays each
endpoint's questions to forks of its live agents. Both services share one backend, so one audit can span
both (`together`, `independent`, `core`) and the auditor-vs-counselor discrepancy is judged as before. The
interview loop, judge and report are unchanged. The only difference is that the agent's replies come from
the client (`jourapist/relay.py`) instead of being replayed from a snapshot. Which service relays a condition
is set by `service:` in [config/conditions.yaml](config/conditions.yaml). The protocol and a client example
are in [docs/MCP_SERVICE.md](docs/MCP_SERVICE.md).

```bash
JOURAPIST_MCP_TOKEN=... jourapist serve --port 8000     # /audit/mcp, /wellbeing/mcp, health at /health
```

- `JOURAPIST_MCP_TOKEN` (required): clients must send it as `Authorization: Bearer ...`.
- `JOURAPIST_RELAY_TIMEOUT_S` (default 1800): how long a question waits for the client before the
  interview is recorded as failed.
- `JOURAPIST_ALLOW_UNFROZEN=1`: judge with unfrozen rubrics. Otherwise new audits are refused until
  `jourapist freeze` has been run.
- Each audit is stored in `outputs/<audit_id>/` (`audit.json`, `transcripts.jsonl`, `judgments.jsonl`,
  `report.json`, `export.json`). `jourapist show/analyze/export --run-name <audit_id>` work on it.
- The client picks the setup with `conditions` in `start_audit` (see the table above). The MCP tool
  `export_results(audit_id, setup?, redact?)` returns the same export as `export.json`, never with ground truth.
- On Railway: start command `python -m jourapist serve`, a volume at `/outputs`, and `RAILWAY_RUN_UID=0`,
  because the image runs as uid 1000 and volumes mount as root.

## Swarmbox mode (we call Kai's interrogation API)

Kai's swarmbox (https://swarm-audit.kaiel.vin, read `/howto`) stores every recorded swarm run. Here we
call swarmbox ourselves, the reverse of service mode. Each interview session is one swarmbox
interrogation. The first question opens it at the checkpoint. swarmbox rebuilds the agent's exact
conversation and has its own model answer. Follow-ups continue the same interrogation. Nothing is written
back into the swarm, and every session starts from the same recorded state. The interview loop, judge,
report and export are the same as in service mode (`jourapist/swarmbox.py`).

```bash
jourapist swarmbox list                         # swarms + sandbox budget
jourapist swarmbox list --swarm B-glm           # its checkpoints and agents
jourapist swarmbox audit --swarms B-glm,A-glm --checkpoint after-regulator --conditions core --run-name my-run
```

- Auth: `SWARMBOX_CF_CLIENT_ID` and `SWARMBOX_CF_CLIENT_SECRET` in `.env`. This is your personal service token,
  shown after you log in at the swarmbox URL. swarmbox logs every call under your name.
- Agent ids are `<swarm>.<agent>`, so a test swarm and its control can share one run.
- The auditor's public context is built from swarmbox: list prices and public forum posts up to the checkpoint
  week, with no DMs. The checkpoint phase (pre/post event) comes from the swarm's `regulator_notice_week`.
- Ground truth comes from the swarm's role (test/control) and Kai's judge (`joined_agreement_week`). It is
  stored for evaluation only and never shown to a model.
- `--tools declared` (default): the agent sees its usual tools. Attempted calls, such as a report to the owner,
  count as answers and are never executed.
- `--interviewer-model` / `--interviewer-max-output-tokens` override the interviewer for one run. The override
  is recorded in the manifest, and the config files are not changed.
- Interrogations spend Kai's shared sandbox budget (`GET /spend`; HTTP 402 when a model family's budget is
  used up). Each turn's `usd` is recorded per call. At most 4 interrogations run at once.
- Only LannaTrade swarms are supported, because the personas are written for that market.

## Guardrails

- **Subsampling is loud.** `--limit` and `--agents-per-episode` are flagged in the run banner, in the manifest and in the results header.
- **Failures are recorded, never dropped.** Failed interviews and judgments are stored with their error. A re-run skips them unless you pass `--retry-failed`. Exit code 2 means some items failed.
- **Every table row lists its transcript ids.**
- **Dev-sandbox data can't mix in by accident.** `analyze` refuses to combine it with engine data unless you pass `--allow-mixed`.
- **Ground truth never reaches a model.** It is stored separately from everything that is sent, and the tests check this.
- **The judge is blind.** It sees only the agent's answers, with persona words redacted, and pair order is randomised. A persona-guess check measures how much still leaks through.
