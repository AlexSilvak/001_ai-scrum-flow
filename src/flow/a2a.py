"""A2A (Agent2Agent) v1.0 sobre JSON-RPC 2.0 — servidor e cliente.

Servidor: cada agente é um servidor A2A em  /a2a/<slug>/  com Agent Card em
          /a2a/<slug>/.well-known/agent-card.json. Métodos: SendMessage, GetTask, ListTasks,
          CancelTask (+ aliases 0.3: message/send, tasks/get, tasks/cancel).
Cliente:  `send_message()` fala com agentes locais (mesmo handler, sem rede) ou remotos (HTTP).
Hierarquia Scrum: `can_delegate_to` do agente remetente é verificado antes de qualquer envio.
Não implementado (declarado no card): streaming SSE e push notifications.
"""
import json
import threading
import uuid

import httpx
from flask import current_app

from . import audit, runtime
from . import skills as skl
from . import tools as toolreg
from .extensions import db
from .models import A2ATask, AgentConfig

VERSION = "1.0"
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
MAX_DEPTH = 3

ERR = {  # código JSON-RPC por erro (spec §5.4 / §9.5)
    "parse": (-32700, "Invalid JSON payload"), "request": (-32600, "Request payload validation error"),
    "method": (-32601, "Method not found"), "params": (-32602, "Invalid parameters"),
    "internal": (-32603, "Internal error"), "task_not_found": (-32001, "Task not found"),
    "not_cancelable": (-32002, "Task cannot be canceled"), "push": (-32003, "Push notifications not supported"),
    "unsupported": (-32004, "Unsupported operation"), "extended": (-32007, "Extended agent card not configured"),
    "version": (-32009, "A2A version not supported"),
}


class A2AError(Exception):
    def __init__(self, kind, detail=""):
        self.kind, self.detail = kind, detail
        super().__init__(detail or ERR[kind][1])


# ------------------------------------------------------------------ Agent Card
def public_url() -> str:
    return current_app.config["FLOW_PUBLIC_URL"].rstrip("/")


def agent_card(agent: AgentConfig) -> dict:
    skills = [{
        "id": f"role-{agent.scrum_role}",
        "name": f"{agent.role_label}{' — ' + agent.specialty if agent.specialty else ''}",
        "description": (agent.system_prompt or agent.name)[:300],
        "tags": ["scrum", agent.scrum_role, *( [agent.specialty] if agent.specialty else [])],
        "examples": [],
    }]
    for s in skl.discover(agent).values():
        skills.append({"id": f"skill-{s.name}", "name": s.name, "description": s.description or s.name, "tags": ["skill"]})
    card = {
        "name": agent.name,
        "description": f"{agent.role_label} do time AI Scrum Flow. {agent.system_prompt[:200]}".strip(),
        "supportedInterfaces": [{"url": f"{public_url()}/a2a/{agent.slug}/", "protocolBinding": "JSONRPC", "protocolVersion": VERSION}],
        "provider": {"organization": "AI Scrum Flow", "url": public_url()},
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": skills,
    }
    if current_app.config["A2A_BEARER_TOKEN"]:
        card["securitySchemes"] = {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}
        card["securityRequirements"] = [{"schemes": {"bearer": {"list": []}}}]
    return card


# ------------------------------------------------------------------ (de)serialização
def _text_of(message: dict) -> str:
    return "\n".join(p["text"] for p in message.get("parts", []) if isinstance(p, dict) and "text" in p)


def _data_of(message: dict):
    for p in message.get("parts", []):
        if isinstance(p, dict) and isinstance(p.get("data"), dict):
            return p["data"]
    return None


def _agent_msg(task: A2ATask, text: str) -> dict:
    return {"messageId": str(uuid.uuid4()), "contextId": task.context_id, "taskId": task.id,
            "role": "ROLE_AGENT", "parts": [{"text": text, "mediaType": "text/plain"}]}


def task_json(t: A2ATask, history_length=None) -> dict:
    hist = t.history if history_length is None else (t.history[-history_length:] if history_length else [])
    ts = t.updated_at.replace(tzinfo=None).isoformat() + "Z"
    out = {"id": t.id, "contextId": t.context_id, "status": {"state": t.state, "timestamp": ts},
           "artifacts": t.artifacts, "history": hist, "metadata": t.meta}
    if t.status_message:
        out["status"]["message"] = t.status_message
    return out


# ------------------------------------------------------------------ servidor
def _validate_message(m) -> dict:
    if not isinstance(m, dict) or not isinstance(m.get("parts"), list) or not m["parts"]:
        raise A2AError("params", "message.parts é obrigatório")
    role = {"user": "ROLE_USER", "agent": "ROLE_AGENT"}.get(m.get("role"), m.get("role"))
    if role not in ("ROLE_USER", "ROLE_AGENT"):
        raise A2AError("params", "message.role inválido")
    parts = []
    for p in m["parts"]:  # aceita partes 0.3 ({"kind":"text"}) e 1.0
        if not isinstance(p, dict) or not any(k in p for k in ("text", "data", "url", "raw")):
            raise A2AError("params", "part inválida")
        parts.append({k: v for k, v in p.items() if k != "kind"})
    return {**m, "role": role, "parts": parts, "messageId": m.get("messageId") or str(uuid.uuid4())}


def _execute(task_id: str, agent_slug: str, message: dict):
    """Roda o agente e fecha a task. Executa síncrono ou em thread (returnImmediately)."""
    t = db.session.get(A2ATask, task_id)
    agent = t.agent
    md = message.get("metadata") or {}
    ctx = toolreg.ToolContext(agent=agent, workflow_id=md.get("workflowId", f"a2a-{t.context_id[:8]}"),
                              user_email=md.get("userEmail", "system"), sprint_id=md.get("sprintId"),
                              depth=int(md.get("depth", 0)))
    data = _data_of(message)
    text = _text_of(message)
    payload = {**data, "text": text} if data and "ceremony" in data else (text or json.dumps(data or {}))
    t.state = "TASK_STATE_WORKING"
    db.session.commit()
    try:
        res = runtime.run(agent, payload, ctx=ctx, context_id=t.context_id, task_id=t.id)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        res = runtime.RunResult(text=f"Erro interno: {e}", ok=False, error=str(e))
    db.session.refresh(t)
    if t.state == "TASK_STATE_CANCELED":
        return
    reply = _agent_msg(t, res.text)
    t.history = [*t.history, reply]
    t.state = "TASK_STATE_COMPLETED" if res.ok else "TASK_STATE_FAILED"
    t.status_message = reply
    t.artifacts = [{"artifactId": str(uuid.uuid4()), "name": "resposta", "parts": [{"text": res.text, "mediaType": "text/plain"}]}]
    t.meta = {**(t.meta or {}), "steps": res.steps, "tokensIn": res.tokens_in, "tokensOut": res.tokens_out, "toolCalls": res.tool_calls}
    db.session.commit()
    audit.record(workflow_id=ctx.workflow_id, actor=agent.slug, event_type="a2a_result",
                 status="ok" if res.ok else "error", target=t.id, payload={"state": t.state, "reply": res.text, **t.meta})


def _send_message(agent: AgentConfig, params: dict) -> dict:
    msg = _validate_message(params.get("message"))
    cfg = params.get("configuration") or {}
    md = msg.get("metadata") or {}
    if int(md.get("depth", 0)) > MAX_DEPTH:
        raise A2AError("unsupported", f"profundidade máxima de delegação ({MAX_DEPTH}) excedida")
    if msg["role"] != "ROLE_USER":
        raise A2AError("params", "SendMessage espera role ROLE_USER")

    if msg.get("taskId"):
        task = db.session.query(A2ATask).filter_by(id=msg["taskId"], agent_id=agent.id).first()
        if not task:
            raise A2AError("task_not_found")
        if task.state in TERMINAL:
            raise A2AError("unsupported", "task em estado terminal; inicie uma nova")
    else:
        task = A2ATask(agent_id=agent.id, context_id=msg.get("contextId") or str(uuid.uuid4()), history=[], artifacts=[], meta={})
        db.session.add(task)
        db.session.flush()
    msg.update(taskId=task.id, contextId=task.context_id)
    task.history = [*(task.history or []), msg]
    db.session.commit()

    wf = md.get("workflowId", f"a2a-{task.context_id[:8]}")
    audit.record(workflow_id=wf, actor=md.get("from", "external"), event_type="a2a_send", target=f"{agent.slug}:{task.id}",
                 payload={"to": agent.slug, "text": _text_of(msg), "ceremony": (_data_of(msg) or {}).get("ceremony")})

    if not agent.enabled:
        task.state = "TASK_STATE_REJECTED"
        task.status_message = _agent_msg(task, f"Agente {agent.slug} está desabilitado.")
        db.session.commit()
    elif cfg.get("returnImmediately"):
        app = current_app._get_current_object()

        def bg():
            with app.app_context():
                _execute(task_id, agent.slug, msg)

        task_id = task.id
        threading.Thread(target=bg, daemon=True).start()
    else:
        _execute(task.id, agent.slug, msg)
    db.session.refresh(task)
    return {"task": task_json(task, cfg.get("historyLength"))}


def _get_task(agent, params):
    t = db.session.query(A2ATask).filter_by(id=params.get("id", ""), agent_id=agent.id).first()
    if not t:
        raise A2AError("task_not_found")
    return task_json(t, params.get("historyLength"))


def _list_tasks(agent, params):
    qs = db.session.query(A2ATask).filter_by(agent_id=agent.id).order_by(A2ATask.created_at.desc())
    if params.get("contextId"):
        qs = qs.filter(A2ATask.context_id == params["contextId"])
    if params.get("status"):
        qs = qs.filter(A2ATask.state == params["status"])
    size = max(1, min(100, int(params.get("pageSize") or 50)))
    offset = int(params.get("pageToken") or 0)
    total = qs.count()
    page = qs.offset(offset).limit(size).all()
    nxt = str(offset + size) if offset + size < total else ""
    return {"tasks": [task_json(t, params.get("historyLength")) for t in page],
            "nextPageToken": nxt, "pageSize": size, "totalSize": total}


def _cancel_task(agent, params):
    t = db.session.query(A2ATask).filter_by(id=params.get("id", ""), agent_id=agent.id).first()
    if not t:
        raise A2AError("task_not_found")
    if t.state in TERMINAL:
        raise A2AError("not_cancelable")
    t.state = "TASK_STATE_CANCELED"
    db.session.commit()
    audit.record(workflow_id=f"a2a-{t.context_id[:8]}", actor="external", event_type="a2a_cancel", target=t.id)
    return task_json(t)


METHODS = {
    "SendMessage": _send_message, "message/send": _send_message,
    "GetTask": _get_task, "tasks/get": _get_task,
    "ListTasks": _list_tasks,
    "CancelTask": _cancel_task, "tasks/cancel": _cancel_task,
}


def _error(id_, kind, detail=""):
    code, msg = ERR[kind]
    err = {"code": code, "message": msg}
    if detail:
        err["data"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": kind, "metadata": {"detail": detail}}]
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def handle_jsonrpc(slug: str, body, version_header: str = "") -> dict:
    """Processa uma requisição JSON-RPC para o agente `slug` (mesmo código para HTTP e in-process)."""
    id_ = body.get("id") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return _error(id_, "request")
    if version_header and version_header.split(".")[0] != VERSION.split(".")[0] and version_header != "0.3":
        return _error(id_, "version", f"suportado: {VERSION}")
    agent = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not agent:
        return _error(id_, "task_not_found", f"agente '{slug}' não existe")
    method = body["method"]
    if method == "GetExtendedAgentCard":
        return _error(id_, "extended")
    fn = METHODS.get(method)
    if not fn:
        return _error(id_, "method", method)
    try:
        return {"jsonrpc": "2.0", "id": id_, "result": fn(agent, body.get("params") or {})}
    except A2AError as e:
        return _error(id_, e.kind, e.detail)
    except (ValueError, TypeError) as e:
        return _error(id_, "params", str(e))
    except Exception as e:  # noqa: BLE001
        return _error(id_, "internal", repr(e))


# ------------------------------------------------------------------ cliente
def _rpc(target: AgentConfig, method: str, params: dict, timeout=300) -> dict:
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    if not target.a2a_url:  # agente local: mesmo handler, sem rede
        return handle_jsonrpc(target.slug, body, VERSION)
    base = target.a2a_url.rstrip("/")
    headers = {"A2A-Version": VERSION, "Content-Type": "application/json"}
    token = current_app.config["A2A_BEARER_TOKEN"]
    if token:
        headers["Authorization"] = f"Bearer {token}"
    card_url = base if base.endswith(".json") else f"{base}/.well-known/agent-card.json"
    card = httpx.get(card_url, timeout=15).json()  # descoberta
    iface = next((i for i in card.get("supportedInterfaces", []) if i.get("protocolBinding") == "JSONRPC"), None)
    if not iface:
        raise A2AError("unsupported", "agente remoto não expõe interface JSONRPC")
    return httpx.post(iface["url"], json=body, headers=headers, timeout=timeout).json()


def send_message(sender: str, target_slug: str, text: str, *, data: dict | None = None, ctx=None,
                 context_id: str | None = None, enforce: bool = True) -> dict:
    """Envia SendMessage de `sender` (slug) para `target_slug`. Devolve a Task A2A (dict)."""
    wf = ctx.workflow_id if ctx else "adhoc"
    target = db.session.query(AgentConfig).filter_by(slug=target_slug).first()
    src = db.session.query(AgentConfig).filter_by(slug=sender).first()
    if not target:
        raise toolreg.ToolError(f"agente '{target_slug}' não existe")
    if enforce and src and target_slug not in (src.can_delegate_to or []):
        audit.record(workflow_id=wf, actor=sender, event_type="a2a_send", status="denied", target=target_slug,
                     payload={"reason": "fora da hierarquia (can_delegate_to)", "allowed": src.can_delegate_to})
        raise toolreg.ToolError(f"{sender} não pode delegar a {target_slug} (hierarquia Scrum). Permitidos: {src.can_delegate_to}")
    parts = [{"text": text, "mediaType": "text/plain"}]
    if data:
        parts.append({"data": data, "mediaType": "application/json"})
    msg = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": parts,
           "metadata": {"from": sender, "workflowId": wf, "sprintId": getattr(ctx, "sprint_id", None),
                        "userEmail": getattr(ctx, "user_email", "system"), "depth": (getattr(ctx, "depth", 0) + 1) if ctx else 0}}
    if context_id:
        msg["contextId"] = context_id
    resp = _rpc(target, "SendMessage", {"message": msg, "configuration": {"historyLength": 0}})
    if "error" in resp:
        raise toolreg.ToolError(f"A2A {target_slug}: {resp['error']['message']} {resp['error'].get('data', '')}")
    return resp["result"]["task"]


def reply_text(task: dict) -> str:
    m = (task.get("status") or {}).get("message") or {}
    return _text_of(m) or "\n".join(_text_of({"parts": a["parts"]}) for a in task.get("artifacts", []))


def delegate(ctx, agent_slug: str, message: str) -> dict:
    """Entrada da tool `delegate_to_agent`."""
    task = send_message(ctx.agent.slug, agent_slug, message, ctx=ctx)
    return {"task_id": task["id"], "state": task["status"]["state"], "reply": reply_text(task)}
