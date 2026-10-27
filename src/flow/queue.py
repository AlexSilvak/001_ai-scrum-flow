"""Fila de jobs do pipeline — **procrastinate** (biblioteca Python de filas sobre o PostgreSQL: equivalente ao pg-boss).

    web (Flask) ──defer──▶ tabela procrastinate_jobs ◀──LISTEN/NOTIFY── worker(s) ──▶ SprintRunner / CI / deploy

Por que fila no banco (e não threads no processo web): o job sobrevive a reinício do servidor, tem retry com espera, um job
`doing` de worker morto é recolocado na fila, e a trava `lock` garante UMA sprint por vez (o workspace é um único repositório git).

Jobs:  flow.refine · flow.run_sprint (lock "workspace", queueing_lock por sprint, abortável) · flow.cd_ci / cd_deploy / cd_stop.
Worker: embutido no `flask run` (thread, [worker] embedded = true) ou à parte: `uv run flask --app flow worker`.
"""
import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, current_app
from procrastinate import App, PsycopgConnector, RetryStrategy, exceptions
from sqlalchemy import text

from .extensions import db

if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    # o psycopg assíncrono não funciona com o ProactorEventLoop (padrão do Windows)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

LOCK_WORKSPACE = "workspace"
ACTIVE = "('todo','doing','aborting')"
_APP: Flask | None = None
_PRODUCERS: dict[str, App] = {}
_PRODUCER_LOCK = threading.Lock()
_EMBEDDED = {"started": False}


# ------------------------------------------------------------------ app Flask que as tasks usam
def bind(app: Flask):
    """Registra o app Flask usado pelas tasks (o worker roda tasks dentro de `app_context`)."""
    global _APP
    _APP = app


def flask_app() -> Flask:
    global _APP
    if _APP is None:
        from . import create_app

        _APP = create_app()
    return _APP


def dsn(app: Flask | None = None) -> str:
    uri = str((app or current_app).config["SQLALCHEMY_DATABASE_URI"])
    return uri.replace("postgresql+psycopg://", "postgresql://", 1)


# ------------------------------------------------------------------ tasks
def make_app(conninfo: str) -> App:
    """Cria uma instância do procrastinate com as tasks. O produtor (web) e cada worker usam instâncias SEPARADAS."""
    q = App(connector=PsycopgConnector(conninfo=conninfo))

    @q.task(name="flow.refine", retry=RetryStrategy(max_attempts=2, wait=15))
    def refine(sprint_id: int, request: str, user: str = "human"):
        from . import audit, scrum
        from .models import Sprint

        with flask_app().app_context():
            try:
                scrum.SprintRunner(db.session.get(Sprint, sprint_id), user).refine(request)
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                audit.record(workflow_id=f"sprint-{sprint_id}", actor="system", event_type="refine_error", status="error", payload={"error": repr(e)})
                raise
            finally:
                db.session.remove()

    @q.task(name="flow.run_sprint", pass_context=True, retry=RetryStrategy(max_attempts=2, wait=30))
    def run_sprint(context, sprint_id: int, user: str = "human"):
        from . import audit, scrum
        from .models import Sprint

        with flask_app().app_context():
            try:
                scrum.SprintRunner(db.session.get(Sprint, sprint_id), user, should_stop=context.should_abort).run()
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                audit.record(workflow_id=f"sprint-{sprint_id}", actor="system", event_type="sprint_error", status="error", payload={"error": repr(e)})
                raise
            finally:
                db.session.remove()

    @q.task(name="flow.cd_ci")
    def cd_ci(deployment_id: int, who: str = "human"):
        _cd(deployment_id, "ci", who)

    @q.task(name="flow.cd_deploy")
    def cd_deploy(deployment_id: int, who: str = "human"):
        _cd(deployment_id, "deploy", who)

    @q.task(name="flow.cd_stop")
    def cd_stop(deployment_id: int, who: str = "human"):
        from . import cd
        from .models import Deployment

        with flask_app().app_context():
            try:
                cd.stop(db.session.get(Deployment, deployment_id), who)
            finally:
                db.session.remove()

    @q.periodic(cron="* * * * *")
    @q.task(name="flow.recover_stalled")
    def recover(timestamp: int):
        recover_stalled()

    return q


def _cd(deployment_id: int, kind: str, who: str):
    from . import audit, cd

    with flask_app().app_context():
        try:
            cd.run_ceremony(deployment_id, kind, who)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            audit.record(workflow_id="cd", actor="system", event_type="cd_error", status="error", payload={"error": repr(e)})
            raise
        finally:
            db.session.remove()


# ------------------------------------------------------------------ esquema
def ensure_schema():
    """Aplica o esquema do procrastinate (uma vez; é idempotente por checagem de tabela)."""
    exists = db.session.execute(text("SELECT to_regclass('procrastinate_jobs')")).scalar()
    db.session.commit()
    if exists:
        return
    with make_app(dsn(flask_app())).open() as a:
        a.schema_manager.apply_schema()


# ------------------------------------------------------------------ produtor (web/CLI)
def producer() -> App:
    key = dsn(flask_app())
    with _PRODUCER_LOCK:
        if key not in _PRODUCERS:
            a = make_app(key)
            a.open()
            _PRODUCERS[key] = a
        return _PRODUCERS[key]


def close_producers():
    with _PRODUCER_LOCK:
        for a in _PRODUCERS.values():
            try:
                a.close()
            except Exception:  # noqa: BLE001
                pass
        _PRODUCERS.clear()


def enqueue(task: str, *, lock: str | None = None, queueing_lock: str | None = None, **kwargs) -> int | None:
    """Põe um job na fila. Devolve o id, ou None se já há um igual esperando (`queueing_lock`)."""
    opts = {k: v for k, v in (("lock", lock), ("queueing_lock", queueing_lock)) if v}
    try:
        return producer().tasks[f"flow.{task}"].configure(**opts).defer(**kwargs)
    except exceptions.AlreadyEnqueued:
        return None


def enqueue_run_sprint(sprint_id: int, user: str = "human") -> int | None:
    return enqueue("run_sprint", lock=LOCK_WORKSPACE, queueing_lock=f"sprint-{sprint_id}", sprint_id=sprint_id, user=user)


# ------------------------------------------------------------------ estado (lido direto da tabela do procrastinate)
def _rows(task: str, sprint_id: int | None = None) -> list:
    q = f"SELECT id, status FROM procrastinate_jobs WHERE task_name = :t AND status IN {ACTIVE}"
    params = {"t": f"flow.{task}"}
    if sprint_id is not None:
        q += " AND (args->>'sprint_id')::int = :s"
        params["s"] = sprint_id
    rows = db.session.execute(text(q), params).all()
    db.session.commit()  # não deixa transação aberta na sessão do web
    return rows


def is_running(sprint_id: int) -> bool:
    return bool(_rows("run_sprint", sprint_id))


def refining(sprint_id: int) -> bool:
    return bool(_rows("refine", sprint_id))


def busy() -> bool:
    """Alguma sprint na fila ou rodando? (o workspace/git é único: uma por vez)"""
    return bool(_rows("run_sprint"))


def stop_sprint(sprint_id: int) -> int:
    """Pausa a sprint: job ainda na fila é cancelado; job rodando recebe `abort` (o runner para entre as histórias)."""
    n = 0
    for jid, status in _rows("run_sprint", sprint_id):
        n += bool(producer().job_manager.cancel_job_by_id(jid, abort=(status != "todo")))
    return n


def worker_status() -> dict:
    alive = db.session.execute(text("SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '30 seconds'")).scalar()
    pending = db.session.execute(text(f"SELECT count(*) FROM procrastinate_jobs WHERE status IN {ACTIVE}")).scalar()
    db.session.commit()
    return {"alive": bool(alive), "workers": int(alive), "pending": int(pending)}


def recent_jobs(limit: int = 15) -> list[dict]:
    rows = db.session.execute(text("SELECT id, task_name, status, attempts, args, scheduled_at FROM procrastinate_jobs WHERE task_name <> 'flow.recover_stalled' OR status = 'failed' ORDER BY id DESC LIMIT :n"), {"n": limit}).all()
    db.session.commit()
    return [{"id": r.id, "task": r.task_name.removeprefix("flow."), "status": r.status, "attempts": r.attempts, "args": r.args,
             "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None} for r in rows]


# ------------------------------------------------------------------ worker
def recover_stalled(seconds_since_heartbeat: float = 60) -> int:
    """Job `doing` cujo worker morreu (sem heartbeat) volta para a fila. Chamado ao iniciar o worker e a cada minuto."""
    with flask_app().app_context():
        ids = [r.id for r in db.session.execute(text(
            "SELECT j.id FROM procrastinate_jobs j LEFT JOIN procrastinate_workers w ON w.id = j.worker_id "
            "WHERE j.status = 'doing' AND (w.id IS NULL OR w.last_heartbeat < now() - make_interval(secs => :s))"), {"s": seconds_since_heartbeat})]
        db.session.commit()
        db.session.remove()
    p = producer()
    for jid in ids:
        p.job_manager.retry_job_by_id(jid, retry_at=datetime.now(timezone.utc))
    return len(ids)


def run_worker(concurrency: int | None = None, wait: bool = True):
    """Bloqueia processando jobs (LISTEN/NOTIFY). Em thread secundária não instala handlers de sinal."""
    app = flask_app()
    try:
        recover_stalled()
    except Exception:  # noqa: BLE001 — recuperação nunca impede o worker de subir
        pass
    make_app(dsn(app)).run_worker(wait=wait, concurrency=concurrency or app.config["FLOW_WORKER_CONCURRENCY"], listen_notify=True,
                                  install_signal_handlers=threading.current_thread() is threading.main_thread())


def start_embedded_on_first_request(app: Flask):
    """Dev local: o 1º request do servidor web sobe um worker em thread (comandos de CLI nunca sobem)."""
    if not app.config.get("FLOW_EMBEDDED_WORKER"):
        return

    @app.before_request
    def _start():
        if _EMBEDDED["started"]:
            return
        _EMBEDDED["started"] = True
        threading.Thread(target=run_worker, daemon=True, name="flow-worker").start()


def drain(max_rounds: int = 30) -> None:
    """Testes: processa jobs até a fila esvaziar (jobs com `lock` bloqueado exigem mais de uma rodada)."""
    for _ in range(max_rounds):
        make_app(dsn(flask_app())).run_worker(wait=False, concurrency=4, listen_notify=False, install_signal_handlers=False)
        n = db.session.execute(text("SELECT count(*) FROM procrastinate_jobs WHERE status IN ('todo','doing','aborting') AND (scheduled_at IS NULL OR scheduled_at <= now())")).scalar()
        db.session.commit()
        if not n:
            return
