"""Registro único de tools. Fonte de verdade para: prompt dos agentes, MCP server e auditoria.

Uma tool = função tipada `fn(ctx, **args)`. O que cada agente pode chamar é a allowlist
`AgentConfig.tools` (configurável); `roles=` é uma trava extra de regra Scrum (defesa em profundidade).
"""
import inspect
import json
import re
import typing
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from pathlib import Path

from flask import current_app
from sqlalchemy import func

from . import activity, audit, cd, gitflow, sandbox
from . import memory as mem
from . import stages
from . import skills as skl
from .domain.permissoes import conceder_permissao as _conceder
from .domain.permissoes import revogar_permissao as _revogar
from .domain.permissoes import verificar_autoridade
from .domain.usuarios import criar_usuario as _criar
from .extensions import db
from .models import AgentConfig, Deployment, Role, Sprint, WorkItem


class ToolError(Exception):
    """Erro que volta para o LLM como resultado da tool (ele pode se corrigir)."""


@dataclass
class ToolContext:
    agent: AgentConfig
    workflow_id: str = "adhoc"
    user_email: str = "system"
    sprint_id: int | None = None
    depth: int = 0  # profundidade de delegação A2A (anti-loop)
    extra: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    fn: typing.Callable
    description: str
    roles: tuple = ()
    mutating: bool = False

    def params(self):
        out = []
        for n, p in list(inspect.signature(self.fn).parameters.items())[1:]:
            ann = typing.get_type_hints(self.fn).get(n, str)
            out.append({"name": n, "type": _jtype(ann), "required": p.default is inspect._empty,
                        "default": None if p.default is inspect._empty else p.default})
        return out

    def json_schema(self):
        props = {p["name"]: {"type": p["type"]} for p in self.params()}
        return {"type": "object", "properties": props, "required": [p["name"] for p in self.params() if p["required"]]}


def _jtype(ann) -> str:
    origin = typing.get_origin(ann)
    if ann is list or origin is list:
        return "array"
    if origin is dict or ann is dict:
        return "object"
    return {int: "integer", float: "number", bool: "boolean", str: "string"}.get(ann, "string")


REGISTRY: dict[str, Tool] = {}


def tool(*, roles=(), mutating=False):
    def deco(fn):
        doc = inspect.getdoc(fn) or fn.__name__
        REGISTRY[fn.__name__] = Tool(fn.__name__, fn, doc.strip().split("\n\n")[0], tuple(roles), mutating)
        return fn
    return deco


def _coerce(value, jt):
    if value is None:
        return value
    try:
        if jt == "integer":
            return int(value)
        if jt == "number":
            return float(value)
        if jt == "boolean":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "sim", "yes")
        if jt == "array" and isinstance(value, str):
            v = json.loads(value) if value.strip().startswith("[") else [x.strip() for x in value.split("\n") if x.strip()]
            return v
        if jt == "object" and isinstance(value, str):
            return json.loads(value)
    except (ValueError, json.JSONDecodeError) as e:
        raise ToolError(f"argumento inválido ({jt}): {e}")
    return value


def call(name: str, ctx: ToolContext, args: dict | None):
    """Executa uma tool interna com validação, checagem de allowlist/papel e auditoria."""
    args = dict(args or {})
    t = REGISTRY.get(name)

    def deny(reason):
        audit.record(workflow_id=ctx.workflow_id, actor=ctx.agent.slug, event_type="tool_call", status="denied",
                     target=name, payload={"args": args, "reason": reason})
        raise ToolError(reason)

    if not t and name in skl.discover(ctx.agent) and "load_skill" in (ctx.agent.tools or []):
        # modelo pequeno chamou o NOME DE UMA SKILL como se fosse ferramenta: entrega a skill em vez de falhar
        audit.record(workflow_id=ctx.workflow_id, actor=ctx.agent.slug, event_type="tool_redirect", target=name, payload={"to": "load_skill"})
        return load_skill(ctx, name)
    if not t:
        deny(f"tool '{name}' não existe (se é uma skill, use load_skill com name='{name}')")
    if name not in (ctx.agent.tools or []) and name not in BASELINE:
        deny(f"tool '{name}' não está habilitada para o agente {ctx.agent.slug}")
    if t.roles and ctx.agent.scrum_role not in t.roles:
        deny(f"tool '{name}' é restrita aos papéis {list(t.roles)} (agente é {ctx.agent.scrum_role})")

    declared = {p["name"]: p for p in t.params()}
    unknown = set(args) - set(declared)
    if unknown:
        raise ToolError(f"argumentos desconhecidos: {sorted(unknown)}; esperados: {list(declared)}")
    missing = [n for n, p in declared.items() if p["required"] and n not in args]
    if missing:
        raise ToolError(f"argumentos obrigatórios ausentes: {missing}")
    for n, v in list(args.items()):
        args[n] = _coerce(v, declared[n]["type"])

    try:
        result = t.fn(ctx, **args)
    except ToolError as e:
        audit.record(workflow_id=ctx.workflow_id, actor=ctx.agent.slug, event_type="tool_call", status="error",
                     target=name, payload={"args": args, "error": str(e)})
        raise
    audit.record(workflow_id=ctx.workflow_id, actor=ctx.agent.slug, event_type="tool_call", status="ok",
                 target=name, payload={"args": args, "result": result})
    return result


# ------------------------------------------------------------------ helpers
def _item(ref) -> WorkItem:
    try:
        pk = int(str(ref).upper().replace("US-", ""))
    except ValueError:
        raise ToolError(f"item '{ref}' inválido")
    item = db.session.get(WorkItem, pk)
    if not item:
        raise ToolError(f"item '{ref}' não encontrado")
    return item


def _ser(i: WorkItem) -> dict:
    return {"id": i.id, "key": i.key, "title": i.title, "status": i.status, "points": i.points,
            "assignee": i.assignee.slug if i.assignee else None, "acceptance_criteria": i.acceptance_criteria}


def workspace_root(ctx: ToolContext) -> Path:
    root = Path(current_app.config["FLOW_WORKSPACE"]) / "".join(c for c in ctx.workflow_id if c.isalnum() or c in "-_")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe(ctx, rel: str) -> Path:
    root = workspace_root(ctx)
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ToolError("caminho fora do workspace do sprint")
    return p


# ------------------------------------------------------------------ memória & skills
@tool()
def memory_save(ctx, content: str, kind: str = "fact", scope: str = "agent", importance: int = 3, tags: list = None):
    """Salva uma memória (fact|episode|learning|decision). scope: agent (privada) ou shared (time)."""
    if scope not in ("agent", "shared"):
        raise ToolError("scope deve ser 'agent' ou 'shared'")
    if kind == "goal":
        raise ToolError('não salve a meta da sprint como memória: ela vale só para esta sprint. Responda com {"final": "<a meta em uma frase>"}')
    m = mem.save(ctx.agent, content, kind=kind, scope=scope, tags=tags or [], importance=importance, sprint_id=ctx.sprint_id)
    return {"memory_id": m.id}


@tool()
def memory_search(ctx, query: str, k: int = 5):
    """Busca memórias relevantes (suas e compartilhadas) por palavras-chave."""
    return [{"kind": m.kind, "scope": m.scope, "content": m.content} for m in mem.search(ctx.agent, query, k=k)]


BASELINE = ("sprint_status",)  # somente leitura: disponível a TODO agente, para ninguém “chutar” o andamento


@tool()
def sprint_status(ctx):
    """Estado REAL do trabalho: sprint atual, itens por etapa (com responsável), quem está trabalhando agora e onde ver os resultados."""
    from . import activity, scrum, stages
    from .models import ItemStage  # noqa: F401

    sp = db.session.query(Sprint).order_by(Sprint.id.desc()).first()
    items = db.session.query(WorkItem).filter((WorkItem.sprint_id == (sp.id if sp else -1)) | (WorkItem.status == "backlog")).order_by(WorkItem.rank).all()
    tls = stages.timelines(items)
    live = {k: v for k, v in activity.snapshot().items() if v.get("state") == "working"}
    return {
        "sprint": sp and {"id": sp.id, "name": sp.name, "status": sp.status, "goal": sp.goal, "capacity": sp.capacity, "velocity": sp.velocity,
                          "executando_agora": scrum.is_running(sp.id), "po_refinando": scrum.refining(sp.id)},
        "itens": [{"key": i.key, "titulo": i.title, "status": i.status, "pontos": i.points, "responsavel": i.assignee.slug if i.assignee else None,
                   "etapa_atual": tls[i.id]["current"], "retrabalhos": i.rework_count,
                   "entregaveis": _deliverables(i)} for i in items],
        "trabalhando_agora": {k: f"{v['what']} {v['item']} — {v['action']}".strip() for k, v in live.items()},
        "git": {k: v for k, v in gitflow.status().items() if k in ("head", "commits", "branches", "tags")},
        "deployments": [{"id": d.id, "app": d.app, "release": d.tag, "status": d.status, "url": d.url}
                        for d in db.session.query(Deployment).order_by(Deployment.id.desc()).limit(3)],
        "onde_ver_resultados": "Dashboard: card da história (linha do tempo + entregáveis), aba Git (commits/branches/tags) e aba Deploy (aplicação no ar). Arquivos em workspace/sprint-<n>/<US-xxx>/.",
    }


def _deliverables(item) -> list:
    if not item.sprint_id:
        return []
    base = Path(current_app.config["FLOW_WORKSPACE"]) / f"sprint-{item.sprint_id}" / item.key
    return sorted(str(f.relative_to(base.parent)) for f in base.rglob("*") if f.is_file())[:10] if base.is_dir() else []


@tool()
def list_skills(ctx):
    """Lista as skills disponíveis no skills_path deste agente."""
    return [{"name": s.name, "description": s.description} for s in skl.discover(ctx.agent).values()]


@tool()
def load_skill(ctx, name: str):
    """Carrega as instruções completas de uma skill."""
    try:
        return skl.load(ctx.agent, name)
    except KeyError as e:
        raise ToolError(str(e))


@tool()
def read_skill_file(ctx, name: str, path: str):
    """Lê um arquivo auxiliar dentro da pasta de uma skill."""
    try:
        return skl.read_skill_file(ctx.agent, name, path)
    except (KeyError, PermissionError, FileNotFoundError) as e:
        raise ToolError(str(e))


# ------------------------------------------------------------------ backlog / sprint (hierarquia Scrum)
@tool()
def backlog_list(ctx, status: str = ""):
    """Lista itens do backlog (opcionalmente filtrando por status)."""
    q = db.session.query(WorkItem).order_by(WorkItem.rank, WorkItem.id)
    if status:
        q = q.filter(WorkItem.status == status)
    return [_ser(i) for i in q.limit(50)]


@tool(roles=[Role.PO.value], mutating=True)
def backlog_create(ctx, title: str, description: str = "", acceptance_criteria: list = None, points: int = 3):
    """(PO) Cria uma user story no Product Backlog, com critérios de aceite."""
    try:  # modelo pequeno erra a escala: ajusta para o Fibonacci mais próximo em vez de recusar (recusar o fazia repetir até estourar os passos)
        points = min((1, 2, 3, 5, 8, 13), key=lambda f: abs(f - int(points)))
    except (TypeError, ValueError):
        points = 3
    title = re.sub(r"^\s*US-?\d+\s*[-–:]\s*", "", title).strip() or title  # a chave US-xxx é gerada pelo sistema
    criteria = [str(c).strip() for c in (acceptance_criteria or []) if str(c).strip()]
    trimmed = len(criteria) > 6
    criteria = criteria[:6]  # no máximo 6 critérios curtos e verificáveis (o gemma3:4b chegava a 10)
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()  # noqa: E731
    for other in db.session.query(WorkItem).filter(WorkItem.status != WorkItem.REJECTED):
        if SequenceMatcher(None, norm(title), norm(other.title)).ratio() >= 0.8:
            # não é erro: o PO apenas descobriu que a história já existe (antes, o erro fazia o modelo tentar de novo até estourar os passos)
            return {"created": False, "duplicate_of": other.key, "title": other.title,
                    "instrucao": f"{other.key} já cobre este pedido; não crie de novo. Crie só o que for realmente novo ou finalize com {{\"final\": \"...\"}}."}
    rank = (db.session.query(func.max(WorkItem.rank)).scalar() or 0) + 1
    i = WorkItem(title=title[:200], description=description, points=points, rank=rank,
                 acceptance_criteria=criteria, created_by=ctx.agent.slug)
    db.session.add(i)
    db.session.commit()
    stages.mark(i, "refine", "done", ctx.agent.slug, f"{points} pts · {len(i.acceptance_criteria)} critérios de aceite")
    gitflow.commit_story(i, ctx.agent.slug)
    return {**_ser(i), **({"aviso": "mantive só os 6 primeiros critérios; crie outra história para o resto"} if trimmed else {})}


@tool(roles=[Role.PO.value], mutating=True)
def backlog_prioritize(ctx, item_ids: list):
    """(PO) Reordena o backlog: a lista de ids vem do mais para o menos prioritário."""
    for r, ref in enumerate(item_ids, start=1):
        _item(ref).rank = r
    db.session.commit()
    return {"ordered": len(item_ids)}


@tool(roles=[Role.SM, Role.PO.value], mutating=True)
def sprint_add_item(ctx, item_id: int, assignee: str):
    """(SM) Move um item do backlog para o Sprint Backlog e define quem vai desenvolver."""
    if not ctx.sprint_id:
        raise ToolError("não há sprint em contexto")
    item = _item(item_id)
    if item.status != WorkItem.BACKLOG:
        raise ToolError(f"{item.key} não está no backlog (status={item.status})")
    dev = db.session.query(AgentConfig).filter_by(slug=assignee, scrum_role=Role.DEV.value, enabled=True).first()
    if not dev:
        raise ToolError(f"assignee '{assignee}' não é um developer ativo")
    if any(w in (dev.specialty or "").lower() for w in ("qa", "devops")):
        raise ToolError(f"{dev.slug} ({dev.specialty}) não implementa histórias; escolha um desenvolvedor (arquiteto, fullstack, ui-ux)")
    item.sprint_id, item.assignee, item.status = ctx.sprint_id, dev, WorkItem.SPRINT
    db.session.commit()
    stages.mark(item, "plan", "done", ctx.agent.slug, f"atribuído a {dev.name}")
    return _ser(item)


@tool(roles=[Role.SM.value], mutating=True)
def sprint_set_retro(ctx, text: str):
    """(SM) Registra a retrospectiva da sprint."""
    sp = db.session.get(Sprint, ctx.sprint_id) if ctx.sprint_id else None
    if not sp:
        raise ToolError("não há sprint em contexto")
    sp.retro = text
    db.session.commit()
    return {"ok": True}


@tool(roles=[Role.DEV.value, Role.SM.value], mutating=True)
def work_item_update(ctx, item_id: int, progress: int = -1, notes: str = ""):
    """Atualiza progresso (0-100) e notas de um item. Developers só mexem nos seus itens."""
    item = _item(item_id)
    if ctx.agent.scrum_role == Role.DEV.value and item.assignee_id != ctx.agent.id:
        raise ToolError(f"{item.key} não está atribuído a {ctx.agent.slug}")
    if progress >= 0:
        item.progress = max(0, min(100, progress))
    if notes:
        item.notes = notes
    db.session.commit()
    return _ser(item)


@tool(roles=[Role.DEV.value], mutating=True)
def work_item_verdict(ctx, item_id: int, passed: bool, notes: str = ""):
    """(QA) Registra o veredito de verificação. Quem desenvolveu o item não pode verificá-lo."""
    item = _item(item_id)
    if item.assignee_id == ctx.agent.id:
        raise ToolError("conflito de interesses: o autor do item não pode ser o verificador")
    if passed and sandbox.enabled() and not sandbox.passed_this_round(item):
        raise ToolError("não é possível aprovar: o pytest no sandbox não passou nesta rodada. Chame qa_run_tests e só aprove se ok=true")
    item.qa_verdict = {"passed": bool(passed), "notes": notes, "by": ctx.agent.slug}
    db.session.commit()
    return item.qa_verdict


@tool(roles=[Role.DEV.value], mutating=True)
def qa_run_tests(ctx, item_id: int):
    """(QA) Roda o pytest da história num container descartável (sem rede, com limites) e devolve o relatório: ok, resumo e saída. É a evidência objetiva do veredito."""
    if "qa" not in (ctx.agent.specialty or "").lower():
        raise ToolError("ferramenta restrita ao agente de QA")
    item = _item(item_id)
    if item.assignee_id == ctx.agent.id:
        raise ToolError("conflito de interesses: o autor do item não pode verificá-lo")
    r = sandbox.run(item, actor=ctx.agent.slug)
    return {k: r.get(k) for k in ("ok", "summary", "isolation", "exit_code", "seconds", "output")}


@tool(roles=[Role.PO.value], mutating=True)
def work_item_accept(ctx, item_id: int, accepted: bool, reason: str = ""):
    """(PO) Aceita ou rejeita o item na Sprint Review. Só aceita se o QA aprovou."""
    item = _item(item_id)
    if accepted and not (item.qa_verdict or {}).get("passed"):
        raise ToolError(f"{item.key} não tem aprovação do QA — definição de pronto não atendida")
    item.status = WorkItem.DONE if accepted else WorkItem.REJECTED
    if accepted:
        item.progress = 100
    item.notes = reason or item.notes
    db.session.commit()
    stages.mark(item, "accept", "done" if accepted else "failed", ctx.agent.slug, reason or ("aceito" if accepted else "rejeitado"))
    return _ser(item)


# ------------------------------------------------------------------ workspace (sandbox por sprint)
def _item_path(ctx, path: str) -> str:
    """Normaliza o caminho do entregável: tira `workspace/`/`sprint-N/` e, se só há UMA história em desenvolvimento,
    garante que o arquivo caia em <US-xxx>/ (o log real mostrou `workspace/entrega.md`, que o QA não encontrava)."""
    path = path.replace("\\", "/").lstrip("/")
    for pre in ("workspace/", f"{ctx.workflow_id}/"):
        if path.startswith(pre):
            path = path[len(pre):]
    if ctx.sprint_id:
        active = db.session.query(WorkItem).filter_by(sprint_id=ctx.sprint_id, status=WorkItem.PROGRESS).all()
        if len(active) == 1 and not path.startswith(active[0].key + "/"):
            path = f"{active[0].key}/{path}" if not re.match(r"US-\d+/", path) else path
    return path



@tool(mutating=True)
def write_workspace_file(ctx, path: str, content: str):
    """Escreve um entregável no workspace do sprint (caminho relativo, sandboxed)."""
    path = _item_path(ctx, path)
    qa = "qa" in (ctx.agent.specialty or "").lower()
    if qa and not re.fullmatch(r"US-\d+/tests/test_qa\.py", path):
        raise ToolError("o QA só escreve testes, em <US-xxx>/tests/test_qa.py (não altera o código do dev)")
    if not qa and path.endswith("tests/test_qa.py"):
        raise ToolError("tests/test_qa.py é do QA")
    p = _safe(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    out = {"path": path, "bytes": len(content.encode()), "o_que": activity._write_text({"path": path, "content": content})}
    if "erro de sintaxe" in out["o_que"]:
        out["aviso"] = "o arquivo Python tem erro de sintaxe — reescreva-o corrigido com write_workspace_file"
    return out


@tool()
def read_workspace_file(ctx, path: str):
    """Lê um arquivo do workspace do sprint."""
    p = _safe(ctx, path)
    if p.is_dir():  # o QA do log real pediu "US-001" (a pasta): devolve a listagem em vez de erro
        root = workspace_root(ctx)
        return {"é_uma_pasta": path, "arquivos": sorted(str(f.relative_to(root)) for f in p.rglob("*") if f.is_file())[:50],
                "dica": "chame read_workspace_file com o caminho de UM destes arquivos"}
    if not p.is_file():
        raise ToolError(f"arquivo não encontrado: {path}" + (f" (use list_workspace para ver o que existe)"))
    return p.read_text(encoding="utf-8")[:20000]


@tool()
def list_workspace(ctx, path: str = ""):
    """Lista arquivos do workspace do sprint."""
    root = workspace_root(ctx)
    base = _safe(ctx, path)
    return sorted(str(f.relative_to(root)) for f in base.rglob("*") if f.is_file())[:200]


# ------------------------------------------------------------------ git (leitura) e CI/CD local (agente devops)
@tool()
def git_log(ctx, branch: str = "", story: str = "", limit: int = 15):
    """Histórico git do projeto (1 commit por etapa). Filtre por branch (ex.: main, sprint/1) ou story (ex.: US-001)."""
    return [{"sha": c["short"], "autor": c["author"], "etapa": c["stage"], "historia": c["story"], "assunto": c["subject"], "refs": c["refs"]}
            for c in gitflow.log(branch=branch, story=story, limit=min(limit, 40))]


def _devops(ctx):
    if "devops" not in (ctx.agent.specialty or "").lower():
        raise ToolError("ferramenta restrita ao agente devops")
    if not cd.enabled():
        raise ToolError("CI/CD local desabilitado ([cd].enabled = false)")


def _dep(ref) -> Deployment:
    d = db.session.get(Deployment, int(ref)) if ref else db.session.query(Deployment).order_by(Deployment.id.desc()).first()
    if not d:
        raise ToolError(f"deployment {ref} não encontrado")
    return cd.refresh(d)


@tool(roles=[Role.DEV.value], mutating=True)
def ci_run(ctx, deployment_id: int):
    """(devops) Roda o CI da release (venv, dependências, testes do flow-app.toml) e devolve o relatório."""
    _devops(ctx)
    d = _dep(deployment_id)
    try:
        report = cd.run_ci(d)
    except cd.CDError as e:
        raise ToolError(str(e))
    return {"deployment_id": d.id, "app": d.app, "tag": d.tag, "status": d.status, "detail": d.detail,
            "passos": [{"nome": r["name"], "ok": r["ok"], "segundos": r["seconds"], "saida": r["output"][-300:] if not r["ok"] else ""} for r in report]}


@tool(roles=[Role.DEV.value], mutating=True)
def deploy_app(ctx, deployment_id: int):
    """(devops) Sobe a aplicação da release nesta máquina (exige CI ok + aprovação humana) e devolve a URL."""
    _devops(ctx)
    d = _dep(deployment_id)
    try:
        return {"deployment_id": d.id, "app": d.app, **cd.start(d, actor=ctx.agent.slug)}
    except cd.CDError as e:
        raise ToolError(str(e))


@tool(roles=[Role.DEV.value])
def deploy_status(ctx, deployment_id: int = 0):
    """(devops) Estado de uma release (ou da última): CI, aprovação, URL, PID."""
    _devops(ctx)
    return _dep(deployment_id).to_dict()


@tool(roles=[Role.DEV.value], mutating=True)
def deploy_stop(ctx, deployment_id: int):
    """(devops) Encerra a aplicação que este servidor subiu."""
    _devops(ctx)
    return {"resultado": cd.stop(_dep(deployment_id), actor=ctx.agent.slug)}


@tool(roles=[Role.DEV.value])
def deploy_logs(ctx, deployment_id: int, lines: int = 40):
    """(devops) Últimas linhas do log da aplicação."""
    _devops(ctx)
    return cd.tail(_dep(deployment_id), lines) or "(sem saída ainda)"


# ------------------------------------------------------------------ A2A / auditoria / domínio legado
@tool(mutating=True)
def delegate_to_agent(ctx, agent: str, message: str):
    """Envia uma mensagem A2A a outro agente (respeitando a hierarquia Scrum) e devolve a resposta."""
    from . import a2a

    return a2a.delegate(ctx, agent, message)


@tool()
def audit_query(ctx, limit: int = 10, actor: str = "", event_type: str = ""):
    """Consulta os últimos eventos de auditoria."""
    from .models import AuditEvent

    q = db.session.query(AuditEvent).order_by(AuditEvent.id.desc())
    if actor:
        q = q.filter(AuditEvent.actor == actor)
    if event_type:
        q = q.filter(AuditEvent.event_type == event_type)
    return [{"id": e.id, "when": e.created_at.isoformat(), "actor": e.actor, "type": e.event_type,
             "status": e.status, "target": e.target} for e in q.limit(min(limit, 50))]


def _rbac(ctx, intent):
    if not verificar_autoridade(ctx.user_email, intent):
        raise ToolError(f"Acesso Negado: {ctx.user_email} não tem permissão para '{intent}'")


@tool(mutating=True)
def criar_usuario(ctx, nome: str, perfil: str):
    """(domínio) Cria um usuário corporativo. Exige RBAC do solicitante humano."""
    _rbac(ctx, "criar_usuario")
    return _criar(nome, perfil)


@tool(mutating=True)
def conceder_permissao(ctx, nome: str, perfil: str):
    """(domínio) Concede um perfil a um usuário. Exige RBAC do solicitante humano."""
    _rbac(ctx, "conceder_permissao")
    return _conceder(nome, perfil)


@tool(mutating=True)
def revogar_permissao(ctx, nome: str, perfil: str):
    """(domínio) Revoga um perfil de um usuário. Exige RBAC do solicitante humano."""
    _rbac(ctx, "revogar_permissao")
    return _revogar(nome, perfil)


def describe(names) -> str:
    """Texto das tools para o prompt do LLM."""
    lines = []
    for n in names:
        t = REGISTRY.get(n)
        if not t:
            continue
        args = ", ".join(f"{p['name']}:{p['type']}{'' if p['required'] else '?'}" for p in t.params())
        lines.append(f"- {t.name}({args}) — {t.description}")
    return "\n".join(lines)
