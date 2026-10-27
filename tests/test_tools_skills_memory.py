import json

from flow import memory as mem
from flow import skills as skl
from flow.extensions import db
from flow.models import AgentConfig
from flow.tools import ToolContext, ToolError, call


def agent(slug):
    return db.session.query(AgentConfig).filter_by(slug=slug).first()


def ctx(slug, wf="t"):
    return ToolContext(agent=agent(slug), workflow_id=wf)


def test_tool_outside_allowlist_is_denied(app):
    for name in ("backlog_create", "criar_usuario"):
        try:
            call(name, ctx("dev-fullstack"), {"title": "x", "nome": "a", "perfil": "ti"})
        except ToolError as e:
            assert "habilitada" in str(e) or "restrita" in str(e)
        else:
            raise AssertionError(name)


def test_workspace_is_sandboxed(app):
    c = ctx("dev-fullstack", "sprint-9")
    assert call("write_workspace_file", c, {"path": "US-1/a.md", "content": "oi"})["bytes"] == 2
    assert call("read_workspace_file", c, {"path": "US-1/a.md"}) == "oi"
    for bad in ("../../etc/passwd", "/etc/passwd", "../sprint-1/x"):
        try:
            call("read_workspace_file", c, {"path": bad})
        except ToolError as e:
            assert "fora do workspace" in str(e) or "não encontrado" in str(e)
        else:
            raise AssertionError(bad)


def test_domain_tools_require_rbac(app):
    a = agent("dev-fullstack")
    a.tools = [*a.tools, "criar_usuario"]
    db.session.commit()
    c = ToolContext(agent=a, workflow_id="t", user_email="intruso@x.com")
    try:
        call("criar_usuario", c, {"nome": "Ana", "perfil": "Ti"})
    except ToolError as e:
        assert "Acesso Negado" in str(e)
    else:
        raise AssertionError
    c.user_email = "alex.silvak23@gmail.com"
    assert call("criar_usuario", c, {"nome": "Ana Souza", "perfil": "Ti"})["usuario_id"] == "user_ana_souza"


def test_memory_private_vs_shared_and_ranking(app):
    po, qa = agent("product-owner"), agent("qa")
    mem.save(po, "Cliente exige login com SSO corporativo", kind="decision", importance=5)
    mem.save(po, "Segredo privado do PO sobre orçamento", scope="agent")
    mem.save(po, "Time decidiu usar Tailwind no frontend", scope="shared", importance=4)
    found = [m.content for m in mem.search(qa, "frontend Tailwind")]
    assert any("Tailwind" in c for c in found) and not any("privado" in c for c in found)
    assert "SSO" in mem.search(po, "login SSO")[0].content


def test_skills_path_per_agent_and_allowlist(app):
    po, qa = agent("product-owner"), agent("qa")
    assert "user-story-writing" in skl.discover(po) and "code-review-checklist" not in skl.discover(po)
    assert "code-review-checklist" in skl.discover(qa)
    assert "definition-of-done" in skl.discover(qa)  # shared
    qa.skills_allow = ["definition-of-done"]
    assert set(skl.discover(qa)) == {"definition-of-done"}
    body = call("load_skill", ctx("qa"), {"name": "definition-of-done"})
    assert len(body) > 20
    try:
        skl.read_skill_file(qa, "definition-of-done", "../../../pyproject.toml")
    except PermissionError:
        pass
    else:
        raise AssertionError("path traversal em skill")


def test_context_budget_and_paths(app, tmp_path):
    from flow import context as cx

    po = agent("product-owner")
    doc = tmp_path / "produto.md"
    doc.write_text("Visão do produto: " + "x" * 8000)
    po.context = {"max_tokens": 700, "context_paths": [str(doc)], "memory_k": 0}
    system, hist, meta = cx.build(po, query="oi", external_tools={}, context_id="c", task_id=None)
    assert meta["system_tokens"] <= 700 + 50  # respeita o orçamento configurado
    po.context = {"max_tokens": 20000, "context_paths": [str(doc)]}
    system, _, _ = cx.build(po, query="oi", external_tools={}, context_id="c", task_id=None)
    assert "Visão do produto" in system


def test_audit_detects_tampering(app):
    from flow import audit
    from flow.models import AuditEvent

    for i in range(3):
        audit.record(workflow_id="w", actor="a", event_type="t", payload={"i": i})
    assert audit.verify_chain()[0]
    ev = db.session.query(AuditEvent).order_by(AuditEvent.id).all()[1]
    ev.payload = {"i": 999}
    db.session.commit()
    ok, bad, _ = audit.verify_chain()
    assert not ok and bad == ev.id
