"""Fluxo Scrum completo: PO → SM → Devs → QA → PO, com retrabalho e auditoria íntegra."""
from flow import audit
from flow.extensions import db
from flow.models import AuditEvent, Sprint, WorkItem
from flow.scrum import SprintRunner


def _run(request, capacity=13):
    sp = Sprint(name="S1", capacity=capacity)
    db.session.add(sp)
    db.session.commit()
    runner = SprintRunner(sp)
    items, _ = runner.refine(request)
    return sp, items, runner


def test_full_sprint_with_rework(app):
    sp, items, runner = _run("Tela de login responsiva; Pipeline CI/CD; Cadastro com API #qa-fail-once")
    assert len(items) == 3
    stats = runner.run()
    assert stats["done"] == 3 and stats["rework"] == 1
    db.session.refresh(sp)
    assert sp.status == "done" and sp.velocity == stats["points"] and sp.retro
    assert all(i.status == "done" and i.qa_verdict["passed"] for i in sp.items)
    assert db.session.query(AuditEvent).filter_by(event_type="ceremony", target="rework").count() == 1
    assert audit.verify_chain()[0]


def test_capacity_leaves_overflow_in_backlog(app):
    sp, items, runner = _run("Uma história grande o bastante para consumir bastante capacidade desta sprint de teste; Outra igualmente grande para estourar a capacidade", capacity=5)
    runner.run()
    states = {i.status for i in db.session.query(WorkItem)}
    assert "backlog" in states and "done" in states  # o que não coube continua no Product Backlog


def test_hierarchy_is_enforced_and_audited(app):
    from flow.a2a import send_message
    from flow.tools import ToolError

    for sender, target in [("dev-fullstack", "scrum-master_x"), ("product-owner", "dev-fullstack"), ("qa", "scrum-master")]:
        try:
            send_message(sender, target.replace("_x", ""), "oi")
        except ToolError as e:
            assert "hierarquia" in str(e)
        else:
            raise AssertionError(f"{sender}->{target} deveria ser negado")
    denied = db.session.query(AuditEvent).filter_by(event_type="a2a_send", status="denied").count()
    assert denied == 3


def test_po_only_accepts_after_qa(app):
    from flow.models import AgentConfig
    from flow.tools import ToolContext, ToolError, call

    sp, items, runner = _run("Tela de login")
    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    ctx = ToolContext(agent=po, workflow_id=sp.workflow_id, sprint_id=sp.id)
    try:
        call("work_item_accept", ctx, {"item_id": items[0].id, "accepted": True})
    except ToolError as e:
        assert "QA" in str(e)
    else:
        raise AssertionError("PO aceitou sem aprovação do QA")


def test_qa_cannot_verify_own_work_and_devs_cannot_accept(app):
    from flow.models import AgentConfig
    from flow.tools import ToolContext, ToolError, call

    sp, items, runner = _run("Tela de login")
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    items[0].assignee = qa
    db.session.commit()
    ctx = ToolContext(agent=qa, workflow_id=sp.workflow_id, sprint_id=sp.id)
    for name, args in [("work_item_verdict", {"item_id": items[0].id, "passed": True}),
                       ("work_item_accept", {"item_id": items[0].id, "accepted": True}),
                       ("backlog_create", {"title": "x"})]:
        try:
            call(name, ctx, args)
        except ToolError:
            pass
        else:
            raise AssertionError(f"{name} deveria ser negado ao QA")


def test_audit_chain_survives_db_roundtrip(app):
    """Regressão: no PostgreSQL o timestamp relido do banco precisava ser idêntico ao usado no hash."""
    from flow.models import AuditEvent

    audit.record(workflow_id="w", actor="a", event_type="t", payload={"k": "ç✓"})
    db.session.expire_all()
    db.session.remove()
    assert audit.verify_chain()[0]
    assert db.session.query(AuditEvent).count() == 1


def test_item_timeline_follows_every_stage_and_rework(app):
    from flow import stages

    sp, items, runner = _run("Cadastro com API #qa-fail-once")
    it = items[0]
    tl = stages.timelines([it])[it.id]
    assert [s["state"] for s in tl["steps"]] == ["done"] + ["pending"] * 5 and tl["current"] == "plan"
    runner.plan()
    db.session.refresh(it)
    tl = stages.timelines([it])[it.id]
    assert [s["state"] for s in tl["steps"][:3]] == ["done", "done", "pending"] and "atribuído" in tl["steps"][1]["detail"]
    runner.run()
    db.session.refresh(it)
    tl = stages.timelines([it])[it.id]
    assert [s["state"] for s in tl["steps"]] == ["done"] * 6 and tl["rework"] == 1
    assert all(s["at"] for s in tl["steps"]) and tl["steps"][4]["actor"] == "product-owner" and tl["steps"][3]["actor"] == "qa"
    # o retrabalho ficou registrado no histórico (reprovou 1x, depois aprovou)
    from flow.models import ItemStage
    verify = [r.state for r in db.session.query(ItemStage).filter_by(item_id=it.id, stage="verify").order_by(ItemStage.id)]
    assert verify == ["active", "failed", "active", "done"]


def test_timeline_mid_flow_states_and_api(app, client):
    from flow import stages
    from flow.models import AgentConfig

    sp, items, runner = _run("Tela de login")
    it = items[0]
    runner.plan()
    db.session.refresh(it)
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    stages.mark(it, "implement", "done", it.assignee.slug, "ok")
    stages.mark(it, "verify", "active", qa.slug)
    steps = client.get(f"/api/items/{it.id}/timeline").get_json()["steps"]
    assert [s["state"] for s in steps] == ["done", "done", "done", "active", "pending", "pending"]
    sprint_tl = client.get(f"/api/sprints/{sp.id}/timeline").get_json()
    assert sprint_tl["steps"][0]["state"] == "done" and sprint_tl["steps"][1]["state"] == "done" and sprint_tl["steps"][2]["state"] == "active"
    state = client.get("/api/state").get_json()
    assert state["items"][0]["timeline"]["current"] == "verify" and state["sprint_timeline"]["current"] == "execute"


def test_timeline_dropped_when_item_returns_to_backlog(app):
    from flow import stages

    sp, items, runner = _run("Uma história grande o bastante para consumir bastante capacidade desta sprint de teste; Outra igualmente grande para estourar a capacidade", capacity=5)
    runner.run()
    leftover = db.session.query(WorkItem).filter_by(status="backlog").first()
    tl = stages.timelines([leftover])[leftover.id]
    assert [s["state"] for s in tl["steps"]] == ["done"] + ["pending"] * 5  # histórico da sprint não vaza


def test_live_activity_tracks_who_does_what(app, client, monkeypatch):
    """Durante a execução o painel mostra agente, cerimônia, item e a ação atual; depois volta a ocioso."""
    from flow import activity, llm

    seen = []
    real = llm.PROVIDERS["mock"]

    def spy(messages, cfg):
        seen.append({slug: a for slug, a in activity.snapshot().items() if a["state"] == "working"})
        return real(messages, cfg)

    monkeypatch.setitem(llm.PROVIDERS, "mock", spy)
    sp, items, runner = _run("Tela de login")
    runner.run()
    working = [w for snap in seen for w in snap.values()]
    assert {"refinando o pedido", "planejando a sprint", "desenvolvendo", "verificando (QA)"} <= {w["what"] for w in working}
    dev = next(w for w in working if w["what"] == "desenvolvendo")
    assert dev["item"].startswith("US-") and "pensando" in dev["action"]
    assert all(a["state"] == "idle" for a in activity.snapshot().values())  # tudo ocioso no fim
    data = client.get("/api/activity").get_json()
    assert any("escreveu" in f["text"] or "escrevendo" in f["text"] for f in data["feed"])
    assert all("activity" in a for a in client.get("/api/state").get_json()["agents"])


def test_stale_working_state_is_shown_idle(app):
    from datetime import timedelta

    from flow import activity
    from flow.models import AgentActivity, AgentConfig, now

    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    activity.start(po, {"ceremony": "refine"})
    assert activity.snapshot()["product-owner"]["state"] == "working"
    row = db.session.get(AgentActivity, po.id)
    row.updated_at = now() - timedelta(seconds=po.timeout_s + 120)
    db.session.commit()
    assert activity.snapshot()["product-owner"]["state"] == "idle"  # servidor caiu no meio: não fica “trabalhando” para sempre


def test_plan_without_adding_items_fails_instead_of_closing_empty_sprint(app, monkeypatch):
    """Regressão (gemma3:4b real): SM respondeu 'Sprint planejado' sem chamar sprint_add_item → sprint encerrada com 0 itens."""
    import pytest

    from flow import llm

    real = llm.PROVIDERS["mock"]

    def sm_only_talks(messages, cfg):
        if "[[role:scrum_master" in messages[0]["content"]:
            return '{"final": "Sprint planejado. Vamos começar!"}', {"in": 1, "out": 1}
        return real(messages, cfg)

    monkeypatch.setitem(llm.PROVIDERS, "mock", sm_only_talks)
    sp, items, runner = _run("Tela de login")
    with pytest.raises(RuntimeError, match="não montou o Sprint Backlog"):
        runner.run()
    db.session.refresh(sp)
    assert sp.status == "planning" and db.session.query(WorkItem).filter_by(status="backlog").count() == 1  # nada foi "concluído"


def test_nothing_fits_capacity_has_clear_error(app):
    import pytest

    sp, items, runner = _run("Uma história grande o bastante para consumir bastante capacidade desta sprint de teste", capacity=1)
    with pytest.raises(RuntimeError, match="cabe na capacidade"):
        runner.run()


def test_sprint_status_available_to_every_agent_and_reports_truth(app):
    from flow.models import AgentConfig
    from flow.tools import ToolContext, call

    sp, items, runner = _run("Tela de login")
    runner.run()
    for slug in ("product-owner", "qa", "dev-fullstack"):  # mesmo sem estar na allowlist configurada
        agent = db.session.query(AgentConfig).filter_by(slug=slug).first()
        assert "sprint_status" not in agent.tools
        st = call("sprint_status", ToolContext(agent=agent, workflow_id="t"), {})
        assert st["sprint"]["status"] == "done" and st["itens"][0]["status"] == "done"
        assert st["itens"][0]["entregaveis"] and st["itens"][0]["entregaveis"][0].endswith("entrega.md")
        assert "onde_ver_resultados" in st


def test_deliverables_api_serves_files_and_blocks_traversal(app, client):
    sp, items, runner = _run("Tela de login")
    runner.run()
    iid = items[0].id
    files = client.get(f"/api/items/{iid}/files").get_json()["files"]
    assert files and files[0]["path"].endswith("entrega.md")
    body = client.get(f"/api/items/{iid}/files/{files[0]['path']}").get_json()["content"]
    assert "Critérios de aceite atendidos" in body
    for bad in ("../../../../etc/passwd", "..%2f..%2fflow.db", "nao-existe.md"):
        assert client.get(f"/api/items/{iid}/files/{bad}").status_code == 404


def test_every_agent_prompt_offers_sprint_status_and_forbids_guessing(app):
    from flow import context as cx
    from flow.models import AgentConfig

    for a in db.session.query(AgentConfig):
        system, _, _ = cx.build(a, query="em qual etapa parou?", external_tools={}, context_id="c", task_id=None)
        assert "sprint_status(" in system and "NUNCA invente andamento" in system, a.slug
