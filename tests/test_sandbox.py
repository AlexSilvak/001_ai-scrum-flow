"""Sandbox de testes do QA: pytest isolado, allowlist de pacotes, rejeição automática e trava do veredito."""
import shutil
import subprocess
from pathlib import Path

import pytest

from flow import sandbox, scrum
from flow.extensions import db
from flow.models import AgentConfig, AuditEvent, Sprint, WorkItem
from flow.tools import ToolContext, ToolError, call

pytestmark = pytest.mark.filterwarnings("ignore")


def _docker_image() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", "flow-sandbox:1"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


needs_docker = pytest.mark.skipif(not _docker_image(), reason="imagem flow-sandbox:1 / Docker indisponível")

OK_APP = "def soma(a, b):\n    return a + b\n"
OK_TEST = "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\nfrom app import soma\n\n\ndef test_soma():\n    assert soma(1, 2) == 3\n"
BAD_TEST = OK_TEST.replace("== 3", "== 4")


@pytest.fixture()
def story(app):
    app.config["FLOW_SANDBOX_MODE"] = "local"
    sp = Sprint(name="Sprint 1")
    db.session.add(sp)
    db.session.commit()
    items, _ = scrum.SprintRunner(sp).refine("Calculadora de soma")
    it = items[0]
    it.sprint_id = sp.id
    db.session.commit()

    def write(files: dict):
        base = sandbox.story_dir(it)
        for rel, text in files.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(text, encoding="utf-8")
        return it
    return write


def test_docs_only_is_rejected_before_qa(story):
    it = story({"entrega.md": "# pronto\nTudo testado."})
    assert "não contém código" in sandbox.precheck(it)
    assert sandbox.run(it)["ok"] is False


def test_static_site_skips_pytest(story):
    it = story({"index.html": "<h1>oi</h1>", "entrega.md": "x"})
    r = sandbox.run(it)
    assert r["ok"] and r["skipped"] and sandbox.passed_this_round(it)


def test_python_without_tests_fails_with_exit_5(story):
    it = story({"app.py": OK_APP})
    r = sandbox.run(it)
    assert not r["ok"] and r["exit_code"] == 5 and "nenhum teste" in r["summary"]


def test_passing_and_failing_pytest_local(story):
    it = story({"app.py": OK_APP, "tests/test_soma.py": OK_TEST})
    assert sandbox.run(it)["ok"] and sandbox.passed_this_round(it)
    story({"tests/test_soma.py": BAD_TEST})
    r = sandbox.run(it)
    assert not r["ok"] and "failed" in r["summary"]
    assert "pytest no sandbox" in sandbox.feedback(it)
    assert db.session.query(AuditEvent).filter_by(event_type="sandbox_run").count() == 2


def test_package_outside_allowlist_blocks_run(story):
    it = story({"app.py": OK_APP, "tests/test_soma.py": OK_TEST, "requirements.txt": "flask\nleftpad-malicioso\n"})
    r = sandbox.run(it)
    assert not r["ok"] and r["phase"] == "setup" and "allowlist" in r["summary"] and sandbox.setup_failure(r)


@pytest.mark.parametrize("line", ["git+https://evil/x.git", "-r outro.txt", "--index-url http://evil", "https://evil/x.whl", "-e ."])
def test_requirement_urls_and_options_are_refused(story, line):
    it = story({"app.py": OK_APP, "requirements.txt": line + "\n"})
    assert "formato não permitido" in sandbox.run(it)["summary"]


def test_snapshot_never_runs_in_place_and_is_cleaned(story):
    it = story({"app.py": OK_APP, "tests/test_soma.py": OK_TEST})
    sandbox.run(it)
    assert not list((Path(sandbox._c("FLOW_WORKSPACE")) / ".sandbox").glob("*"))
    assert not (sandbox.story_dir(it) / ".pytest_cache").exists()


def test_verdict_cannot_pass_without_green_pytest(story, app):
    it = story({"app.py": OK_APP, "tests/test_soma.py": BAD_TEST})
    sandbox.run(it)
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    ctx = ToolContext(agent=qa, workflow_id="t")
    with pytest.raises(ToolError, match="pytest"):
        call("work_item_verdict", ctx, {"item_id": it.id, "passed": True, "notes": "ok"})
    assert call("work_item_verdict", ctx, {"item_id": it.id, "passed": False, "notes": "falhou"})["passed"] is False


def test_qa_run_tests_tool_is_qa_only(story, app):
    it = story({"app.py": OK_APP, "tests/test_soma.py": OK_TEST})
    qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
    r = call("qa_run_tests", ToolContext(agent=qa, workflow_id="t"), {"item_id": it.id})
    assert r["ok"] is True
    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    with pytest.raises(ToolError):
        call("qa_run_tests", ToolContext(agent=po, workflow_id="t"), {"item_id": it.id})


def test_runner_auto_rejects_docs_only_without_calling_qa(app):
    app.config["FLOW_SANDBOX_MODE"] = "local"
    sp = Sprint(name="Sprint 1")
    db.session.add(sp)
    db.session.commit()
    runner = scrum.SprintRunner(sp)
    items, _ = runner.refine("Tela de login")
    item = items[0]
    orig = sandbox.code_files
    sandbox.code_files = lambda i: []  # simula dev que só descreve
    try:
        runner.plan(); runner.execute()
    finally:
        sandbox.code_files = orig
    db.session.refresh(item)
    assert item.status == WorkItem.REJECTED and "não contém código" in item.notes


def test_docker_unavailable_in_docker_mode_is_an_impediment(story, app):
    app.config["FLOW_SANDBOX_MODE"] = "docker"
    sandbox._DOCKER.update(t=1e18, ok=False)
    try:
        r = sandbox.run(story({"app.py": OK_APP, "tests/test_soma.py": OK_TEST}))
    finally:
        sandbox._DOCKER.update(t=0.0, ok=False)
    assert sandbox.setup_failure(r) and "Docker" in r["summary"]


# ----------------------------------------------------------------------------- container real
@needs_docker
def test_container_runs_pytest_without_network_and_readonly(story, app):
    app.config["FLOW_SANDBOX_MODE"] = "docker"
    sandbox._DOCKER.update(t=0.0, ok=False)
    it = story({"app.py": OK_APP, "tests/test_soma.py": OK_TEST})
    r = sandbox.run(it)
    assert r["ok"] and r["isolation"] == "container", (r["summary"], r["output"])
    # o código do LLM tenta sair para a rede, escrever fora do /tmp e ler a raiz do host
    story({"tests/test_escape.py": (
        "import socket, os, pytest\n"
        "def test_no_network():\n    with pytest.raises(OSError):\n        socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "def test_readonly_root():\n    with pytest.raises(OSError):\n        open('/etc/pwned', 'w')\n"
        "def test_readonly_workdir():\n    with pytest.raises(OSError):\n        open('/work/pwned', 'w')\n"
        "def test_not_root():\n    assert os.getuid() == 1000\n")})
    r = sandbox.run(it)
    assert r["ok"], r["output"]


@needs_docker
def test_container_timeout_kills_runaway_test(story, app):
    app.config.update(FLOW_SANDBOX_MODE="docker", FLOW_SANDBOX_TIMEOUT=4)
    sandbox._DOCKER.update(t=0.0, ok=False)
    it = story({"app.py": OK_APP, "tests/test_loop.py": "def test_loop():\n    while True:\n        pass\n"})
    r = sandbox.run(it)
    assert not r["ok"] and r["exit_code"] == 124
    left = subprocess.run(["docker", "ps", "-q", "--filter", "label=flow=sandbox"], capture_output=True, text=True).stdout.strip()
    assert left == ""
