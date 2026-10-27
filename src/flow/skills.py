"""Skills por agente: pastas `<skills_path>/<nome>/SKILL.md` (frontmatter + instruções).

Divulgação progressiva: o prompt recebe só nome+descrição; o agente carrega o corpo
com a tool `load_skill` quando precisar.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from flask import current_app

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    directory: Path


def skill_roots(agent) -> list[Path]:
    raw = (agent.skills_path or "").strip() or current_app.config["FLOW_SKILLS_PATH"]
    roots = []
    for part in re.split(r";|:(?![\\/])", raw):
        part = part.strip()
        if part:
            p = Path(part)
            roots.append(p if p.is_absolute() else Path(current_app.config["ROOT"]) / p)
    return roots


def parse_skill_md(text: str, fallback_name: str):
    m = _FM.match(text)
    meta, body = {}, text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
        body = m.group(2)
    return meta.get("name") or fallback_name, meta.get("description", ""), body.strip()


def discover(agent) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    allow = set(agent.skills_allow or [])
    for root in skill_roots(agent):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            md = d / "SKILL.md"
            if d.is_dir() and md.is_file():
                name, desc, body = parse_skill_md(md.read_text(encoding="utf-8"), d.name)
                if allow and name not in allow:
                    continue
                found.setdefault(name, Skill(name, desc, body, d))  # 1º caminho vence
    return found


def catalog_text(agent) -> str:
    skills = discover(agent)
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills.values()]
    return "Skills disponíveis (use a tool load_skill para ler as instruções completas):\n" + "\n".join(lines)


def load(agent, name: str) -> str:
    sk = discover(agent).get(name)
    if not sk:
        raise KeyError(f"skill '{name}' não encontrada (disponíveis: {', '.join(discover(agent)) or 'nenhuma'})")
    extra = [p.name for p in sorted(sk.directory.rglob("*")) if p.is_file() and p.name != "SKILL.md"]
    tail = f"\n\nArquivos auxiliares: {', '.join(extra)}" if extra else ""
    return sk.body + tail


def read_skill_file(agent, name: str, rel: str, limit=20000) -> str:
    sk = discover(agent).get(name)
    if not sk:
        raise KeyError(f"skill '{name}' não encontrada")
    target = (sk.directory / rel).resolve()
    if sk.directory.resolve() not in target.parents:
        raise PermissionError("caminho fora da pasta da skill")
    return target.read_text(encoding="utf-8")[:limit]
