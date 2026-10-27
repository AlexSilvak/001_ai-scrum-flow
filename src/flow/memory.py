"""Memória dos agentes (tabela agent_memory, PostgreSQL) com recuperação por relevância.

Relevância = sobreposição de termos + importância + recência. Sem dependências extras;
trocar por FTS5 / pgvector é uma mudança local em `search()`.
"""
import math
import re
import unicodedata

from sqlalchemy import or_, and_

from .extensions import db
from .models import AgentMemory, now


def _terms(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return set(re.findall(r"[a-z0-9_]{3,}", t))


def save(agent, content: str, *, kind="fact", scope="agent", tags=None, importance=3, sprint_id=None) -> AgentMemory:
    m = AgentMemory(agent_id=agent.id, scope=scope, kind=kind, content=content.strip(), tags=tags or [],
                    importance=max(1, min(5, int(importance))), sprint_id=sprint_id)
    db.session.add(m)
    db.session.commit()
    return m


def search(agent, query: str, k=5, include_shared=True) -> list[AgentMemory]:
    cond = and_(AgentMemory.agent_id == agent.id, AgentMemory.scope == "agent")
    if include_shared:
        cond = or_(cond, AgentMemory.scope == "shared")
    # kind "goal" fica de fora: a meta de uma sprint só vale para ela (a fonte da verdade é Sprint.goal) — o modelo copiava metas de sprints antigas
    cands = db.session.query(AgentMemory).filter(cond, AgentMemory.kind != "goal").order_by(AgentMemory.id.desc()).limit(500).all()
    qt, t_now, scored = _terms(query), now().replace(tzinfo=None), []
    for m in cands:
        mt = _terms(m.content) | _terms(" ".join(m.tags or []))
        overlap = len(qt & mt) / (len(qt) or 1)
        age_days = max((t_now - m.created_at.replace(tzinfo=None)).total_seconds() / 86400, 0)
        score = 2.0 * overlap + 0.15 * m.importance + 0.3 * math.exp(-age_days / 30)
        if qt and overlap == 0 and m.importance < 4:
            continue
        scored.append((score, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:k]]


def format_for_prompt(mems) -> str:
    if not mems:
        return ""
    return "Memórias relevantes:\n" + "\n".join(f"- [{m.kind}{'/shared' if m.scope == 'shared' else ''}] {m.content}" for m in mems)
