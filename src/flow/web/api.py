"""API JSON consumida pelo dashboard (e utilizável por scripts)."""
import hmac
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import a2a, activity, audit, cd, gitflow, llm, queue, scrum, stages
from .. import memory as mem
from .. import skills as skl
from .. import tools as toolreg
from ..extensions import db
from datetime import datetime

from sqlalchemy import func

from ..models import A2ATask, AgentConfig, AgentMemory, AgentTrace, AuditEvent, Deployment, Role, Sprint, WorkItem

bp = Blueprint("api", __name__, url_prefix="/api")

EDITABLE = {"name", "emoji", "specialty", "system_prompt", "provider", "model", "base_url", "temperature", "max_output_tokens",
            "timeout_s", "tools", "mcp_servers", "max_steps", "skills_path", "skills_allow", "context", "can_delegate_to",
            "a2a_url", "enabled"}


@bp.before_request
def _auth():
    token = os.environ.get("FLOW_API_TOKEN", "")
    if token and request.endpoint != "api.health":
        got = request.headers.get("Authorization", "").removeprefix("Bearer ") or request.headers.get("X-API-Token", "")
        if not hmac.compare_digest(got, token):
            return jsonify(error="não autorizado"), 401


def _bad(msg, code=400):
    return jsonify(error=msg), code


def _inside_root(raw: str) -> bool:
    if os.environ.get("FLOW_ALLOW_ANY_PATH") == "1":
        return True
    root = Path(current_app.config["ROOT"]).resolve()
    p = Path(raw)
    p = (p if p.is_absolute() else root / p).resolve()
    return p == root or root in p.parents


def _validate_patch(data: dict):
    """Retorna mensagem de erro ou None. Fecha vetores de abuso da configuração dinâmica."""
    if "provider" in data and data["provider"] not in llm.PROVIDERS:
        return f"provider deve ser um de {list(llm.PROVIDERS)}"
    if "tools" in data:
        if not isinstance(data["tools"], list):
            return "tools deve ser lista"
        bad = [t for t in data["tools"] if t not in toolreg.REGISTRY and "." not in t]
        if bad:
            return f"tools desconhecidas: {bad}"
    if "mcp_servers" in data:
        if not isinstance(data["mcp_servers"], dict):
            return "mcp_servers deve ser objeto"
        for n, c in data["mcp_servers"].items():
            if not isinstance(c, dict) or not ("url" in c or "command" in c):
                return f"mcp_servers.{n} precisa de 'url' ou 'command'"
            if "command" in c and os.environ.get("FLOW_ALLOW_STDIO_MCP") != "1":
                return "servidor MCP via 'command' (executa processo local) exige FLOW_ALLOW_STDIO_MCP=1; use 'url'"
    for key in ("temperature",):
        if key in data and not isinstance(data[key], (int, float)):
            return f"{key} deve ser número"
    for key in ("max_steps", "max_output_tokens", "timeout_s"):
        if key in data and (not isinstance(data[key], int) or data[key] < 1):
            return f"{key} deve ser inteiro >= 1"
    if "skills_path" in data:
        import re
        for part in re.split(r";|:(?![\\/])", data["skills_path"]):
            if part.strip() and not _inside_root(part.strip()):
                return f"skills_path fora do projeto: {part} (defina FLOW_ALLOW_ANY_PATH=1 para permitir)"
    if "context" in data:
        if not isinstance(data["context"], dict):
            return "context deve ser objeto"
        for part in data["context"].get("context_paths", []):
            if not _inside_root(part):
                return f"context_paths fora do projeto: {part}"
    if "can_delegate_to" in data:
        known = {a.slug for a in db.session.query(AgentConfig)}
        bad = [s for s in data["can_delegate_to"] if s not in known]
        if bad:
            return f"can_delegate_to com agentes inexistentes: {bad}"
    return None


def _last_error(sprint):
    """Último erro de sprint/refinamento (aparece no cabeçalho do dashboard)."""
    ev = (db.session.query(AuditEvent).filter(AuditEvent.workflow_id == sprint.workflow_id,
                                              AuditEvent.event_type.in_(["sprint_error", "refine_error"]))
          .order_by(AuditEvent.id.desc()).first())
    if not ev or (sprint.status == "done"):
        return None
    ok_after = db.session.query(AuditEvent).filter(AuditEvent.workflow_id == sprint.workflow_id, AuditEvent.id > ev.id,
                                                   AuditEvent.event_type == "ceremony").count()
    return None if ok_after else ev.payload.get("error")


# ------------------------------------------------------------------ estado
@bp.get("/health")
def health():
    return jsonify(ok=True)


@bp.get("/state")
def state():
    sprints = db.session.query(Sprint).order_by(Sprint.id.desc()).limit(10).all()
    current = next((s for s in sprints if s.status != "done"), sprints[0] if sprints else None)
    items = db.session.query(WorkItem).order_by(WorkItem.rank, WorkItem.id).all()
    shown = [i for i in items if i.status == "backlog" or (current and i.sprint_id == current.id)]
    done_points = sum(i.points for i in items if current and i.sprint_id == current.id and i.status == "done")
    last_done = next((s for s in sprints if s.status == "done"), None)
    tls = stages.timelines(shown)
    act = activity.snapshot()
    running = scrum.is_running(current.id) if current else False
    refining = scrum.refining(current.id) if current else False
    return jsonify(
        agents=[{**a.to_dict(), "activity": act.get(a.slug, {"state": "idle"})} for a in db.session.query(AgentConfig).order_by(AgentConfig.id)],
        items=[{**i.to_dict(), "timeline": tls[i.id]} for i in shown],
        sprint_timeline=stages.sprint_timeline(current, running, refining) if current else None,
        sprint=None if not current else {"id": current.id, "name": current.name, "goal": current.goal, "status": current.status,
                                         "capacity": current.capacity, "velocity": current.velocity, "retro": current.retro,
                                         "running": running, "refining": refining,
                                         "done_points": done_points, "error": _last_error(current)},
        sprints=[{"id": s.id, "name": s.name, "status": s.status, "velocity": s.velocity} for s in sprints],
        last_velocity=last_done.velocity if last_done else 0,
        deployments=[cd.refresh(d).to_dict() for d in db.session.query(Deployment).order_by(Deployment.id.desc()).limit(8)],
        worker=queue.worker_status(),
        audit_events=db.session.query(AuditEvent).count(),
    )


# ------------------------------------------------------------------ backlog / sprint
@bp.post("/refine")
def refine():
    """Pedido em linguagem natural → PO refina em user stories (em background)."""
    text = ((request.get_json(silent=True) or {}).get("request") or "").strip()
    if not text:
        return _bad("request vazio")
    sprint = db.session.query(Sprint).order_by(Sprint.id.desc()).first()
    if not sprint or sprint.status == "done":
        sprint = Sprint(name=f"Sprint {db.session.query(Sprint).count() + 1}")
        db.session.add(sprint)
        db.session.commit()
    job = queue.enqueue("refine", sprint_id=sprint.id, request=text, user=request.headers.get("X-User-Email", "human"))
    return jsonify(accepted=True, job=job), 202


@bp.post("/items")
def create_item():
    d = request.get_json(silent=True) or {}
    if not (d.get("title") or "").strip():
        return _bad("title obrigatório")
    from sqlalchemy import func
    rank = (db.session.query(func.max(WorkItem.rank)).scalar() or 0) + 1
    i = WorkItem(title=d["title"][:200], description=d.get("description", ""), points=int(d.get("points", 3)), rank=rank,
                 acceptance_criteria=d.get("acceptance_criteria", []), created_by="human")
    db.session.add(i)
    db.session.commit()
    stages.mark(i, "refine", "done", "human", f"{i.points} pts · criado manualmente")
    gitflow.commit_story(i, "human")
    audit.record(workflow_id="backlog", actor="human", event_type="backlog_change", target=i.key, payload=i.to_dict())
    return jsonify(i.to_dict()), 201


@bp.delete("/items/<int:iid>")
def delete_item(iid):
    """Remove uma história do Product Backlog (duplicatas etc.). Só itens em `backlog`; fica na auditoria e no histórico do git."""
    i = db.session.get(WorkItem, iid)
    if not i:
        return _bad("item não encontrado", 404)
    if i.status != WorkItem.BACKLOG:
        return _bad(f"{i.key} está em '{i.status}': só dá para remover itens do Product Backlog", 409)
    snap = i.to_dict()
    db.session.delete(i)
    db.session.commit()
    audit.record(workflow_id="backlog", actor=request.headers.get("X-User-Email", "human"), event_type="backlog_change", target=snap["key"], payload={"removed": snap})
    return jsonify(ok=True)


TEXT_EXT = {".md", ".txt", ".json", ".html", ".css", ".js", ".py", ".yaml", ".yml", ".csv", ".toml", ".sql"}


def _item_dir(i):
    if not i or not i.sprint_id:
        return None
    return Path(current_app.config["FLOW_WORKSPACE"]).resolve() / f"sprint-{i.sprint_id}" / i.key


def _git_ref(i):
    """(ref, prefix) do que a história entregou no git — para ver entregáveis de itens rejeitados/devolvidos (a branch não foi mergeada)."""
    last = next((c for c in gitflow.log(story=i.key, limit=30) if c["sprint"]), None)  # o commit de refinamento não tem sprint
    if not last:
        return None, None
    return f"feature/{i.key}", f"sprint-{last['sprint']}/{i.key}"


@bp.get("/items/<int:iid>/files")
def item_files(iid):
    """Entregáveis da história: do disco (workspace/sprint-N/US-xxx/) ou, se a branch não foi mergeada, do git."""
    i = db.session.get(WorkItem, iid)
    base = _item_dir(i)
    if base and base.is_dir() and any(base.rglob("*")):
        return jsonify(files=[{"path": str(f.relative_to(base)), "bytes": f.stat().st_size} for f in sorted(base.rglob("*")) if f.is_file()][:50], source="disk")
    ref, prefix = _git_ref(i) if i else (None, None)
    if ref:
        return jsonify(files=[{"path": f[len(prefix) + 1:], "bytes": 0} for f in gitflow.ls_tree(ref, prefix)][:50], source=f"git:{ref}")
    return jsonify(files=[])


@bp.get("/items/<int:iid>/files/<path:rel>")
def item_file(iid, rel):
    i = db.session.get(WorkItem, iid)
    base = _item_dir(i)
    if base:
        f = (base / rel).resolve()
        if base in f.parents and f.is_file():  # sem path traversal
            if f.suffix.lower() not in TEXT_EXT:
                return _bad("tipo de arquivo não exibível", 415)
            return jsonify(path=rel, content=f.read_text(encoding="utf-8", errors="replace")[:60000])
    ref, prefix = _git_ref(i) if i else (None, None)
    if ref and ".." not in Path(rel).parts and Path(rel).suffix.lower() in TEXT_EXT:
        txt = gitflow.read_blob(ref, f"{prefix}/{rel}")
        if txt is not None:
            return jsonify(path=rel, content=txt)
    return _bad("arquivo não encontrado", 404)


# ------------------------------------------------------------------ git
@bp.get("/git/status")
def git_status():
    return jsonify(gitflow.status())


@bp.get("/git/log")
def git_log():
    return jsonify(commits=gitflow.log(branch=request.args.get("branch", ""), story=request.args.get("story", ""), limit=int(request.args.get("limit", 60))))


@bp.get("/git/branches")
def git_branches():
    return jsonify(branches=gitflow.branches(), tags=gitflow.tags())


@bp.get("/git/commits/<sha>")
def git_commit(sha):
    c = gitflow.show(sha)
    return jsonify(c) if c else _bad("commit não encontrado", 404)


@bp.get("/items/<int:iid>/git")
def item_git(iid):
    i = db.session.get(WorkItem, iid)
    return jsonify(branch=f"feature/{i.key}", commits=gitflow.log(story=i.key, limit=40)) if i else _bad("item não encontrado", 404)


# ------------------------------------------------------------------ CI/CD local

@bp.get("/jobs")
def jobs():
    """Fila de jobs (procrastinate): o que está esperando/rodando/falhou + saúde do worker."""
    return jsonify(jobs=queue.recent_jobs(int(request.args.get("limit", 15))), worker=queue.worker_status())


@bp.get("/deployments")
def deployments():
    return jsonify(deployments=[cd.refresh(d).to_dict() for d in db.session.query(Deployment).order_by(Deployment.id.desc()).limit(30)])


@bp.get("/deployments/<int:did>/logs")
def deployment_logs(did):
    d = db.session.get(Deployment, did)
    return jsonify(log=cd.tail(d, int(request.args.get("lines", 80)))) if d else _bad("deployment não encontrado", 404)


@bp.post("/deployments/<int:did>/approve")
def deployment_approve(did):
    """Aprovação humana: libera o agente devops a subir a aplicação (a subida em si é feita pelo agente, em background)."""
    d = db.session.get(Deployment, did)
    if not d:
        return _bad("deployment não encontrado", 404)
    who = request.headers.get("X-User-Email", "human")
    try:
        cd.approve(d, who)
    except cd.CDError as e:
        return _bad(str(e), 409)
    queue.enqueue("cd_deploy", deployment_id=did, who=who)
    return jsonify(ok=True), 202


@bp.post("/deployments/<int:did>/ci")
def deployment_ci(did):
    d = db.session.get(Deployment, did)
    if not d:
        return _bad("deployment não encontrado", 404)
    if d.status in ("deploying", "running", "skipped") or not d.workdir:
        return _bad(f"release em '{d.status}' não pode rodar o CI agora", 409)
    cd._set(d, "ci_pending", "CI solicitado pelo humano")
    queue.enqueue("cd_ci", deployment_id=did, who=request.headers.get("X-User-Email", "human"))
    return jsonify(ok=True), 202


@bp.post("/deployments/<int:did>/stop")
def deployment_stop(did):
    d = db.session.get(Deployment, did)
    if not d:
        return _bad("deployment não encontrado", 404)
    queue.enqueue("cd_stop", deployment_id=did, who=request.headers.get("X-User-Email", "human"))  # quem encerra é o worker que subiu o processo
    return jsonify(result="encerramento solicitado ao worker"), 202


@bp.get("/activity")
def live_activity():
    """Quem está fazendo o quê agora + feed legível dos últimos acontecimentos."""
    return jsonify(agents=activity.snapshot(), feed=activity.feed(int(request.args.get("limit", 30))))


def _trace_row(r: AgentTrace) -> dict:
    return {"id": r.id, "kind": r.kind, "state": r.state, "title": r.title, "body": r.body, "result": r.result, "meta": r.meta or {}, "ms": r.ms,
            "at": r.created_at.isoformat(), "upd": r.updated_at.isoformat()}


@bp.get("/agents/<slug>/trace")
def agent_trace(slug):
    """Detalhe da tarefa de um agente: passo a passo da execução (último run por padrão; `run=` escolhe outro; `cursor=` devolve só o que mudou)."""
    a = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not a:
        return _bad("agente não encontrado", 404)
    runs = []
    for run_id, first, last, n in db.session.query(AgentTrace.run_id, func.min(AgentTrace.created_at), func.max(AgentTrace.updated_at), func.count()) \
            .filter_by(agent_slug=slug).group_by(AgentTrace.run_id).order_by(func.max(AgentTrace.id).desc()).limit(20):
        head = db.session.query(AgentTrace).filter_by(run_id=run_id).order_by(AgentTrace.id).first()
        fin = db.session.query(AgentTrace.state).filter_by(run_id=run_id, kind="final").first()
        runs.append({"run": run_id, "ceremony": head.ceremony, "item": head.item_key, "title": head.title, "started": first.isoformat(), "last": last.isoformat(),
                     "steps": n, "state": ("ok" if fin[0] == "ok" else "error") if fin else "running"})
    run = request.args.get("run") or (runs[0]["run"] if runs else "")
    q = db.session.query(AgentTrace).filter_by(agent_slug=slug, run_id=run)
    cursor = request.args.get("cursor")
    if cursor:
        try:
            q = q.filter(AgentTrace.updated_at > datetime.fromisoformat(cursor))
        except ValueError:
            return _bad("cursor inválido")
    rows = [_trace_row(r) for r in q.order_by(AgentTrace.id).limit(500)]
    meta = next((x for x in runs if x["run"] == run), None)
    return jsonify(agent={"slug": a.slug, "name": a.name, "emoji": a.emoji, "model": f"{a.provider}:{a.model}", "max_steps": a.max_steps,
                          "activity": activity.snapshot().get(a.slug, {"state": "idle"})},
                   runs=runs, run=meta, rows=rows, cursor=max([r["upd"] for r in rows], default=cursor or ""))


@bp.get("/items/<int:iid>/timeline")
def item_timeline(iid):
    i = db.session.get(WorkItem, iid)
    return (jsonify(stages.timelines([i])[i.id]) if i else _bad("item não encontrado", 404))


@bp.get("/sprints/<int:sid>/timeline")
def sprint_timeline(sid):
    s = db.session.get(Sprint, sid)
    return (jsonify(stages.sprint_timeline(s, scrum.is_running(sid), scrum.refining(sid))) if s else _bad("sprint não encontrada", 404))


@bp.post("/sprints")
def create_sprint():
    d = request.get_json(silent=True) or {}
    s = Sprint(name=d.get("name") or f"Sprint {db.session.query(Sprint).count() + 1}", goal=d.get("goal", ""), capacity=int(d.get("capacity", 13)))
    db.session.add(s)
    db.session.commit()
    return jsonify(id=s.id), 201


@bp.post("/sprints/<int:sid>/run")
def run_sprint(sid):
    s = db.session.get(Sprint, sid)
    if not s:
        return _bad("sprint não encontrada", 404)
    if s.status == "done":
        return _bad("sprint já encerrada")
    if scrum.refining(s.id):
        return _bad("o PO ainda está refinando o pedido — aguarde as user stories aparecerem no backlog", 409)
    if s.status == "planning" and not db.session.query(WorkItem).filter_by(status="backlog").count():
        return _bad("Product Backlog vazio — use “Pedir ao PO” antes de executar a sprint", 422)
    if scrum.busy() and not scrum.is_running(sid):
        return _bad("outra sprint está em execução — o workspace (repositório git) é único; aguarde ou pare a outra sprint", 409)
    job = queue.enqueue_run_sprint(s.id, request.headers.get("X-User-Email", "human"))
    return jsonify(started=bool(job), job=job), 202 if job else 409


@bp.post("/sprints/<int:sid>/stop")
def stop_sprint(sid):
    n = queue.stop_sprint(sid)
    audit.record(workflow_id=f"sprint-{sid}", actor="human", event_type="sprint_stop_requested", payload={"jobs": n})
    return jsonify(ok=True, jobs=n)


@bp.post("/chat")
def chat_with_po():
    """Conversa direta humano → agente (por padrão o PO), via A2A."""
    d = request.get_json(silent=True) or {}
    slug, text = d.get("agent", "product-owner"), (d.get("message") or "").strip()
    if not text:
        return _bad("message vazio")
    try:
        task = a2a.send_message("system", slug, text, ctx=toolreg.ToolContext(agent=db.session.query(AgentConfig).filter_by(slug=slug).first(),
                                workflow_id="chat", user_email=request.headers.get("X-User-Email", "human")),
                                context_id=d.get("context_id"), enforce=False)
    except toolreg.ToolError as e:
        return _bad(str(e), 404)
    return jsonify(task=task, reply=a2a.reply_text(task))


# ------------------------------------------------------------------ agentes (config dinâmica)
@bp.get("/agents/<slug>")
def get_agent(slug):
    a = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not a:
        return _bad("agente não encontrado", 404)
    d = a.to_dict()
    d["skills_found"] = [{"name": s.name, "description": s.description} for s in skl.discover(a).values()]
    d["memories"] = [{"id": m.id, "kind": m.kind, "scope": m.scope, "content": m.content, "importance": m.importance}
                     for m in db.session.query(AgentMemory).filter((AgentMemory.agent_id == a.id) | (AgentMemory.scope == "shared"))
                     .order_by(AgentMemory.id.desc()).limit(30)]
    return jsonify(d)


@bp.patch("/agents/<slug>")
def patch_agent(slug):
    a = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not a:
        return _bad("agente não encontrado", 404)
    data = request.get_json(silent=True) or {}
    unknown = set(data) - EDITABLE
    if unknown:
        return _bad(f"campos não editáveis: {sorted(unknown)}")
    if err := _validate_patch(data):
        return _bad(err, 422)
    before = {k: getattr(a, k) for k in data}
    for k, v in data.items():
        setattr(a, k, v)
    db.session.commit()
    audit.record(workflow_id="config", actor=request.headers.get("X-User-Email", "human"), event_type="agent_config_change",
                 target=slug, payload={"before": before, "after": data})
    return jsonify(a.to_dict())


@bp.post("/agents/<slug>/memory")
def add_memory(slug):
    a = db.session.query(AgentConfig).filter_by(slug=slug).first()
    d = request.get_json(silent=True) or {}
    if not a or not (d.get("content") or "").strip():
        return _bad("agente ou content inválido")
    m = mem.save(a, d["content"], kind=d.get("kind", "fact"), scope=d.get("scope", "agent"), importance=d.get("importance", 3))
    return jsonify(id=m.id), 201


@bp.get("/tools")
def list_tools():
    return jsonify([{"name": t.name, "description": t.description, "roles": list(t.roles), "mutating": t.mutating, "params": t.params()}
                    for t in toolreg.REGISTRY.values()])


@bp.get("/models")
def models():
    return jsonify(provider="ollama", models=llm.list_ollama_models(request.args.get("base_url", "")), providers=list(llm.PROVIDERS))


# ------------------------------------------------------------------ auditoria / A2A
@bp.get("/audit")
def audit_list():
    q = db.session.query(AuditEvent).order_by(AuditEvent.id.desc())
    for col, arg in ((AuditEvent.actor, "actor"), (AuditEvent.event_type, "type"), (AuditEvent.workflow_id, "workflow"), (AuditEvent.status, "status")):
        if request.args.get(arg):
            q = q.filter(col == request.args[arg])
    return jsonify([e.to_dict() for e in q.limit(min(int(request.args.get("limit", 100)), 500))])


@bp.get("/audit/verify")
def audit_verify():
    ok, bad, total = audit.verify_chain()
    return jsonify(ok=ok, first_invalid=bad, total=total)


@bp.get("/a2a/tasks")
def a2a_tasks():
    rows = db.session.query(A2ATask).order_by(A2ATask.created_at.desc()).limit(50).all()
    return jsonify([{"id": t.id, "agent": t.agent.slug, "state": t.state, "context_id": t.context_id,
                     "created_at": t.created_at.isoformat(), **{k: (t.meta or {}).get(k) for k in ("steps", "tokensIn", "tokensOut")}} for t in rows])
