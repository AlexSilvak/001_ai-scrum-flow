import asyncio
import threading

import httpx
import pytest
from werkzeug.serving import make_server

from flow import a2a
from flow.extensions import db
from flow.models import AgentConfig, AuditEvent


def rpc(client, slug, method, params=None, headers=None):
    r = client.post(f"/a2a/{slug}/", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers or {})
    return r.get_json()


def msg(text, **kw):
    return {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}], **kw}}


# ------------------------------------------------------------------ A2A
def test_agent_card_follows_spec_1_0(client):
    card = client.get("/a2a/qa/.well-known/agent-card.json").get_json()
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC" and card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a/qa/")
    assert {"name", "description", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills"} <= set(card)
    assert any(s["id"] == "skill-definition-of-done" for s in card["skills"])
    front = client.get("/.well-known/agent-card.json").get_json()  # porta de entrada do time = PO
    assert front["name"] == "Product Owner"


def test_send_get_list_cancel(client):
    r = rpc(client, "product-owner", "SendMessage", msg("olá time"))
    task = r["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and task["artifacts"][0]["parts"][0]["text"]
    assert rpc(client, "product-owner", "GetTask", {"id": task["id"]})["result"]["id"] == task["id"]
    lst = rpc(client, "product-owner", "ListTasks", {"contextId": task["contextId"]})["result"]
    assert lst["totalSize"] == 1
    assert rpc(client, "product-owner", "CancelTask", {"id": task["id"]})["error"]["code"] == -32002  # já terminal
    assert rpc(client, "product-owner", "GetTask", {"id": "nope"})["error"]["code"] == -32001
    assert rpc(client, "product-owner", "Nope")["error"]["code"] == -32601
    assert rpc(client, "product-owner", "SendMessage", {"message": {"role": "ROLE_USER", "parts": []}})["error"]["code"] == -32602
    assert rpc(client, "product-owner", "GetExtendedAgentCard")["error"]["code"] == -32007
    assert rpc(client, "product-owner", "SendMessage", msg("x"), headers={"A2A-Version": "9.0"})["error"]["code"] == -32009
    # alias do protocolo 0.3
    assert rpc(client, "product-owner", "message/send", {"message": {"role": "user", "parts": [{"kind": "text", "text": "oi"}]}})["result"]["task"]


def test_data_part_drives_ceremony_and_context_continues(client, app):
    r = rpc(client, "product-owner", "SendMessage", {"message": {"messageId": "x", "role": "ROLE_USER", "parts": [
        {"text": "refine"}, {"data": {"ceremony": "refine", "request": "Login; Relatórios"}}]}})
    assert r["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    from flow.models import WorkItem
    assert db.session.query(WorkItem).count() == 2


def test_bearer_token_enforced(app, client):
    app.config["A2A_BEARER_TOKEN"] = "segredo"
    assert client.post("/a2a/qa/", json={}).status_code == 401
    ok = client.post("/a2a/qa/", json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks"}, headers={"Authorization": "Bearer segredo"})
    assert ok.status_code == 200 and "result" in ok.get_json()


def test_remote_agent_over_real_http(app):
    """Um agente do time apontando para a_url de OUTRO servidor A2A: descoberta pelo card + JSON-RPC via HTTP."""
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    port = srv.server_port
    app.config["FLOW_PUBLIC_URL"] = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        qa = db.session.query(AgentConfig).filter_by(slug="qa").first()
        qa.a2a_url = f"http://127.0.0.1:{port}/a2a/qa"
        db.session.commit()
        task = a2a.send_message("scrum-master", "qa", "ping remoto")
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert db.session.query(AuditEvent).filter_by(event_type="a2a_send", actor="scrum-master").count() >= 1
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ API de configuração dinâmica
def test_agent_config_patch_and_validation(client):
    ok = client.patch("/api/agents/qa", json={"provider": "ollama", "model": "llama3.2:3b", "temperature": 0.7, "max_steps": 3,
                                              "tools": ["read_workspace_file"], "context": {"max_tokens": 3000}})
    assert ok.status_code == 200 and ok.get_json()["model"] == "llama3.2:3b" and ok.get_json()["context"]["max_tokens"] == 3000
    assert db.session.query(AuditEvent).filter_by(event_type="agent_config_change", target="qa").count() == 1
    for bad in ({"provider": "skynet"}, {"tools": ["rm_rf"]}, {"skills_path": "/etc"}, {"context": {"context_paths": ["/etc/passwd"]}},
                {"mcp_servers": {"x": {"command": "bash"}}}, {"can_delegate_to": ["ninguem"]}, {"slug": "hack"}, {"max_steps": 0}):
        assert client.patch("/api/agents/qa", json=bad).status_code in (400, 422), bad


def test_api_token_protects_api(client, monkeypatch):
    monkeypatch.setenv("FLOW_API_TOKEN", "t0k")
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer t0k"}).status_code == 200
    assert client.get("/api/health").status_code == 200


def test_dashboard_and_state(client):
    assert b"AI SCRUM FLOW" in client.get("/").data
    s = client.get("/api/state").get_json()
    assert len(s["agents"]) == 7 and "audit_events" in s and "deployments" in s
    assert client.get("/static/css/app.css").status_code == 200


# ------------------------------------------------------------------ MCP
def test_mcp_server_exposes_registry_under_agent_permissions(app):
    from fastmcp import Client

    from flow.mcp_server import build_server

    async def go():
        async with Client(build_server(app)) as c:
            names = {t.name for t in await c.list_tools()}
            assert {"backlog_list", "memory_save", "delegate_to_agent", "flow_status", "ask_product_owner"} <= names
            ok = await c.call_tool("memory_save", {"content": "via mcp", "scope": "shared"})
            assert ok.data["memory_id"]
            with pytest.raises(Exception, match="restrita|habilitada"):
                await c.call_tool("work_item_verdict", {"item_id": 1, "passed": True})  # PO não tem essa tool
            reply = await c.call_tool("ask_product_owner", {"message": "oi"})
            assert reply.data

    asyncio.run(go())


def test_agent_uses_external_mcp_server(app, monkeypatch):
    """Agente chama tool de um servidor MCP externo (allowlist `srv.*`)."""
    from fastmcp import Client, FastMCP

    from flow import llm, mcp_client, runtime
    from flow.tools import ToolContext

    ext = FastMCP("ext")

    @ext.tool
    def clima(cidade: str) -> str:
        """Clima fictício."""
        return f"sol em {cidade}"

    monkeypatch.setattr(mcp_client, "_client_for", lambda cfg: Client(ext))
    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    po.mcp_servers, po.tools = {"meteo": {"url": "http://fake/mcp"}}, [*po.tools, "meteo.*"]
    assert "meteo.clima" in mcp_client.list_tools(po.mcp_servers)
    assert mcp_client.allowed(po.tools, "meteo.clima") and not mcp_client.allowed(["x.*"], "meteo.clima")

    script = iter(['{"tool": "meteo.clima", "args": {"cidade": "Recife"}}'])
    seen = []

    def scripted(messages, cfg):
        seen.append(messages[-1]["content"])
        return next(script, '{"final": "ok"}'), {"in": 1, "out": 1}

    monkeypatch.setitem(llm.PROVIDERS, "mock", scripted)
    res = runtime.run(po, "como está o clima?", ctx=ToolContext(agent=po, workflow_id="t"), context_id="c")
    assert res.text == "ok" and "sol em Recife" in seen[-1]  # o resultado MCP voltou ao LLM
    assert db.session.query(AuditEvent).filter_by(event_type="mcp_call", target="meteo.clima").count() == 1
    # sem estar na allowlist, a chamada é recusada
    po.tools = [t for t in po.tools if t != "meteo.*"]
    script2 = iter(['{"tool": "meteo.clima", "args": {"cidade": "X"}}'])
    monkeypatch.setitem(llm.PROVIDERS, "mock", lambda m, c: (next(script2, '{"final": "fim"}'), {}))
    runtime.run(po, "de novo", ctx=ToolContext(agent=po, workflow_id="t"), context_id="c2")
    assert db.session.query(AuditEvent).filter_by(event_type="mcp_call").count() == 1


# ------------------------------------------------------------------ Ollama + config TOML
def test_ollama_provider_and_model_listing(app, monkeypatch):
    from flow import llm, runtime
    from flow.tools import ToolContext

    calls = []

    import contextlib
    import json as _json

    @contextlib.contextmanager
    def fake_stream(method, url, **kw):  # o runtime usa streaming (o painel mostra o que o modelo gera ao vivo)
        calls.append((url, kw["json"]["model"], kw["json"]["stream"]))
        lines = [_json.dumps({"message": {"content": '{"final": "resposta '}, "done": False}), _json.dumps({"message": {"content": 'do ollama"}'}, "done": False}),
                 _json.dumps({"message": {"content": ""}, "done": True, "prompt_eval_count": 11, "eval_count": 5})]
        yield httpx.Response(200, content="\n".join(lines).encode(), request=httpx.Request(method, url))

    monkeypatch.setattr(llm.httpx, "stream", fake_stream)
    monkeypatch.setattr(llm.httpx, "get", lambda url, **kw: httpx.Response(200, json={"models": [{"name": "gemma3:4b"}, {"name": "llama3.2:3b"}]}, request=httpx.Request("GET", url)))
    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    po.provider, po.model = "ollama", "llama3.2:3b"
    res = runtime.run(po, "oi", ctx=ToolContext(agent=po, workflow_id="t"), context_id="c")
    assert res.text == "resposta do ollama" and (res.tokens_in, res.tokens_out) == (11, 5)
    assert calls[0] == ("http://localhost:11434/api/chat", "llama3.2:3b", True)
    from flow.models import AgentTrace
    think = db.session.query(AgentTrace).filter_by(kind="think").first()
    assert think.body == '{"final": "resposta do ollama"}' and think.state == "ok" and think.meta["tokens_out"] == 5
    assert llm.list_ollama_models() == ["gemma3:4b", "llama3.2:3b"]


def test_ollama_down_fails_task_cleanly(app):
    from flow import llm, runtime
    from flow.tools import ToolContext

    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    po.provider, po.base_url = "ollama", "http://127.0.0.1:9"
    res = runtime.run(po, "oi", ctx=ToolContext(agent=po, workflow_id="t"), context_id="c")
    assert not res.ok and "Ollama indisponível" in res.text


def test_config_precedence_env_over_toml(tmp_path, monkeypatch):
    from flow.config import Config

    f = tmp_path / "f.toml"
    f.write_text('[llm]\ndefault_model = "do-toml"\ndefault_provider = "ollama"\n[database]\nurl = "postgres://u:p@h/db"\n')
    monkeypatch.setenv("FLOW_CONFIG", str(f))
    monkeypatch.delenv("FLOW_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = Config()
    assert c.FLOW_DEFAULT_MODEL == "do-toml" and c.SQLALCHEMY_DATABASE_URI == "postgresql+psycopg://u:p@h/db"
    monkeypatch.setenv("FLOW_DEFAULT_MODEL", "da-env")
    assert Config().FLOW_DEFAULT_MODEL == "da-env"


def test_run_blocked_while_po_refines_or_backlog_empty(client):
    from flow import queue

    sid = client.post("/api/sprints", json={"name": "S"}).get_json()["id"]
    assert client.post(f"/api/sprints/{sid}/run").status_code == 422  # backlog vazio
    queue.enqueue("refine", sprint_id=sid, request="x")  # o PO ainda está refinando = job de refinamento na fila
    r = client.post(f"/api/sprints/{sid}/run")
    assert r.status_code == 409 and "refinando" in r.get_json()["error"]
    assert client.get("/api/state").get_json()["sprint"]["refining"] is True


def test_dashboard_assets_are_versioned_and_js_handles_401(client):
    html = client.get("/").data.decode()
    assert "app.js?v=" in html and "app.css?v=" in html
    js = client.get("/static/js/app.js").data.decode()
    assert "askToken" in js and "flowApiToken" in js


def test_ceremony_rejects_free_text_and_premature_final(app, monkeypatch):
    """Regressão (gemma3:4b real): PO respondeu em texto livre/“final” sem criar histórias → backlog vazio."""
    from flow import llm, runtime
    from flow.models import WorkItem
    from flow.tools import ToolContext

    replies = iter([
        "Ok, vamos refinar este pedido. Criarei algumas user stories.",          # texto livre → deve ser rejeitado
        '{"final": "pronto"}',                                                   # final sem agir → deve ser rejeitado
        '{"tool": "backlog_create", "args": {"title": "Login", "points": 3, "acceptance_criteria": ["a"]}}',
        '{"final": "criei 1 história"}',
    ])
    monkeypatch.setitem(llm.PROVIDERS, "mock", lambda m, c: (next(replies), {"in": 1, "out": 1}))
    po = db.session.query(AgentConfig).filter_by(slug="product-owner").first()
    task = {"ceremony": "refine", "request": "login", "requires": ["backlog_create"]}
    res = runtime.run(po, task, ctx=ToolContext(agent=po, workflow_id="t"), context_id="c")
    assert res.ok and res.tool_calls == ["backlog_create"] and res.steps == 4
    assert db.session.query(WorkItem).count() == 1


def test_refine_without_stories_surfaces_error(app, monkeypatch):
    from flow import llm
    from flow.models import Sprint
    from flow.scrum import SprintRunner

    monkeypatch.setitem(llm.PROVIDERS, "mock", lambda m, c: ("só conversa, sem json", {"in": 1, "out": 1}))
    sp = Sprint(name="S")
    db.session.add(sp)
    db.session.commit()
    with pytest.raises(RuntimeError, match="não criou nenhuma user story"):
        SprintRunner(sp).refine("login")
