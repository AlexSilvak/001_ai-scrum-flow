"""Monta o contexto do agente respeitando o orçamento de tokens configurado."""
from pathlib import Path

from flask import current_app

from . import memory as mem
from . import skills as skl
from . import tools as toolreg
from .llm import estimate_tokens
from .extensions import db
from .models import A2ATask, ROLE_LABEL

SCRUM_RULES = """Regras Scrum deste time:
- O Product Owner (PO) é dono do Product Backlog e decide o que é aceito.
- O Scrum Master (SM) facilita a sprint e remove impedimentos; não decide o escopo.
- Developers (incl. arquiteto, designer e QA) entregam e verificam; quem desenvolve não aprova o próprio item.
- Só delegue a quem a sua configuração permite. Toda ação é auditada.
- NUNCA invente andamento, resultados ou prontidão: para responder "em que etapa está", "já terminou?", "onde vejo o resultado?" ou
  qualquer pergunta de status, chame PRIMEIRO a ferramenta sprint_status e responda somente com o que ela devolver."""

PROTOCOL = """Protocolo de resposta (SOMENTE UM objeto JSON por turno, sem texto fora dele):
- Para usar uma ferramenta: {"tool": "<nome>", "args": {...}}
- Quando terminar: {"final": "<resposta curta para quem pediu>"}
Você recebe o resultado como "TOOL_RESULT <nome>: ...". Se uma tool falhar, corrija os argumentos ou explique no final."""

TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".csv", ".html"}


def _context_files(paths: list[str], budget_chars: int) -> str:
    chunks, used = [], 0
    for raw in paths:
        p = Path(raw)
        p = p if p.is_absolute() else Path(current_app.config["ROOT"]) / p
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else []
        for f in files:
            if f.suffix.lower() not in TEXT_EXT or used >= budget_chars:
                continue
            txt = f.read_text(encoding="utf-8", errors="ignore")[: budget_chars - used]
            used += len(txt)
            chunks.append(f"### {f.name}\n{txt}")
    return "Documentos de contexto:\n" + "\n\n".join(chunks) if chunks else ""


def _history(agent, context_id: str, n: int, exclude_task: str | None):
    msgs = []
    if n <= 0:
        return msgs
    q = db.session.query(A2ATask).filter(A2ATask.agent_id == agent.id, A2ATask.context_id == context_id)
    if exclude_task:
        q = q.filter(A2ATask.id != exclude_task)
    tasks = q.order_by(A2ATask.created_at.desc()).limit(n).all()
    for t in reversed(tasks):
        for m in t.history:
            text = " ".join(p.get("text", "") for p in m.get("parts", []) if "text" in p)
            if text:
                msgs.append({"role": "user" if m["role"] == "ROLE_USER" else "assistant", "content": text})
    return msgs[-n * 2:]


def build(agent, *, query: str, external_tools: dict, context_id: str, task_id: str | None):
    cfg = agent.ctx()
    budget = int(cfg["max_tokens"])
    internal = [t for t in [*(agent.tools or []), *toolreg.BASELINE] if t in toolreg.REGISTRY]
    internal = list(dict.fromkeys(internal))
    tools_txt = toolreg.describe(internal)
    for qn, info in external_tools.items():
        if info["tool"] != "__error__":
            tools_txt += f"\n- {qn}({', '.join((info['schema'].get('properties') or {}).keys())}) — {info['description']} [MCP]"

    core = (
        f"[[role:{agent.scrum_role}|agent:{agent.slug}]]\n"
        f"Você é {agent.name} — {ROLE_LABEL.get(agent.scrum_role, agent.scrum_role)}{' / ' + agent.specialty if agent.specialty else ''}.\n"
        f"{agent.system_prompt}\n\n{SCRUM_RULES}\n\n{PROTOCOL}\n\nFerramentas disponíveis:\n{tools_txt or '(nenhuma)'}"
    )
    sections = {
        "skills": skl.catalog_text(agent),
        "memory": mem.format_for_prompt(mem.search(agent, query, k=int(cfg["memory_k"]), include_shared=cfg["include_shared_memory"])),
        "files": "",
    }
    used = estimate_tokens(core)
    for key in ("skills", "memory"):
        cost = estimate_tokens(sections[key])
        if used + cost > budget:
            sections[key] = sections[key][: max(0, (budget - used) * 4)]  # trunca o que não couber
        used += estimate_tokens(sections[key])
    if cfg["context_paths"]:
        sections["files"] = _context_files(cfg["context_paths"], max(0, (budget - used) * 4))
        used += estimate_tokens(sections["files"])

    system = "\n\n".join(s for s in (core, sections["skills"], sections["memory"], sections["files"]) if s)
    hist = _history(agent, context_id, int(cfg["history_messages"]), task_id)
    while hist and used + sum(estimate_tokens(h["content"]) for h in hist) > budget:
        hist.pop(0)  # descarta o mais antigo primeiro
    return system, hist, {"budget": budget, "system_tokens": used, "history_msgs": len(hist)}
