"""Linha do tempo: etapas de cada user story e cerimônias da sprint (estado: concluído / em andamento / pendente / falhou)."""
from .extensions import db
from .models import AgentConfig, AuditEvent, Deployment, ItemStage, Sprint, WorkItem, now

STAGES = [  # (chave, rótulo, quem costuma agir)
    ("refine", "Refinamento", "PO"),
    ("plan", "Planejamento", "Scrum Master"),
    ("implement", "Desenvolvimento", "Dev Team"),
    ("verify", "Verificação QA", "QA"),
    ("accept", "Aceite do PO", "PO"),
    ("done", "Concluído", ""),
]


def mark(item: WorkItem, stage: str, state: str, actor: str = "", detail: str = ""):
    """Registra uma transição de etapa. `refine` não pertence a nenhuma sprint; as demais pertencem à sprint atual do item."""
    db.session.add(ItemStage(item_id=item.id, sprint_id=None if stage == "refine" else item.sprint_id,
                             stage=stage, state=state, actor=actor, detail=detail[:500], created_at=now()))
    db.session.commit()


def _actors():
    return {a.slug: (a.name, a.emoji) for a in db.session.query(AgentConfig)}


def _legacy(item: WorkItem):
    """Itens sem histórico (criados antes desta versão): infere o estado pelo status."""
    order = {"backlog": 1, "sprint": 2, "progress": 2, "qa": 3, "done": 5, "rejected": 3}[item.status]
    st = {i: ("done" if i < order else "pending") for i in range(6)}
    if item.status == "progress": st[2] = "active"
    if item.status == "qa": st[3] = "active"
    if item.status == "rejected": st[3] = "failed"
    if item.status == "done": st[5] = "done"
    return [st[i] for i in range(6)]


def build(item: WorkItem, rows: list, actors: dict) -> dict:
    """Etapa vale pela linha mais recente SE for posterior à última linha de qualquer etapa anterior (retrabalho reabre as seguintes)."""
    rows = [r for r in rows if r.stage == "refine" or r.sprint_id == item.sprint_id]
    latest = {}
    barrier = 0
    steps = []
    for i, (key, label, who) in enumerate(STAGES):
        if key == "done":
            acc = latest.get("accept")
            st = "done" if item.status == WorkItem.DONE else "pending"
            steps.append({"key": key, "label": label, "state": st, "actor": "", "actor_name": "", "emoji": "", "at": acc["at"] if acc and st == "done" else None,
                          "detail": f"{item.points} pontos entregues" if st == "done" else ""})
            continue
        cand = [r for r in rows if r.stage == key and r.id > barrier]
        r = max(cand, key=lambda x: x.id) if cand else None
        if r:
            barrier = max(barrier, r.id)
            name, emoji = actors.get(r.actor, (r.actor or who, "👤" if r.actor in ("human", "") else "🤖"))
            latest[key] = {"at": r.created_at.isoformat()}
            steps.append({"key": key, "label": label, "state": r.state, "actor": r.actor, "actor_name": name, "emoji": emoji,
                          "at": r.created_at.isoformat(), "detail": r.detail})
        else:
            steps.append({"key": key, "label": label, "state": "pending", "actor": "", "actor_name": who, "emoji": "", "at": None, "detail": ""})
    if not rows and item.status != WorkItem.BACKLOG:  # legado
        for s, st in zip(steps, _legacy(item)):
            s["state"] = st
    if not rows and item.status == WorkItem.BACKLOG:
        steps[0]["state"] = "done"
    cur = next((s["key"] for s in steps if s["state"] in ("active", "failed")), next((s["key"] for s in steps if s["state"] == "pending"), "done"))
    return {"item": item.key, "title": item.title, "steps": steps, "current": cur, "rework": item.rework_count}


def timelines(items: list) -> dict:
    """{item_id: timeline} com 2 queries no total (não N+1)."""
    ids = [i.id for i in items]
    rows = db.session.query(ItemStage).filter(ItemStage.item_id.in_(ids)).all() if ids else []
    by = {}
    for r in rows:
        by.setdefault(r.item_id, []).append(r)
    actors = _actors()
    return {i.id: build(i, by.get(i.id, []), actors) for i in items}


# ---------------------------------------------------------------- sprint
def sprint_timeline(sprint: Sprint, running: bool = False, refining: bool = False) -> dict:
    ev = {}
    for e in db.session.query(AuditEvent).filter(AuditEvent.workflow_id == sprint.workflow_id, AuditEvent.event_type == "ceremony").order_by(AuditEvent.id):
        ev[e.target] = e.created_at.isoformat()  # último de cada tipo
    items = db.session.query(WorkItem).filter_by(sprint_id=sprint.id).all()
    total, done = len(items), sum(1 for i in items if i.status == "done")
    st = sprint.status
    backlog_n = db.session.query(WorkItem).filter_by(status="backlog").count()

    def state(cond_done, cond_active):
        return "done" if cond_done else "active" if cond_active else "pending"

    steps = [
        {"key": "refine", "label": "Refinamento", "actor_name": "Product Owner", "emoji": "🧭",
         "state": state(bool(ev.get("refine")) and not refining and (backlog_n or total), refining), "at": ev.get("refine"),
         "detail": f"{backlog_n + total} histórias no backlog"},
        {"key": "plan", "label": "Planning", "actor_name": "Scrum Master", "emoji": "🧙",
         "state": state("plan_done" in ev or st != "planning", "plan_start" in ev and "plan_done" not in ev), "at": ev.get("plan_done") or ev.get("plan_start"),
         "detail": f"{total} itens · capacidade {sprint.capacity} pts" if total else ""},
        {"key": "execute", "label": "Execução", "actor_name": "Dev Team + QA", "emoji": "👨‍💻",
         "state": state(st in ("review", "done"), st == "active"), "at": ev.get("plan_done"),
         "detail": f"{done}/{total} itens aceitos" if total else ""},
        {"key": "review", "label": "Sprint Review", "actor_name": "Product Owner", "emoji": "✅",
         "state": state(st == "done", st == "review"), "at": ev.get("sprint_closed"), "detail": f"velocidade {sprint.velocity} pts" if st == "done" else ""},
        {"key": "retro", "label": "Retrospectiva", "actor_name": "Scrum Master", "emoji": "🔁",
         "state": state(bool(sprint.retro), st == "review" and not sprint.retro), "at": ev.get("sprint_closed") if sprint.retro else None, "detail": (sprint.retro or "")[:140]},
    ]
    deps = db.session.query(Deployment).filter_by(sprint_id=sprint.id).order_by(Deployment.id.desc()).all()
    if st == "done" or deps:
        d = deps[0] if deps else None
        rel_state = {"running": "done", "stopped": "done", "skipped": "done", "ci_failed": "failed", "failed": "failed"}.get(d.status if d else "skipped", "active")
        steps.append({"key": "release", "label": "Release & Deploy", "actor_name": "DevOps", "emoji": "🚀", "state": rel_state if st == "done" else "pending",
                      "at": d.updated_at.isoformat() if d else None,
                      "detail": (f"{d.tag} · {d.detail}" if d else "sem release")[:140]})
    if "paused" in ev and st == "active" and not running:
        steps[2]["detail"] += " · pausada"
    cur = next((s["key"] for s in steps if s["state"] == "active"), next((s["key"] for s in steps if s["state"] == "pending"), steps[-1]["key"]))
    return {"sprint": sprint.name, "status": st, "steps": steps, "current": cur}
