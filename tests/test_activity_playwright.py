"""Atividade detalhada do dev, fases do sandbox no feed, testes escritos pelo QA e Playwright."""
import os
from pathlib import Path

import pytest

from flow import activity, sandbox, scrum
from flow.extensions import db
from flow.models import AgentConfig, AuditEvent, Sprint, WorkItem
from flow.tools import ToolContext, ToolError, call

pytestmark = pytest.mark.filterwarnings("ignore")


def w(path, content):
    return activity.describe_tool("write_workspace_file", {"path": path, "content": content})


def test_python_activity_lists_classes_routes_functions_and_sqlalchemy():
    t = w("US-005/app.py", "from flask import Flask\nfrom flask_sqlalchemy import SQLAlchemy\n\nclass User(db.Model):\n    __tablename__ = 'users'\n\n"
                           "@app.route('/login', methods=['POST'])\ndef login():\n    pass\n\n@app.get('/home')\ndef home():\n    pass\n")
    assert "🐍" in t and "modelos SQLAlchemy User" in t and "tabelas users" in t and "/login" in t and "/home" in t and "login, home" in t and "flask" in t


def test_other_kinds_sql_html_deps_alembic_tests_manifest():
    assert "script SQL" in w("db/schema.sql", "CREATE TABLE users (id int);") and "CREATE" in w("db/schema.sql", "CREATE TABLE users (id int);")
    html = w("US-005/templates/login.html", "<script src='tailwind'></script><form><input><input></form>")
    assert "🌐" in html and "formulário com 2 campo(s)" in html and "TailwindCSS" in html
    deps = w("US-005/requirements.txt", "flask>=3\nsqlalchemy\nalembic\n# x\n")
    assert "📦" in deps and "flask, sqlalchemy, alembic" in deps
    alem = w("US-005/migrations/versions/001_users.py", "revision = '1'\nfrom alembic import op\ndef upgrade():\n    op.create_table('users')\n")
    assert "migração Alembic" in alem and "create_table users" in alem
    assert "teste Playwright" in w("US-005/tests/test_qa.py", "from playwright.sync_api import Page\ndef test_login(page): pass\n")
    assert "teste pytest: test_soma" in w("US-005/tests/test_x.py", "def test_soma(): pass\n")
    assert "manifesto de deploy" in w("US-005/flow-app.toml", 'name = "login"\n')


def test_syntax_error_is_flagged_and_returned_to_the_agent(app):
    assert "erro de sintaxe" in w("US-005/app.py", "def x(:\n")
    dev = db.session.query(AgentConfig).filter_by(slug="dev-fullstack").first()
    r = call("write_workspace_file", ToolContext(agent=dev, workflow_id="sprint-1"), {"path": "US-005/app.py", "content": "def x(:\n"})
    assert "aviso" in r and "erro de sintaxe" in r["o_que"]


def test_feed_shows_the_classified_activity_even_for_big_files(app):
    dev = db.session.query(AgentConfig).filter_by(slug="dev-fullstack").first()
    big = "def f():\n    return 1\n" + "# x\n" * 3000  # > 4000 chars: o payload da auditoria é truncado, o resumo vem do resultado
    call("write_workspace_file", ToolContext(agent=dev, workflow_id="sprint-1"), {"path": "US-005/app.py", "content": big})
    top = activity.feed(5)[0]["text"]
    assert "escrevendo código Python US-005/app.py" in top and "funções f" in top and "sintaxe" not in top


def test_qa_may_only_write_its_test_file_and_dev_may_not_touch_it(app):
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    dev = db.session.query(AgentConfig).filter_by(slug="dev-fullstack").first()
    cq, cd_ = ToolContext(agent=qa, workflow_id="sprint-1"), ToolContext(agent=dev, workflow_id="sprint-1")
    assert call("write_workspace_file", cq, {"path": "US-005/tests/test_qa.py", "content": "def test_a(): pass\n"})["path"] == "US-005/tests/test_qa.py"
    with pytest.raises(ToolError, match="só escreve testes"):
        call("write_workspace_file", cq, {"path": "US-005/app.py", "content": "x=1"})
    with pytest.raises(ToolError, match="do QA"):
        call("write_workspace_file", cd_, {"path": "US-005/tests/test_qa.py", "content": "def test_a(): pass\n"})


@pytest.fixture()
def sprint_item(app):
    app.config["FLOW_SANDBOX_MODE"] = "local"
    sp = Sprint(name="Sprint 1")
    db.session.add(sp)
    db.session.commit()
    runner = scrum.SprintRunner(sp)
    items, _ = runner.refine("Saudação em Python #py")
    return sp, items[0], runner


def test_qa_writes_the_tests_when_the_dev_brought_none(sprint_item):
    sp, item, runner = sprint_item
    assert runner.run()["done"] == 1
    db.session.refresh(item)
    assert (sandbox.story_dir(item) / "tests" / "test_qa.py").is_file()
    assert item.status == WorkItem.DONE
    ev = db.session.query(AuditEvent).filter_by(event_type="sandbox_phase").all()
    assert any("rodando pytest" in (e.payload or {}).get("text", "") for e in ev)
    assert any("pytest" in x["text"] for x in activity.feed(60))
    assert activity.snapshot()["qa"]["state"] == "idle"  # o runner libera o QA depois do sandbox


def test_dev_tests_replace_qa_tests(sprint_item):
    sp, item, runner = sprint_item
    item.sprint_id = sp.id
    db.session.commit()
    base = sandbox.story_dir(item)
    (base / "tests").mkdir(parents=True)
    (base / "app.py").write_text("x = 1\n")
    assert sandbox.needs_qa_tests(item)
    (base / "tests" / "test_dev.py").write_text("def test_x():\n    assert 1\n")
    assert not sandbox.needs_qa_tests(item)


def test_playwright_detection_and_allowlist(tmp_path, app):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ui.py").write_text("from playwright.sync_api import Page\n")
    assert sandbox.uses_playwright(tmp_path)
    assert sandbox.uses_playwright(Path("/nonexistent"), ["pytest-playwright"])
    assert not sandbox.check_packages(["playwright", "pytest-playwright"])
    assert "playwright" in sandbox.DOCKERFILE_E2E and "--with-deps chromium" in sandbox.DOCKERFILE_E2E


UI_APP = ("import os\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n\nclass H(BaseHTTPRequestHandler):\n    def do_GET(self):\n"
          "        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers()\n"
          "        self.wfile.write('<h1>Entrar</h1><form><input name=email></form>'.encode())\n    def log_message(self, *a): pass\n\n"
          "if __name__ == '__main__':\n    HTTPServer((os.environ.get('HOST','127.0.0.1'), int(os.environ.get('PORT','8000'))), H).serve_forever()\n")
UI_TEST = ("from playwright.sync_api import Page, expect\n\n\ndef test_tela_de_login(start_app, page: Page):\n    url = start_app('app.py')\n    page.goto(url)\n"
           "    expect(page.locator('h1')).to_have_text('Entrar')\n    page.fill('input[name=email]', 'a@b.com')\n")


@pytest.mark.skipif(not os.path.isdir(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))),
                    reason="Playwright/Chromium não instalados")
def test_playwright_e2e_runs_in_the_sandbox_local_mode(sprint_item):
    sp, item, runner = sprint_item
    item.sprint_id = sp.id
    db.session.commit()
    base = sandbox.story_dir(item)
    (base / "tests").mkdir(parents=True)
    (base / "app.py").write_text(UI_APP)
    (base / "tests" / "test_qa.py").write_text(UI_TEST)
    (base / "requirements.txt").write_text("playwright==1.63.0\n")
    r = sandbox.run(item)
    assert r["ok"], (r["summary"], r["output"])


# ----------------------------------------------------------------------------- painel “Detalhe da tarefa”
def test_trace_records_model_files_tools_and_sandbox(app, client):
    from flow.models import AgentTrace
    app.config["FLOW_SANDBOX_MODE"] = "local"
    sp = Sprint(name="Sprint 1")
    db.session.add(sp)
    db.session.commit()
    runner = scrum.SprintRunner(sp)
    runner.refine("Saudação em Python #py")
    assert runner.run()["done"] == 1
    d = client.get("/api/agents/dev-fullstack/trace?run=" + client.get("/api/agents/dev-fullstack/trace").get_json()["runs"][-1]["run"]).get_json()
    kinds = [r["kind"] for r in d["rows"]]
    assert kinds[0] == "task" and "think" in kinds and "tool" in kinds and kinds[-1] == "final"
    w = next(r for r in d["rows"] if r["meta"].get("tool") == "write_workspace_file" and r["meta"].get("name") == "app.py")
    assert w["meta"]["ext"] == "py" and w["meta"]["action"] == "escreveu" and "def saudacao" in w["body"] and "🐍" in w["title"] and w["ms"] is not None
    assert next(r for r in d["rows"] if r["kind"] == "think")["meta"]["model"]
    # o QA: escreveu os testes e depois o sandbox mostrou instalação/pytest
    qa = client.get("/api/agents/qa/trace").get_json()
    allrows = []
    for run in qa["runs"]:
        allrows += client.get(f"/api/agents/qa/trace?run={run['run']}").get_json()["rows"]
    titles = " | ".join(r["title"] for r in allrows)
    assert "test_qa.py" in titles and "rodando pytest" in titles and "pytest passou" in titles
    assert any(r["kind"] == "test" and r["state"] == "ok" for r in allrows)
    assert any(r["kind"] == "phase" for r in allrows)
    # incremental: com o cursor, só volta o que mudou
    again = client.get(f"/api/agents/dev-fullstack/trace?run={d['run']['run']}&cursor={d['cursor']}").get_json()
    assert again["rows"] == []
    assert db.session.query(AgentTrace).count() > 10
    assert client.get("/api/agents/nao-existe/trace").status_code == 404


def test_trace_keeps_only_recent_runs_and_caps_big_bodies(app):
    from flow import trace
    from flow.models import AgentTrace
    for i in range(trace.KEEP_RUNS + 5):
        trace.begin("qa", "verify", "US-001", title=f"run {i}")
    assert db.session.query(AgentTrace.run_id).filter_by(agent_slug="qa").distinct().count() == trace.KEEP_RUNS
    rid = trace.latest_run("qa")
    row = trace.add(rid, "qa", "tool", "x", body="a" * 50000)
    assert len(db.session.get(AgentTrace, row).body) < 20100


def test_old_databases_get_the_tools_the_pipeline_needs(app):
    from flow.seed import ensure_required_tools
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    qa.tools = ["memory_save", "read_workspace_file", "work_item_verdict"]  # banco antigo: sem write_workspace_file / qa_run_tests
    db.session.commit()
    added = ensure_required_tools()
    assert "qa:write_workspace_file" in added and "qa:qa_run_tests" in added
    db.session.refresh(qa)
    assert {"write_workspace_file", "qa_run_tests", "memory_save"} <= set(qa.tools)
    assert ensure_required_tools() == []  # idempotente
    evs = [e for e in db.session.query(AuditEvent).filter_by(event_type="agent_config_change", actor="system") if "ferramentas" in e.payload.get("auto", "")]
    assert len(evs) == 1


def test_docker_fallback_explains_why_in_the_feed(sprint_item, app, monkeypatch):
    sp, it, runner = sprint_item
    it.sprint_id = sp.id
    db.session.commit()
    base = sandbox.story_dir(it)
    (base / "tests").mkdir(parents=True)
    (base / "app.py").write_text("x = 1\n")
    (base / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    app.config["FLOW_SANDBOX_MODE"] = "auto"
    monkeypatch.setattr(sandbox, "docker_ok", lambda force=False: False)
    sandbox._DOCKER.update(why="comando `docker` não encontrado no PATH")
    r = sandbox.run(it)
    assert r["ok"] and "SEM isolamento" in r["isolation"]
    assert any("Docker indisponível" in x["text"] and "PATH" in x["text"] for x in activity.feed(30))


def test_syntax_errors_are_reported_clearly_and_blamed_correctly(sprint_item):
    sp, it, runner = sprint_item
    it.sprint_id = sp.id
    db.session.commit()
    base = sandbox.story_dir(it)
    (base / "tests").mkdir(parents=True)
    (base / "app.py").write_text("x = 1\n")
    (base / "tests" / "test_qa.py").write_text('def test_a():\n    assert b"inválido" in b"x"\n', encoding="utf-8")  # o erro real visto no Windows
    assert "erro de sintaxe" in sandbox.qa_test_error(it) and "linha 2" in sandbox.qa_test_error(it)
    r = sandbox.run(it)
    assert not r["ok"] and r["fault"] == "qa" and "erro de sintaxe do QA" in r["summary"] and not sandbox.setup_failure(r)
    (base / "tests" / "test_qa.py").write_text("def test_a():\n    assert True\n")
    (base / "app.py").write_text("def x(:\n")
    r = sandbox.run(it)
    assert r["fault"] == "dev" and "do dev em app.py" in r["summary"]
    (base / "tests" / "test_qa.py").unlink()
    assert "não escreveu" in sandbox.qa_test_error(it)


def test_missing_module_tells_the_dev_to_declare_it():
    out = "app.py:1: in <module>\n    from flask import Flask\nE   ModuleNotFoundError: No module named 'flask_cors'\n1 error in 0.17s"
    assert "flask_cors" in sandbox._summary(2, out, "test") and "requirements.txt" in sandbox._summary(2, out, "test")


def test_local_venv_gets_the_same_base_packages_as_the_image(tmp_path, app, monkeypatch):
    """Windows: o venv local só tinha pytest-playwright e o app importava flask (ModuleNotFoundError)."""
    calls = []

    class R:
        returncode, stdout, stderr = 0, b"1 passed", b""

    monkeypatch.setattr(sandbox.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), R())[1])
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a(): pass\n")
    code, out, phase, extra = sandbox._run_local(tmp_path, ["sqlalchemy"])
    install = next(c for c in calls if "install" in c)
    assert {"pytest", "flask", "httpx", "sqlalchemy"} <= set(install)
    assert phase == "test" and code == 0
    env_used = [kw for kw in [{}]]  # (a env UTF-8 é verificado abaixo)
    assert sandbox._run_local.__code__.co_consts and any("PYTHONUTF8" in str(c) for c in sandbox._run_local.__code__.co_consts)


# ----------------------------------------------------------------------------- meta da sprint não herda a de sprints antigas
def test_old_sprint_goals_are_not_recalled_or_saveable(app):
    from flow import memory as mem
    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    mem.save(po, "A meta desta sprint é implementar a tela de login com TailwindCSS & ShadcnUI", kind="goal", scope="shared", importance=5, tags=["sprint_goal"])
    assert mem.search(po, "sprint goal meta da sprint login", k=5) == []
    with pytest.raises(ToolError, match="final"):
        call("memory_save", ToolContext(agent=po, workflow_id="t"), {"content": "meta velha", "kind": "goal", "scope": "shared"})


def test_goal_must_talk_about_the_current_backlog(app):
    sp = Sprint(name="Sprint 1")
    db.session.add(sp)
    db.session.commit()
    items, _ = scrum.SprintRunner(sp).refine("Tabela de produtos em index.html usando TailwindCSS")
    assert scrum.goal_matches("Criar uma tabela de produtos em index.html usando TailwindCSS.", items)
    assert not scrum.goal_matches("A meta desta sprint é implementar a tela de login com TailwindCSS & ShadcnUI e o formulário de cadastro de clientes.", items)
    assert not scrum.goal_matches("", items)


def test_plan_replaces_a_stale_goal_with_the_real_stories_and_forbids_tools(app, monkeypatch):
    from flow import llm
    from flow.models import AuditEvent
    orig = llm.PROVIDERS["mock"]
    seen = {}

    def stale_po(messages, cfg, on_chunk=None):
        if any('"ceremony": "goal"' in m["content"] for m in messages if m["role"] == "user"):
            if "TOOL_RESULT" in messages[-1]["content"]:
                seen["denied"] = messages[-1]["content"]
                return '{"final": "A meta desta sprint é implementar a tela de login com TailwindCSS & ShadcnUI e o formulário de cadastro de clientes."}', {}
            return '{"tool": "memory_search", "args": {"query": "sprint goal"}}', {}   # o PO tenta consultar a memória em vez de responder
        return orig(messages, cfg)

    monkeypatch.setitem(llm.PROVIDERS, "mock", stale_po)
    sp = Sprint(name="Sprint 1")
    db.session.add(sp)
    db.session.commit()
    runner = scrum.SprintRunner(sp)
    runner.refine("Tabela de produtos em index.html usando TailwindCSS")
    runner.plan()
    db.session.refresh(sp)
    assert "NÃO use ferramentas" in seen["denied"]
    assert sp.goal.startswith("Entregar:") and "produtos" in sp.goal.lower() and "login" not in sp.goal.lower()
    ev = db.session.query(AuditEvent).filter_by(event_type="ceremony", target="goal_overridden").first()
    assert ev and "login" in ev.payload["model_goal"]


def test_developers_get_enough_steps_and_ceremonies_restrict_tools(app):
    from flow.seed import MIN_DEV_STEPS, ensure_required_tools
    dev = db.session.query(AgentConfig).filter_by(slug="dev-fullstack").first()
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    dev.max_steps = 6
    db.session.commit()
    assert f"dev-fullstack:max_steps={MIN_DEV_STEPS}" in ensure_required_tools()
    db.session.refresh(dev)
    assert dev.max_steps == MIN_DEV_STEPS
    dev.max_steps = 20  # nunca reduz o que o usuário aumentou
    db.session.commit()
    ensure_required_tools()
    db.session.refresh(dev)
    assert dev.max_steps == 20
