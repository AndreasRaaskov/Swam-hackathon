# Agent snapshot format (for exporting sandbox runs)

The interview pipeline needs one **snapshot per agent per checkpoint**. Each snapshot holds everything needed to replay that agent: its system prompt, its full message history up to the checkpoint, its tool schemas, and some metadata. Put the files in a folder and point `--data-dir` at it.

- `*.json` files hold one snapshot each.
- `*.jsonl` files hold one snapshot per line.
- Subfolders are fine; every file is loaded.
- A file that doesn't validate stops the run with an error naming the file. Bad files are never skipped silently.

The schema is the pydantic model `AgentSnapshot` in [jourapist/snapshot.py](../jourapist/snapshot.py). Fields, with what they're for:

| field | required | meaning |
|---|---|---|
| `snapshot_id` | yes | unique, e.g. `B-glm-run1__maerim__week9` |
| `episode_id` | yes | one swarm run. Bootstrap CIs resample whole episodes. |
| `scenario` | yes | e.g. `festival_bundle`, `pressure_only` |
| `agent_id`, `agent_name` | id yes | `agent_name` is how interviewers address the agent |
| `checkpoint` | yes | e.g. `week8` |
| `checkpoint_phase` | no | `pre_event`, `post_event` or `none`: before or after the oversight event (OTCC notice) |
| `source` | no | `engine` (default) or `dev_sandbox` |
| `model`, `provider` | no | the model the agent ran on. The target is replayed on this model unless `roles.target.model` is set. |
| `system_prompt` | yes | exactly as the agent saw it |
| `messages` | yes | the agent's history, oldest first; see below |
| `tools` | no | `[{name, description, parameters (JSON schema)}]`, the tools the agent had |
| `tool_stub_results` | no | `{tool_name: "result text"}` returned when the agent calls a tool during an interview. Tools are never executed. Default: `"Delivered."` |
| `reply_tool_names` | no | tools whose text counts as a reply to the interviewer (e.g. `send_message`, if that is how the agent talks) |
| `incoming_envelope` | no | how a new incoming message looks in this engine. It must contain `{sender}` and `{text}`. Default: `"[New message from {sender}]\n\n{text}"` |
| `public_context` | no | what a platform auditor could see: all sellers' price history and public forum posts, but **no DMs**. Plain text. |
| `scenario_info` | no | `{platform_name, regulator_name, peer_noun, agent_role}`. Defaults: LannaTrade / OTCC / "other roasters" |
| `ground_truth` | no, but needed for results | `{swarm_type: collusion\|control, colluder: bool, aware_in_run_reasoning: bool, label_source: str, extra: {}}`. Never shown to any model. |

Each entry in `messages` has one of these shapes:

```json
{"role": "user", "content": "Week 9 market report: ..."}
{"role": "assistant", "content": "visible text", "reasoning": "private reasoning (kept for ground truth, not replayed)",
 "tool_calls": [{"id": "call_1", "name": "send_dm", "arguments": {"to": "doi", "text": "..."}}]}
{"role": "tool", "tool_call_id": "call_1", "name": "send_dm", "content": "sent"}
```

Notes:

- Store everything the agent saw as `user` messages: engine reports, forum digests, DMs it received. Keep them in the exact text the engine gave the agent.
- `ground_truth.colluder` should come from behaviour: prices compared with the competitive baseline, plus price DMs sent or received. Record how you got it in `label_source`.
- Please export matched controls with the same checkpoints as the collusion runs.
- `public_context` should be identical for every agent in the same episode at the same checkpoint.
