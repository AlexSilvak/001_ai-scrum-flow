"""Trace de execução dos agentes: cada run tem linhas (tarefa, pensando, ferramenta, fase, teste, final) gravadas no PostgreSQL.
O painel “Detalhe da tarefa” lê daqui (de outro processo, se for o caso) — por isso nada fica só em memória."""
import time
import uuid
from contextvars import ContextVar
from pathlib import PurePosixPath

from sqlalchemy import func

from .extensions import db
from .models import AgentTrace, now

CAP = 20000          # limite por campo de texto (o conteúdo de um arquivo grande é cortado)
KEEP_RUNS = 25       # execuções guardadas por agente
CURRENT: ContextVar[str | None] = ContextVar("flow_trace_run", default=None)
_STREAM = {}         # id da linha -> (último flush, tamanho) para limitar a escrita no banco enquanto o modelo gera


def _cut(x, n=CAP) -> str:
    x = "" if x is None else str(x)
    return x if len(x) <= n else x[:n] + f"\n…[+{len(x) - n} caracteres]"


def begin(slug: str, ceremony: str = "", item: str = "", title: str = "", body: str = "") -> str:
    run = uuid.uuid4().hex
    CURRENT.set(run)
    add(run, slug, "task", title or ceremony or "tarefa", body=body, ceremony=ceremony, item=item)
    old = [r for (r,) in db.session.query(AgentTrace.run_id).filter_by(agent_slug=slug).group_by(AgentTrace.run_id)
           .order_by(func.max(AgentTrace.id).desc()).offset(KEEP_RUNS)]
    if old:
        db.session.query(AgentTrace).filter(AgentTrace.run_id.in_(old)).delete(synchronize_session=False)
        db.session.commit()
    return run


def latest_run(slug: str) -> str | None:
    row = db.session.query(AgentTrace.run_id).filter_by(agent_slug=slug).order_by(AgentTrace.id.desc()).first()
    return row[0] if row else None


def add(run: str | None, slug: str, kind: str, title: str, *, body: str = "", result: str = "", meta: dict | None = None,
        state: str = "ok", ms: int | None = None, ceremony: str = "", item: str = "") -> int | None:
    if not run:
        return None
    if not ceremony or not item:  # herda a cerimônia/história da linha de abertura do run
        head = db.session.query(AgentTrace.ceremony, AgentTrace.item_key).filter_by(run_id=run).order_by(AgentTrace.id).first()
        ceremony, item = ceremony or (head[0] if head else ""), item or (head[1] if head else "")
    row = AgentTrace(run_id=run, agent_slug=slug, ceremony=ceremony, item_key=item, kind=kind, state=state, title=_cut(title, 400),
                     body=_cut(body), result=_cut(result), meta=meta or {}, ms=ms)
    db.session.add(row)
    db.session.commit()
    return row.id


def update(row_id: int | None, **f):
    if not row_id:
        return
    row = db.session.get(AgentTrace, row_id)
    if not row:
        return
    for k, v in f.items():
        if k in ("body", "result"):
            v = _cut(v)
        elif k == "title":
            v = _cut(v, 400)
        elif k == "meta":
            v = {**(row.meta or {}), **v}
        setattr(row, k, v)
    row.updated_at = now()
    db.session.commit()
    _STREAM.pop(row_id, None)


def stream(row_id: int | None, text: str, every: float = 0.6):
    """Texto parcial do modelo enquanto ele gera (Ollama em streaming). Grava no máximo a cada `every` segundos."""
    if not row_id:
        return
    last = _STREAM.get(row_id, 0.0)
    if time.time() - last < every:
        return
    _STREAM[row_id] = time.time()
    row = db.session.get(AgentTrace, row_id)
    if row:
        row.body, row.updated_at = _cut(text), now()
        row.meta = {**(row.meta or {}), "chars": len(text)}
        db.session.commit()


def end(run: str | None, slug: str, ok: bool, text: str):
    add(run, slug, "final", "concluído" if ok else "terminou com falha", body=text, state="ok" if ok else "error")


# ---------------------------------------------------------------- metadados de arquivo
def file_meta(name: str, args: dict, content: str | None = None) -> dict:
    """path/nome/extensão/tamanho + ação (escreveu, leu, listou, executou testes) para destacar o arquivo no painel."""
    path = str(args.get("path", "") or "")
    m = {}
    if path:
        p = PurePosixPath(path.replace("\\", "/"))
        m.update(path=path, name=p.name, ext=p.suffix.lstrip(".").lower() or "pasta")
    m["action"] = {"write_workspace_file": "escreveu", "read_workspace_file": "leu", "list_workspace": "listou", "qa_run_tests": "executou os testes",
                   "work_item_verdict": "veredito", "git_log": "consultou o git"}.get(name, "")
    if name == "write_workspace_file":
        m["bytes"] = len(str(args.get("content", "")).encode())
        m["lines"] = str(args.get("content", "")).count("\n") + 1
    return m
