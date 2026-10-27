"""Git por etapa, CI/CD local, recuperação de tool anunciada, anti-loop e dedupe."""
import json
import socket
import urllib.request

import pytest

from flow import cd, gitflow, llm, runtime, scrum
from flow.extensions import db
from flow.models import AgentConfig, AuditEvent, Deployment, Sprint, WorkItem
from flow.tools import ToolContext, ToolError, call

pytestmark = pytest.mark.filterwarnings("ignore")


def _run(request, capacity=13):
    sp = Sprint(name="Sprint 1", capacity=capacity)
    db.session.add(sp)
    db.session.commit()
    runner = scrum.SprintRunner(sp)
    items, _ = runner.refine(request)
    return sp, items, runner


def _git(*a):
    return gitflow._run(list(a))


def _agent(slug):
    return db.session.query(AgentConfig).filter_by(slug=slug).first()


# ----------------------------------------------------------------------------- git
def test_every_stage_is_committed_on_the_right_branch_with_agent_authorship(app):
    sp, items, runner = _run("Tela de login #qa-fail-once")
    runner.run()
    commits = gitflow.log(limit=100)
    stages = [c["stage"] for c in commits]
    for st in ("init", "refine", "plan", "implement", "verify", "accept", "merge", "review", "release"):
        assert st in stages, f"faltou commit da etapa {st}"
    assert stages.count("implement") == 2 and stages.count("verify") == 2  # retrabalho = commits extras, nada é sobrescrito
    by_stage = {c["stage"]: c for c in commits}
    assert by_stage["refine"]["author"] == "Product Owner" and by_stage["plan"]["author"] == "Scrum Master"
    assert by_stage["verify"]["author"] == "QA Guard" and by_stage["implement"]["agent"] != "qa"
    names = {b["name"] for b in gitflow.branches()}
    assert {"main", "backlog", "sprint/1", "feature/US-001"} <= names
    assert {t["name"] for t in gitflow.tags()} == {"story/US-001", "sprint-1"}
    # main só tem o que o PO aceitou e traz o entregável + PLANNING/REVIEW
    assert "sprint-1/US-001/entrega.md" in _git("ls-tree", "-r", "--name-only", "main")
    assert "sprint-1/REVIEW.md" in _git("ls-tree", "-r", "--name-only", "main")
    assert gitflow.log(story="US-001", limit=50) and all(c["story"] == "US-001" for c in gitflow.log(story="US-001"))
    assert _git("status", "--porcelain") == ""  # árvore limpa ao final


def test_git_commits_are_audited_and_chain_stays_valid(app):
    from flow import audit

    _, _, runner = _run("Tela de login")
    runner.run()
    ev = db.session.query(AuditEvent).filter_by(event_type="git_commit").all()
    assert len(ev) >= 8 and all(e.payload["sha"] for e in ev)
    assert db.session.query(AuditEvent).filter_by(event_type="git_error").count() == 0
    assert audit.verify_chain()[0]


def test_rejected_story_is_not_merged_but_kept_as_evidence(app, client):
    sp, items, runner = _run("Tela de login #qa-fail-always")
    runner.run()
    item = db.session.get(WorkItem, items[0].id)
    assert item.status == "backlog"  # devolvida ao backlog no fim da sprint
    assert "sprint-1/US-001/entrega.md" not in _git("ls-tree", "-r", "--name-only", "main")
    br = {b["name"]: b for b in gitflow.branches()}
    assert br["feature/US-001"]["merged"] is False
    assert "story/US-001" not in {t["name"] for t in gitflow.tags()}
    assert any(c["stage"] == "reject" for c in gitflow.log(story="US-001"))
    # o entregável continua visível pelo dashboard (lido da branch não mergeada)
    r = client.get(f"/api/items/{item.id}/files").get_json()
    assert r["source"].startswith("git:feature/") and r["files"]
    body = client.get(f"/api/items/{item.id}/files/{r['files'][0]['path']}").get_json()["content"]
    assert "US-001" in body


def test_git_failure_never_breaks_the_sprint(app, monkeypatch):
    def boom(*a, **k):
        raise gitflow.GitError("disco cheio")

    monkeypatch.setattr(gitflow, "_run", boom)
    _, _, runner = _run("Tela de login")
    stats = runner.run()
    assert stats["done"] == 1
    assert db.session.query(AuditEvent).filter_by(event_type="git_error").count() >= 1


def test_git_can_be_disabled(app):
    app.config["FLOW_GIT_ENABLED"] = False
    _, _, runner = _run("Tela de login")
    assert runner.run()["done"] == 1
    assert not (gitflow.root() / ".git").exists()
    assert gitflow.log() == [] and gitflow.status()["enabled"] is False


def test_git_api_endpoints(app, client):
    _, _, runner = _run("Tela de login")
    runner.run()
    assert client.get("/api/git/status").get_json()["initialized"]
    log = client.get("/api/git/log?branch=main").get_json()["commits"]
    assert log[0]["stage"] == "release"
    sha = log[0]["sha"]
    c = client.get(f"/api/git/commits/{sha}").get_json()
    assert c["subject"].startswith("release(") and "diff" in c
    assert client.get("/api/git/commits/not-a-sha;rm").status_code == 404  # sha validado (sem injeção)
    b = client.get("/api/git/branches").get_json()
    assert any(x["name"] == "sprint/1" and x["merged"] for x in b["branches"]) and any(t["name"] == "sprint-1" for t in b["tags"])
    assert client.get("/api/items/1/git").get_json()["commits"]


def test_one_sprint_at_a_time_because_workspace_is_a_single_repo(app, client):
    from flow import queue

    sp, _, _ = _run("Tela de login")
    other = Sprint(name="S2")
    db.session.add(other)
    db.session.commit()
    assert queue.enqueue_run_sprint(sp.id)  # sprint 1 na fila/rodando
    r = client.post(f"/api/sprints/{other.id}/run")
    assert r.status_code == 409 and "outra sprint" in r.get_json()["error"]


# ----------------------------------------------------------------------------- CI/CD
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_release_runs_ci_by_devops_and_waits_for_human_approval(app):
    sp, _, runner = _run("Site de boas-vindas #app")
    assert runner.run()["done"] == 1
    dep = db.session.query(Deployment).one()
    assert dep.status == "awaiting_approval" and dep.tag == "sprint-1" and dep.app == "demo-app"
    assert dep.ci_report and all(r["ok"] for r in dep.ci_report)
    assert db.session.query(AuditEvent).filter_by(actor="devops", event_type="ci_result").count() == 1
    ctx = ToolContext(agent=cd.devops_agent(), workflow_id="sprint-1")
    with pytest.raises(ToolError, match="aprovação humana"):
        call("deploy_app", ctx, {"deployment_id": dep.id})
    assert dep.status == "awaiting_approval" and dep.pid is None  # nada subiu


def test_approved_release_is_started_by_agent_serves_and_stops(app):
    _, _, runner = _run("Site de boas-vindas #app")
    runner.run()
    dep = db.session.query(Deployment).one()
    m = dict(dep.manifest)
    port = _free_port()
    app.config["FLOW_CD_PORT_RANGE"] = (port, port)
    m["run"] = {**m["run"], "port": port}
    dep.manifest = m
    db.session.commit()
    cd.approve(dep, "alex")
    ctx = ToolContext(agent=cd.devops_agent(), workflow_id="sprint-1")
    try:
        out = call("deploy_app", ctx, {"deployment_id": dep.id})
        assert out["status"] == "running" and out["url"] == f"http://127.0.0.1:{port}"
        assert b"no ar pelo CI/CD local" in urllib.request.urlopen(out["url"], timeout=5).read()
        assert call("deploy_status", ctx, {"deployment_id": dep.id})["status"] == "running"
        assert "resultado" in call("deploy_stop", ctx, {"deployment_id": dep.id})
        assert db.session.get(Deployment, dep.id).status == "stopped"
    finally:
        cd.stop(dep)
    ev = [e.event_type for e in db.session.query(AuditEvent).filter(AuditEvent.event_type.like("deploy_%"))]
    assert {"deploy_approved", "deploy_started", "deploy_stopped"} <= set(ev)


def test_deploy_runs_the_tagged_copy_not_the_working_tree(app):
    _, _, runner = _run("Site de boas-vindas #app")
    runner.run()
    dep = db.session.query(Deployment).one()
    assert ".deploy" in dep.workdir and (cd.Path(dep.workdir) / "app.py").is_file()
    assert (gitflow.root() / "sprint-1" / "US-001" / "app.py").is_file()  # original intocado


def test_release_without_manifest_is_skipped_not_failed(app):
    _, _, runner = _run("Tela de login")
    runner.run()
    dep = db.session.query(Deployment).one()
    assert dep.status == "skipped" and "flow-app.toml" in dep.detail


def test_ci_tools_are_restricted_to_devops(app):
    ctx = ToolContext(agent=_agent("dev-fullstack"), workflow_id="sprint-1")
    for name in ("ci_run", "deploy_app", "deploy_stop"):
        with pytest.raises(ToolError):
            call(name, ctx, {"deployment_id": 1})
    # nem se habilitada por engano na allowlist: a tool confere a especialidade
    dev = _agent("dev-fullstack")
    dev.tools = [*dev.tools, "deploy_app"]
    db.session.commit()
    with pytest.raises(ToolError, match="restrita ao agente devops"):
        call("deploy_app", ctx, {"deployment_id": 1})


@pytest.mark.parametrize("manifest, msg", [
    ({"run": {"command": ["bash", "-c", "rm -rf ~"], "port": 8101}}, "não permitido"),
    ({"run": {"command": ["/bin/sh", "x"], "port": 8101}}, "não permitido"),
    ({"run": {"command": ["curl", "http://x"], "port": 8101}}, "não permitido"),
    ({"run": {"command": ["python", "app.py"], "port": 22}}, "port"),
    ({"run": {"command": ["python", "app.py"], "port": 9000}}, "port"),
    ({"run": {"command": ["python", "app.py"]}}, "port"),
    ({"run": {"command": "python app.py", "port": 8101}}, "lista"),
    ({"workdir": "../fora", "run": {"command": ["python", "a.py"], "port": 8101}}, "workdir"),
    ({"ci": {"steps": [["sh", "-c", "x"]]}, "run": {"command": ["python", "a.py"], "port": 8101}}, "não permitido"),
])
def test_manifest_validation_blocks_unsafe_commands(app, manifest, msg):
    with pytest.raises(cd.CDError, match=msg):
        cd.validate_manifest(manifest)


def test_manifest_env_never_receives_secrets(app, monkeypatch):
    monkeypatch.setenv("FLOW_API_TOKEN", "segredo")
    monkeypatch.setenv("A2A_BEARER_TOKEN", "segredo")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("OPENAI_API_KEY", "segredo")
    env = cd._env()
    assert not any(k in env for k in ("FLOW_API_TOKEN", "A2A_BEARER_TOKEN", "DATABASE_URL", "OPENAI_API_KEY")) and "PATH" in env
    ok = cd.validate_manifest({"run": {"command": ["python", "a.py"], "port": 8101, "env": {"MODO": "x", "MY_TOKEN": "y"}}})
    assert ok["run"]["env"] == {"MODO": "x"}


def test_api_approve_requires_green_ci_and_new_release_replaces_old(app, client):
    _, _, runner = _run("Site de boas-vindas #app")
    runner.run()
    dep = db.session.query(Deployment).one()
    dep.ci_report = [{"name": "x", "cmd": "x", "ok": False, "seconds": 0, "output": "falhou"}]
    db.session.commit()
    assert client.post(f"/api/deployments/{dep.id}/approve").status_code == 409
    assert client.post("/api/deployments/999/approve").status_code == 404
    assert client.get("/api/deployments").get_json()["deployments"][0]["status"] == "awaiting_approval"
    assert client.get(f"/api/deployments/{dep.id}/logs").get_json()["log"] == ""


def test_sprint_timeline_has_release_step(app):
    from flow import stages

    sp, _, runner = _run("Site de boas-vindas #app")
    runner.run()
    db.session.refresh(sp)
    steps = {s["key"]: s for s in stages.sprint_timeline(sp)["steps"]}
    assert steps["release"]["state"] == "active" and "aprovação" in steps["release"]["detail"]


# ----------------------------------------------------------------------------- runtime: recuperação e anti-loop
class Script:
    """LLM roteirizado: devolve as respostas em ordem (a última se repete)."""

    def __init__(self, *replies):
        self.replies, self.n = list(replies), 0

    def __call__(self, messages, cfg):
        r = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return r, {"in": 1, "out": 1}


def _ceremony_agent(slug):
    a = _agent(slug)
    a.provider = "scripted"
    db.session.commit()
    return a


def _accept_setup():
    sp, items, runner = _run("Tela de login")
    item = items[0]
    item.status, item.sprint_id, item.assignee = WorkItem.QA, sp.id, _agent("dev-fullstack")
    item.qa_verdict = {"passed": True, "notes": "ok", "by": "qa"}
    db.session.commit()
    return sp, item, runner


def _accept_task(item, runner):
    return {"ceremony": "accept", "item": runner._item_data(item), "requires": ["work_item_accept"],
            "recover": {"work_item_accept": {"item_id": item.id, "accepted": True, "reason": "ok"}},
            "recover_unless": ["rejeit", "reprov", "recus", "não aceit"]}


def test_announced_tool_call_is_recovered_and_audited(app, monkeypatch):
    """Log real (gemma3:4b): o PO escreveu 'Chamando work_item_accept para US-001.' sem emitir o JSON."""
    monkeypatch.setitem(llm.PROVIDERS, "scripted", Script("Chamando work_item_accept para US-001."))
    sp, item, runner = _accept_setup()
    po = _ceremony_agent("product-owner")
    res = runtime.run(po, _accept_task(item, runner), ctx=ToolContext(agent=po, workflow_id=sp.workflow_id, sprint_id=sp.id), context_id="t")
    assert "work_item_accept" in res.tool_calls
    assert db.session.get(WorkItem, item.id).status == "done"
    ev = db.session.query(AuditEvent).filter_by(event_type="tool_recovered").one()
    assert ev.target == "work_item_accept" and "Chamando" in ev.payload["model_text"]


def test_recovery_is_skipped_when_po_text_says_reject(app, monkeypatch):
    monkeypatch.setitem(llm.PROVIDERS, "scripted", Script("Vou rejeitar: work_item_accept com accepted=false."))
    sp, item, runner = _accept_setup()
    po = _ceremony_agent("product-owner")
    po.max_steps = 3
    db.session.commit()
    res = runtime.run(po, _accept_task(item, runner), ctx=ToolContext(agent=po, workflow_id=sp.workflow_id, sprint_id=sp.id), context_id="t")
    assert not res.ok and db.session.get(WorkItem, item.id).status != "done"
    assert db.session.query(AuditEvent).filter_by(event_type="tool_recovered").count() == 0


def test_final_without_required_tool_still_fails_without_recover_data(app, monkeypatch):
    monkeypatch.setitem(llm.PROVIDERS, "scripted", Script('{"final": "pronto!"}'))
    sp, item, runner = _accept_setup()
    sm = _ceremony_agent("scrum-master")
    sm.max_steps = 3
    db.session.commit()
    res = runtime.run(sm, {"ceremony": "plan", "requires": ["sprint_add_item"]}, ctx=ToolContext(agent=sm, workflow_id=sp.workflow_id, sprint_id=sp.id), context_id="t")
    assert not res.ok and "sprint_add_item" in res.error


def test_repeated_read_only_call_after_required_action_ends_the_task(app, monkeypatch):
    """Log real: o SM fez sprint_add_item e ficou relistando o backlog até estourar os passos."""
    sp, items, runner = _run("Tela de login")
    add = json.dumps({"tool": "sprint_add_item", "args": {"item_id": items[0].id, "assignee": "dev-fullstack"}})
    lst = json.dumps({"tool": "backlog_list", "args": {}})
    script = Script(add, lst, lst, lst, lst, lst)
    monkeypatch.setitem(llm.PROVIDERS, "scripted", script)
    sm = _ceremony_agent("scrum-master")
    res = runtime.run(sm, {"ceremony": "plan", "requires": ["sprint_add_item"]}, ctx=ToolContext(agent=sm, workflow_id=sp.workflow_id, sprint_id=sp.id), context_id="t")
    assert res.ok and "se repetiu" in res.text and script.n <= 4
    assert db.session.get(WorkItem, items[0].id).status == "sprint"


def test_repeat_before_required_action_does_not_end_the_task(app, monkeypatch):
    sp, items, runner = _run("Tela de login")
    lst = json.dumps({"tool": "backlog_list", "args": {}})
    add = json.dumps({"tool": "sprint_add_item", "args": {"item_id": items[0].id, "assignee": "dev-fullstack"}})
    monkeypatch.setitem(llm.PROVIDERS, "scripted", Script(lst, lst, lst, add, '{"final": "ok"}'))
    sm = _ceremony_agent("scrum-master")
    sm.max_steps = 8
    db.session.commit()
    res = runtime.run(sm, {"ceremony": "plan", "requires": ["sprint_add_item"]}, ctx=ToolContext(agent=sm, workflow_id=sp.workflow_id, sprint_id=sp.id), context_id="t")
    assert res.ok and "sprint_add_item" in res.tool_calls


def test_json_parser_handles_escaped_quotes_and_braces_in_strings():
    obj = runtime._first_json('Claro! {"tool":"write_workspace_file","args":{"path":"a.html","content":"<a href=\\"x\\">{ok}</a>\\\\"}} fim')
    assert obj["args"]["content"] == '<a href="x">{ok}</a>\\'
    assert runtime._first_json('```json\n{"final": "disse \\"oi\\""}\n```') == {"final": 'disse "oi"'}
    assert runtime._first_json("sem json") is None


# ----------------------------------------------------------------------------- backlog: duplicatas e critérios
def test_backlog_create_reports_duplicates_without_failing_and_caps_criteria(app):
    po = _agent("product-owner")
    ctx = ToolContext(agent=po)
    a = call("backlog_create", ctx, {"title": "Tela de Login - TailwindCSS & ShadcnUI", "acceptance_criteria": ["a", "b"], "points": 8})
    for title in ("US-002 - Tela de Login - TailwindCSS & ShadcnUI", "tela de login  tailwindcss e shadcnui!"):  # o caso do print
        r = call("backlog_create", ctx, {"title": title, "points": 3})
        assert r["created"] is False and r["duplicate_of"] == "US-001"  # resposta válida, não erro: o PO não entra em loop
    assert db.session.query(WorkItem).count() == 1
    big = call("backlog_create", ctx, {"title": "Cadastro de usuários", "acceptance_criteria": [str(i) for i in range(10)], "points": 7})
    assert len(db.session.get(WorkItem, big["id"]).acceptance_criteria) == 6 and "aviso" in big  # ajusta, não recusa: recusar fazia o PO girar até estourar os passos
    assert big["points"] == 8  # 7 → Fibonacci mais próximo
    ok = call("backlog_create", ctx, {"title": "US-009: Relatório mensal de vendas", "points": 3})
    assert ok["title"] == "Relatório mensal de vendas" and a["key"] == "US-001"  # prefixo US-xxx do modelo é removido


def test_refine_of_an_already_covered_request_succeeds_instead_of_failing(app):
    """Sprint 4 do usuário: pedido parecido com histórias existentes → antes: 'o PO não criou nenhuma user story'."""
    sp, items, runner = _run("Tela de login")
    again, reply = runner.refine("Tela de login")  # o mock tenta criar a mesma história de novo
    assert [i.key for i in again] == ["US-001"] and "já está coberto" in reply
    assert db.session.query(WorkItem).count() == 1
    assert db.session.query(AuditEvent).filter_by(event_type="ceremony", target="refine_duplicates").count() == 1


def test_devops_and_qa_cannot_be_assigned_stories(app):
    sp, items, _ = _run("Tela de login")
    ctx = ToolContext(agent=_agent("scrum-master"), workflow_id=sp.workflow_id, sprint_id=sp.id)
    for who in ("devops", "qa"):
        with pytest.raises(ToolError, match="não implementa"):
            call("sprint_add_item", ctx, {"item_id": items[0].id, "assignee": who})


# ----------------------------------------------------------------------------- achados do log real (gemma3:4b)
def test_skill_name_called_as_tool_is_redirected_to_load_skill(app):
    qa = _agent("qa")
    out = call("code-review-checklist", ToolContext(agent=qa, workflow_id="sprint-1"), {})
    assert "Conflito de interesses" in out  # conteúdo da skill
    assert db.session.query(AuditEvent).filter_by(event_type="tool_redirect", target="code-review-checklist").count() == 1
    with pytest.raises(ToolError, match="load_skill"):
        call("nao-e-skill-nem-tool", ToolContext(agent=qa, workflow_id="sprint-1"), {})


@pytest.mark.parametrize("given", ["entrega.md", "workspace/entrega.md", "/entrega.md", "sprint-1/entrega.md", "US-001/entrega.md"])
def test_deliverable_path_is_normalized_into_the_story_folder(app, given):
    sp, items, _ = _run("Tela de login")
    items[0].status, items[0].sprint_id = WorkItem.PROGRESS, sp.id
    db.session.commit()
    ctx = ToolContext(agent=_agent("dev-fullstack"), workflow_id=sp.workflow_id, sprint_id=sp.id)
    call("write_workspace_file", ctx, {"path": given, "content": "x"})
    assert (gitflow.root() / "sprint-1" / "US-001" / "entrega.md").read_text() == "x"


def test_reading_a_folder_lists_its_files_and_verify_task_carries_the_file_list(app):
    """Log real: o QA pediu read_workspace_file('US-001') (a pasta) e recebeu 'arquivo não encontrado'."""
    sp, items, runner = _run("Tela de login")
    items[0].status, items[0].sprint_id = WorkItem.PROGRESS, sp.id
    db.session.commit()
    dev = ToolContext(agent=_agent("dev-fullstack"), workflow_id=sp.workflow_id, sprint_id=sp.id)
    call("write_workspace_file", dev, {"path": "US-001/index.html", "content": "<h1>login</h1>"})
    out = call("read_workspace_file", ToolContext(agent=_agent("qa"), workflow_id=sp.workflow_id, sprint_id=sp.id), {"path": "US-001"})
    assert out["arquivos"] == ["US-001/index.html"] and "dica" in out
    assert runner._files(items[0]) == ["US-001/index.html"]


def test_refine_failure_message_says_what_the_po_actually_did(app, monkeypatch):
    """Antes só dizia 'Não concluí em 6 passos'; agora mostra erros de ferramenta e a última resposta do modelo."""
    monkeypatch.setitem(llm.PROVIDERS, "scripted", Script("Claro! Vou criar as histórias agora mesmo."))
    po = _ceremony_agent("product-owner")
    po.max_steps = 2
    db.session.commit()
    sp = Sprint(name="S")
    db.session.add(sp)
    db.session.commit()
    with pytest.raises(RuntimeError, match="Última resposta do modelo: 'Claro! Vou criar"):
        scrum.SprintRunner(sp).refine("Tela de login")


def test_sprint_never_ends_without_a_retrospective(app, monkeypatch):
    """Print real: a etapa 'Retrospectiva' ficou vazia porque o SM não chamou sprint_set_retro."""
    sp, items, runner = _run("Tela de login")
    sm = _agent("scrum-master")
    sm.provider = "scripted"
    db.session.commit()
    real = llm.PROVIDERS["mock"]

    def sm_silent(messages, cfg):  # SM só diz 'ok' (sem tool); os demais agentes seguem no mock
        return real(messages, cfg) if cfg.provider != "scripted" or "[[role:developer" in messages[0]["content"] else ('{"final": "ok"}', {"in": 1, "out": 1})

    monkeypatch.setitem(llm.PROVIDERS, "scripted", Script('{"final": "Concluído."}'))
    # o plan/implement/verify do SM no mock usam outro provider; aqui só a retro do SM é silenciosa
    sm.provider = "mock"
    db.session.commit()
    monkeypatch.setattr(scrum.SprintRunner, "_send", _silent_retro(scrum.SprintRunner._send))
    stats = runner.run()
    db.session.refresh(sp)
    assert stats["done"] == 1 and sp.retro and "registrada automaticamente" in sp.retro
    assert db.session.query(AuditEvent).filter_by(event_type="ceremony", target="retro_autofilled").count() == 1


def _silent_retro(orig):
    def send(self, sender, target, text, data, enforce=True):
        if data.get("ceremony") == "retro":
            return {"status": {"state": "TASK_STATE_COMPLETED"}, "history": [], "artifacts": [], "id": "x", "contextId": "x"}
        return orig(self, sender, target, text, data, enforce)
    return send


def test_remove_duplicate_story_from_backlog_via_api(app, client):
    sp, items, runner = _run("Tela de login")
    r = client.delete(f"/api/items/{items[0].id}")
    assert r.status_code == 200 and db.session.query(WorkItem).count() == 0
    assert db.session.query(AuditEvent).filter_by(event_type="backlog_change", target="US-001").count() == 1
    assert client.delete("/api/items/999").status_code == 404
    sp2, items2, runner2 = _run("Cadastro de usuário")
    items2[0].status = WorkItem.DONE
    db.session.commit()
    assert client.delete(f"/api/items/{items2[0].id}").status_code == 409  # só itens do backlog


def test_static_site_is_deployed_live_server_style_without_manifest(app):
    _, _, runner = _run("Página de boas-vindas #site")
    assert runner.run()["done"] == 1
    dep = db.session.query(Deployment).one()
    assert dep.status == "awaiting_approval" and dep.manifest["run"]["static"] is True and dep.manifest_path.endswith("/index.html")
    assert dep.manifest["run"]["command"][:3] == ["python", "-m", "http.server"] and "127.0.0.1" in dep.manifest["run"]["command"]
    port = _free_port()
    app.config["FLOW_CD_PORT_RANGE"] = (port, port)
    dep.manifest = {**dep.manifest, "run": {**dep.manifest["run"], "port": port, "command": ["python", "-m", "http.server", str(port), "--bind", "127.0.0.1"]}}
    db.session.commit()
    cd.approve(dep, "alex")
    try:
        out = call("deploy_app", ToolContext(agent=cd.devops_agent(), workflow_id="sprint-1"), {"deployment_id": dep.id})
        assert b"<h1>" in urllib.request.urlopen(out["url"], timeout=5).read()
    finally:
        cd.stop(dep)


def test_static_manifest_needs_no_command_but_still_validates_port_and_exe(app):
    m = cd.validate_manifest({"run": {"static": True, "port": 8105}})
    assert m["run"]["command"] == ["python", "-m", "http.server", "8105", "--bind", "127.0.0.1"] and m["run"]["health"] == "/"
    with pytest.raises(cd.CDError, match="port"):
        cd.validate_manifest({"run": {"static": True, "port": 80}})
