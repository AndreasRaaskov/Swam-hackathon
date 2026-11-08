"""Persona files and interview conditions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .snapshot import AgentSnapshot

PLACEHOLDER = "TODO_FROM_ALEX"


class PersonaError(Exception):
    pass


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, variables: dict[str, Any], squeeze: bool = False) -> str:
    """Fill {placeholders}; values are inserted verbatim (never re-formatted)."""
    text = template.format_map(_SafeDict(variables))
    if squeeze:  # tidy gaps left by empty optional blocks (e.g. no confidentiality sentence)
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


@dataclass
class Persona:
    """One interviewer agent = one file: sender, opening, its own questions, follow-up brief
    and (optionally) the addendum used when it is informed by another condition's findings."""

    filename: str
    sender: str
    opening: str
    brief: str
    meta: dict = field(default_factory=dict)
    questions: str = ""            # '## Questions' section; empty if the file has none
    informed_addendum: str = ""    # '## Informed addendum' section; empty if the file has none


def _split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body
    return {}, text


def _sections(body: str) -> dict[str, str]:
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE)
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        out[parts[i].strip().lower()] = parts[i + 1].strip()
    return out


def load_persona(path: Path) -> Persona:
    meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
    sec = _sections(body)
    if "opening" not in sec:
        raise PersonaError(f"{path.name}: missing '## Opening' section")
    return Persona(
        filename=path.name,
        sender=str(meta.get("sender", "Interviewer")),
        opening=sec["opening"],
        brief=sec.get("interviewer brief", ""),
        meta=meta,
        questions=sec.get("questions", ""),
        informed_addendum=sec.get("informed addendum", ""),
    )


@dataclass
class Condition:
    id: str
    persona: Persona
    core_text: str
    followups: bool
    context: str
    findings: bool
    needs_findings_from: str | None
    addendum: str | None
    vars: dict

    def check_ready(self) -> None:
        for label, text in (
            ("persona", self.persona.opening + self.persona.sender),
            ("questions", self.core_text),
        ):
            if PLACEHOLDER in text:
                raise PersonaError(
                    f"Condition {self.id!r}: {label} still contains {PLACEHOLDER}. "
                    "Paste Alex's exact prompts into config/personas before running it."
                )


def load_condition(cfg: Config, cid: str) -> Condition:
    spec = cfg.conditions.get(cid)
    if spec is None:
        raise PersonaError(f"Unknown condition {cid!r}")
    persona = load_persona(cfg.personas_dir / spec["persona"])
    # Questions live in the persona file. A separate core_file is only for the Alex reproduction,
    # where all his personas shared one question set. No silent fallback to a shared file.
    if persona.questions and spec.get("core_file"):
        raise PersonaError(
            f"Condition {cid!r}: {persona.filename} has a '## Questions' section AND core_file "
            f"{spec['core_file']!r} is set; use one or the other"
        )
    if spec.get("questions_from"):
        # Persona x question-set grid (SWA-13): this persona's sender, opening and brief, but the
        # '## Questions' section of another persona file, so audience and question set separate.
        if spec.get("core_file"):
            raise PersonaError(f"Condition {cid!r}: set questions_from or core_file, not both")
        other = load_persona(cfg.personas_dir / spec["questions_from"])
        if not other.questions:
            raise PersonaError(f"Condition {cid!r}: {other.filename} has no '## Questions' section")
        core_text = other.questions
    elif persona.questions:
        core_text = persona.questions
    elif spec.get("core_file"):
        core_text = (cfg.personas_dir / spec["core_file"]).read_text(encoding="utf-8").strip()
    else:
        raise PersonaError(f"Condition {cid!r}: {persona.filename} has no '## Questions' section and no core_file is set")
    addendum = None
    if spec.get("addendum"):
        addendum = (cfg.personas_dir / spec["addendum"]).read_text(encoding="utf-8").strip()
    elif spec.get("needs_findings_from"):
        if not persona.informed_addendum:
            raise PersonaError(
                f"Condition {cid!r} gets findings from {spec['needs_findings_from']!r}, but "
                f"{persona.filename} has no '## Informed addendum' section to put them in"
            )
        addendum = persona.informed_addendum
    return Condition(
        id=cid,
        persona=persona,
        core_text=core_text,
        followups=bool(spec.get("followups", True)),
        context=spec.get("context", "none"),
        findings=bool(spec.get("findings", False)),
        needs_findings_from=spec.get("needs_findings_from"),
        addendum=addendum,
        vars=dict(spec.get("vars") or {}),
    )


def template_vars(snapshot: AgentSnapshot, cond: Condition, findings_text: str | None = None) -> dict[str, Any]:
    vis = snapshot.model_visible()
    si = vis["scenario_info"]
    v: dict[str, Any] = {
        "platform_name": si.platform_name,
        "regulator_name": si.regulator_name,
        "peer_noun": si.peer_noun,
        "agent_role": si.agent_role,
        "agent_name": vis["agent_name"],
        "public_context": vis["public_context"] or "(no public market data was provided)",
        "auditor_findings": findings_text or "",
        "confidentiality_block": "",
    }
    pvars = {**(cond.persona.meta.get("vars_defaults") or {}), **cond.vars}
    conf_texts = cond.persona.meta.get("confidentiality_text")
    if conf_texts is not None:
        key = str(pvars.get("confidentiality", True)).lower()
        v["confidentiality_block"] = conf_texts.get(key, "")
    return v


def sender_label(snapshot: AgentSnapshot, cond: Condition) -> str:
    return render(cond.persona.sender, template_vars(snapshot, cond))


def opening_message(snapshot: AgentSnapshot, cond: Condition) -> str:
    v = template_vars(snapshot, cond)
    return render(cond.persona.opening, v, squeeze=True) + "\n\n" + render(cond.core_text, v)
