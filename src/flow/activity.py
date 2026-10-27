"""Atividade ao vivo dos agentes: quem está fazendo o quê (estado persistido, lido pelo dashboard)."""
import ast
import re
from datetime import timedelta

from .extensions import db
from .models import AgentActivity, AgentConfig, AuditEvent, now

CEREMONY = {"refine": "refinando o pedido", "goal": "definindo a meta da sprint", "plan": "planejando a sprint",
            "implement": "desenvolvendo", "verify": "verificando (QA)", "tests": "escrevendo os testes (QA)", "accept": "avaliando o aceite", "retro": "conduzindo a retrospectiva",
            "chat": "respondendo uma mensagem"}


def _names(items, n=6):
    items = list(dict.fromkeys(items))
    return ", ".join(items[:n]) + (f" (+{len(items) - n})" if len(items) > n else "")


def _py(path: str, text: str):
    """Python: classes, funções, rotas, modelos SQLAlchemy, migração Alembic — ou o erro de sintaxe."""
    low = text.lower()
    alembic = "alembic" in path.lower() or "migrations/versions" in path.lower() or "from alembic import" in low or re.search(r"^revision\s*=", text, re.M)
    if alembic:
        ops = re.findall(r"op\.(create_table|drop_table|add_column|drop_column|create_index|alter_column)\(\s*['\"]([\w.]+)", text)
        return "🗄️", "migração Alembic" + (": " + _names([f"{o} {t}" for o, t in ops]) if ops else "")
    if re.search(r"(^|/)(tests?/|test_)", path):
        try:
            fns = [n.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
        except SyntaxError as e:
            return "🧪", f"teste com erro de sintaxe (linha {e.lineno})"
        kind = "teste Playwright (navegador)" if "playwright" in low else "teste pytest"
        return "🧪", f"{kind}: {_names(fns) or 'sem funções test_*'}"
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return "🐍", f"Python com erro de sintaxe (linha {e.lineno}: {str(e.msg)[:40]})"
    classes, funcs, routes, tables, imports = [], [], [], [], set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            classes.append(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(n.name)
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("route", "get", "post", "put", "delete", "patch") and d.args \
                        and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                    routes.append(d.args[0].value)
        elif isinstance(n, ast.Import):
            imports.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imports.add(n.module.split(".")[0])
    tables = re.findall(r"__tablename__\s*=\s*['\"](\w+)", text)
    parts = []
    if tables or "sqlalchemy" in imports:
        parts.append("modelos SQLAlchemy " + (_names(classes) + (f" (tabelas {_names(tables)})" if tables else "") if classes else "(sessão/engine)"))
    elif classes:
        parts.append("classes " + _names(classes))
    if routes:
        parts.append("rotas " + _names(routes))
    others = [f for f in funcs if not f.startswith("_")]
    if others:
        parts.append("funções " + _names(others))
    if imports & {"flask", "fastapi", "django"}:
        parts.append("framework " + _names(sorted(imports & {"flask", "fastapi", "django"})))
    return "🐍", "; ".join(parts) or "módulo sem classes/funções"


def classify_write(path: str, content: str = "") -> tuple[str, str, str]:
    """(ícone, rótulo, resumo do conteúdo) de um arquivo que o agente está escrevendo. Resumo vazio se não há o que dizer."""
    pl, name = path.lower(), path.rsplit("/", 1)[-1].lower()
    text = content or ""
    if name in ("requirements.txt", "requirements-dev.txt") or (name == "pyproject.toml"):
        deps = [re.split(r"[<>=!~\[;\s]", ln.strip(), 1)[0] for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "["))]
        deps = [d for d in deps if re.fullmatch(r"[A-Za-z0-9_.\-]+", d or "")]
        return "📦", "dependências", _names(deps) if deps else ""
    if name == "flow-app.toml":
        return "🚀", "manifesto de deploy", re.search(r'name\s*=\s*"([^"]+)"', text).group(1) if re.search(r'name\s*=\s*"', text) else ""
    if pl.endswith(".sql"):
        stm = re.findall(r"(create\s+table(?:\s+if\s+not\s+exists)?|alter\s+table|create\s+index|insert\s+into|drop\s+table)\s+([\w.\"`]+)", text, re.I)
        return "🗄️", "script SQL", _names([f"{a.upper().split()[0]} {a.upper().split()[1] if len(a.split()) > 1 and a.split()[1].upper() == 'TABLE' else ''} {b}".replace("  ", " ") for a, b in stm])
    if pl.endswith(".py"):
        icon, resumo = _py(path, text)
        return icon, "código Python" if icon == "🐍" else ("migração" if icon == "🗄️" else "teste"), resumo
    if pl.endswith((".html", ".htm", ".jinja", ".j2")):
        bits = []
        if re.search(r"<form", text, re.I):
            bits.append(f"formulário com {len(re.findall(r'<input', text, re.I))} campo(s)")
        if re.search(r"tailwind", text, re.I):
            bits.append("TailwindCSS")
        if re.search(r"\{%\s*extends|\{\{", text):
            bits.append("template Jinja")
        return "🌐", "HTML", "; ".join(bits)
    if pl.endswith(".css"):
        return "🎨", "CSS", ""
    if pl.endswith((".js", ".ts", ".jsx", ".tsx")):
        return "🟨", "JavaScript", ""
    if pl.endswith((".md", ".txt", ".rst")):
        return "📝", "documentação", ""
    return "📄", "arquivo", ""


def describe_tool(name: str, args: dict | None = None) -> str:
    a = args or {}
    q = lambda k: str(a.get(k, ""))[:60]  # noqa: E731
    return {
        "backlog_create": lambda: f"criando a user story “{q('title')}”",
        "backlog_prioritize": lambda: "priorizando o backlog",
        "backlog_list": lambda: "consultando o backlog",
        "sprint_add_item": lambda: f"colocando o item {q('item_id')} no sprint (para {q('assignee')})",
        "sprint_set_retro": lambda: "registrando a retrospectiva",
        "write_workspace_file": lambda: _write_text(a),
        "read_workspace_file": lambda: f"lendo o arquivo {q('path')}",
        "list_workspace": lambda: "listando os arquivos do sprint",
        "work_item_update": lambda: f"atualizando o progresso do item {q('item_id')}",
        "work_item_verdict": lambda: f"registrando o veredito do QA ({'aprovado' if a.get('passed') else 'reprovado'})",
        "work_item_accept": lambda: f"decidindo o aceite do item {q('item_id')} ({'aceito' if a.get('accepted') else 'rejeitado'})",
        "memory_save": lambda: "salvando uma memória",
        "memory_search": lambda: f"consultando a memória: “{q('query')}”",
        "load_skill": lambda: f"lendo a skill {q('name')}",
        "list_skills": lambda: "listando skills",
        "delegate_to_agent": lambda: f"delegando a {q('agent')}",
        "audit_query": lambda: "consultando a auditoria",
    }.get(name, lambda: f"usando a ferramenta {name}")()


def _write_text(a: dict) -> str:
    path = str(a.get("path", ""))[:80]
    icon, label, resumo = classify_write(path, str(a.get("content", "")))
    return f"{icon} escrevendo {label} {path}" + (f" — {resumo}" if resumo else "")


def _row(agent):
    row = db.session.get(AgentActivity, agent.id)
    if not row:
        row = AgentActivity(agent_id=agent.id)
        db.session.add(row)
    return row


def start(agent, task):
    cer = task.get("ceremony", "chat") if isinstance(task, dict) else "chat"
    item = (task.get("item") or {}).get("key", "") if isinstance(task, dict) else ""
    r = _row(agent)
    r.state, r.ceremony, r.item_key, r.action, r.step, r.max_steps = "working", cer, item, "lendo o contexto", 0, agent.max_steps
    r.started_at = r.updated_at = now()
    db.session.commit()


def update(agent, action: str, step: int | None = None):
    r = _row(agent)
    r.action, r.updated_at = action, now()
    if step is not None:
        r.step = step
    db.session.commit()


def finish(agent, last: str = ""):
    r = _row(agent)
    r.state, r.action, r.last_result, r.updated_at = "idle", "", (last or "")[:200], now()
    db.session.commit()


def snapshot() -> dict:
    """{slug: atividade}. `working` sem sinal há mais que timeout+60s (servidor reiniciado no meio) vira ocioso."""
    out = {}
    rows = {r.agent_id: r for r in db.session.query(AgentActivity)}
    for a in db.session.query(AgentConfig):
        r = rows.get(a.id)
        if not r:
            out[a.slug] = {"state": "idle"}
            continue
        stale = r.state == "working" and now() - r.updated_at > timedelta(seconds=a.timeout_s + 60)
        working = r.state == "working" and not stale
        out[a.slug] = {"state": "working" if working else "idle", "ceremony": r.ceremony if working else "",
                       "what": CEREMONY.get(r.ceremony, r.ceremony) if working else "", "item": r.item_key if working else "",
                       "action": r.action if working else "", "step": r.step, "max_steps": r.max_steps,
                       "since": r.started_at.isoformat() if working and r.started_at else None,
                       "updated_at": r.updated_at.isoformat(), "last_result": r.last_result}
    return out


def feed(limit: int = 30) -> list[dict]:
    """Últimos acontecimentos em frases legíveis (“Dev Fullstack escreveu US-001/entrega.md”)."""
    names = {a.slug: (a.name, a.emoji) for a in db.session.query(AgentConfig)}
    out = []
    for e in db.session.query(AuditEvent).filter(AuditEvent.event_type.in_(["tool_call", "a2a_send", "ceremony", "agent_config_change", "sprint_error", "refine_error", "sandbox_phase"])) \
            .order_by(AuditEvent.id.desc()).limit(limit * 3):
        nm, em = names.get(e.actor, (e.actor, "👤"))
        p = e.payload or {}
        if e.event_type == "tool_call":
            if e.target in ("memory_search", "memory_save", "backlog_list", "list_skills", "load_skill", "read_skill_file") and e.status == "ok":
                continue  # ruído
            res = p.get("result")
            text = res["o_que"] if e.target == "write_workspace_file" and isinstance(res, dict) and res.get("o_que") else describe_tool(e.target, p.get("args"))
            text = ("tentou " if e.status != "ok" else "") + text + (f" — {p.get('reason') or p.get('error')}" if e.status != "ok" else "")
        elif e.event_type == "a2a_send":
            text = f"enviou {'uma tarefa de ' + CEREMONY.get(p.get('ceremony'), p.get('ceremony')) if p.get('ceremony') else 'uma mensagem'} para {p.get('to', e.target)}" + (" — NEGADO pela hierarquia" if e.status == "denied" else "")
        elif e.event_type == "ceremony":
            text = {"refine": "iniciou o refinamento", "plan_start": "iniciou o planning", "plan_done": "concluiu o planning", "rework": f"pediu retrabalho de {p.get('item')}",
                    "sprint_closed": "encerrou a sprint", "paused": "pausou a sprint", "impediment": f"registrou impedimento em {p.get('item')}"}.get(e.target, f"cerimônia {e.target}")
        elif e.event_type == "sandbox_phase":
            text = p.get("text", "sandbox")
        elif e.event_type == "agent_config_change":
            text = f"alterou a configuração de {e.target}"
        else:
            text = f"ERRO: {p.get('error', '')}"[:160]
        out.append({"at": e.created_at.isoformat(), "actor": e.actor, "actor_name": nm, "emoji": em, "text": text, "status": e.status})
        if len(out) >= limit:
            break
    return out


def note(slug: str, action: str, ceremony: str = "verify", item: str = ""):
    """Atualiza a atividade de um agente por slug fora do loop dele (ex.: o runner instalando dependências / rodando pytest no sandbox)."""
    a = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not a:
        return
    r = _row(a)
    if r.state != "working":
        r.state, r.ceremony, r.item_key, r.step, r.max_steps, r.started_at = "working", ceremony, item, 0, a.max_steps, now()
    r.action, r.updated_at = action, now()
    db.session.commit()


def release(slug: str, last: str = ""):
    a = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if a:
        finish(a, last)
