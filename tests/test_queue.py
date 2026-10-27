"""Fila de jobs (procrastinate/PostgreSQL): o pipeline roda no worker, não no processo web."""
import pytest
from sqlalchemy import text

from flow import create_app, queue, scrum
from flow.extensions import db
from flow.models import AuditEvent, Deployment, Sprint, WorkItem

pytestmark = pytest.mark.filterwarnings("ignore")


def _jobs():
    rows = db.session.execute(text("SELECT id, task_name, status, attempts, args FROM procrastinate_jobs ORDER BY id")).all()
    db.session.commit()
    return rows


def _status(task):
    return [r.status for r in _jobs() if r.task_name == f"flow.{task}"]


def test_only_postgresql_is_accepted():
    for url in ("sqlite:///x.db", "sqlite://", "mysql://u:p@h/db"):
        with pytest.raises(RuntimeError, match="somente PostgreSQL"):
            create_app({"SQLALCHEMY_DATABASE_URI": url, "FLOW_EMBEDDED_WORKER": False})


def test_refine_is_queued_by_the_api_and_processed_by_the_worker(app, client):
    assert client.post("/api/refine", json={"request": "Tela de login"}).status_code == 202
    assert _status("refine") == ["todo"] and db.session.query(WorkItem).count() == 0  # a API só enfileira
    st = client.get("/api/state").get_json()
    assert st["sprint"]["refining"] is True and st["worker"]["alive"] is False and st["worker"]["pending"] == 1
    queue.drain()
    assert _status("refine") == ["succeeded"] and db.session.query(WorkItem).count() == 1
    assert client.get("/api/state").get_json()["sprint"]["refining"] is False


def test_full_sprint_through_the_queue_with_one_run_at_a_time(app, client):
    client.post("/api/refine", json={"request": "Site de boas-vindas #app"})
    queue.drain()
    sid = db.session.query(Sprint).one().id
    r = client.post(f"/api/sprints/{sid}/run")
    assert r.status_code == 202 and r.get_json()["started"] is True
    assert client.post(f"/api/sprints/{sid}/run").status_code == 409  # já está na fila/rodando
    assert client.get("/api/state").get_json()["sprint"]["running"] is True
    queue.drain()
    db.session.expire_all()
    sp = db.session.get(Sprint, sid)
    assert sp.status == "done" and sp.velocity > 0
    assert _status("run_sprint") == ["succeeded"]
    assert db.session.query(Deployment).one().status == "awaiting_approval"  # a release também saiu do worker
    assert client.get("/api/state").get_json()["sprint"]["running"] is False


def test_stop_cancels_a_queued_run(app, client):
    client.post("/api/refine", json={"request": "Tela de login"})
    queue.drain()
    sid = db.session.query(Sprint).one().id
    client.post(f"/api/sprints/{sid}/run")
    assert client.post(f"/api/sprints/{sid}/stop").get_json()["jobs"] == 1
    assert _status("run_sprint") == ["cancelled"] and not queue.is_running(sid)
    queue.drain()
    assert db.session.get(Sprint, sid).status == "planning"  # nada rodou


def test_pause_then_resume_continues_where_it_stopped(app):
    sp = Sprint(name="S", capacity=13)
    db.session.add(sp)
    db.session.commit()
    runner = scrum.SprintRunner(sp)
    runner.refine("Tela de login; Cadastro de usuário; Relatório mensal de vendas")
    calls = {"n": 0}

    def stop_after_first():  # o worker sinaliza o abort entre uma história e outra
        calls["n"] += 1
        return calls["n"] > 1

    assert scrum.SprintRunner(sp, should_stop=stop_after_first).run() is None
    db.session.refresh(sp)
    done = db.session.query(WorkItem).filter_by(status="done").count()
    assert sp.status == "active" and 1 <= done < 3
    stuck = db.session.query(WorkItem).filter_by(status="sprint").first()  # simula worker que morreu no meio de uma história
    stuck.status = "progress"
    db.session.commit()
    stats = scrum.SprintRunner(sp).run()
    assert stats["done"] == 3 and db.session.query(WorkItem).filter_by(status="done").count() == 3


def test_failed_job_is_retried_with_backoff_and_error_is_audited(app, client, monkeypatch):
    real, calls = scrum.SprintRunner.refine, {"n": 0}

    def flaky(self, request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Ollama fora do ar")
        return real(self, request)

    monkeypatch.setattr(scrum.SprintRunner, "refine", flaky)
    client.post("/api/refine", json={"request": "Tela de login"})
    queue.drain()
    row = _jobs()[0]
    assert row.status == "todo" and row.attempts == 1  # reagendado, não perdido
    assert db.session.query(AuditEvent).filter_by(event_type="refine_error").count() == 1
    db.session.execute(text("UPDATE procrastinate_jobs SET scheduled_at = now()"))  # passou o tempo de espera
    db.session.commit()
    queue.drain()
    assert _status("refine") == ["succeeded"] and db.session.query(WorkItem).count() == 1


def test_job_of_a_dead_worker_goes_back_to_the_queue(app):
    jid = queue.enqueue("refine", sprint_id=1, request="x")
    db.session.execute(text("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '10 minutes')"))
    db.session.execute(text("UPDATE procrastinate_jobs SET status='doing', worker_id=(SELECT max(id) FROM procrastinate_workers) WHERE id=:i"), {"i": jid})
    db.session.commit()
    assert _status("refine") == ["doing"]
    assert queue.recover_stalled() == 1
    assert _status("refine") == ["todo"]


def test_approve_and_stop_run_in_the_worker(app, client):
    from tests.test_git_cd import _free_port
    from flow import cd

    client.post("/api/refine", json={"request": "Site de boas-vindas #app"})
    queue.drain()
    client.post(f"/api/sprints/{db.session.query(Sprint).one().id}/run")
    queue.drain()
    dep = db.session.query(Deployment).one()
    port = _free_port()
    app.config["FLOW_CD_PORT_RANGE"] = (port, port)
    dep.manifest = {**dep.manifest, "run": {**dep.manifest["run"], "port": port}}
    db.session.commit()
    try:
        assert client.post(f"/api/deployments/{dep.id}/approve").status_code == 202
        assert _status("cd_deploy") == ["todo"] and db.session.get(Deployment, dep.id).status == "awaiting_approval"
        queue.drain()
        db.session.expire_all()
        assert db.session.get(Deployment, dep.id).status == "running"
        assert client.post(f"/api/deployments/{dep.id}/stop").status_code == 202
        queue.drain()
        db.session.expire_all()
        assert db.session.get(Deployment, dep.id).status == "stopped"
    finally:
        cd.stop(db.session.get(Deployment, dep.id))


def test_jobs_endpoint_and_worker_health(app, client):
    client.post("/api/refine", json={"request": "Tela de login"})
    j = client.get("/api/jobs").get_json()
    assert j["jobs"][0]["task"] == "refine" and j["jobs"][0]["status"] == "todo" and j["worker"] == {"alive": False, "workers": 0, "pending": 1}
    assert "worker" in client.get("/api/state").get_json()


def test_periodic_recover_task_runs_in_the_worker_without_flask_context(app):
    """Regressão: a task periódica falhava com 'Working outside of application context' (visto num servidor real)."""
    queue.producer().tasks["flow.recover_stalled"].defer(timestamp=1)
    queue.drain()
    st = _status("recover_stalled")
    assert st and set(st) == {"succeeded"}  # inclui a execução que o próprio worker agenda (cron)
