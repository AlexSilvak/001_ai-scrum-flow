"""Servidor MCP do AI Scrum Flow (fastmcp).

Expõe as MESMAS tools do registro interno (backlog, memória, skills, workspace, auditoria, delegação A2A…),
executadas em nome de um agente (`FLOW_MCP_AGENT`, padrão product-owner) — ou seja, a allowlist e as travas de
papel Scrum do agente valem também para quem chama via MCP (Claude Desktop, Open WebUI, outros agentes).

  uv run flow-mcp                              # stdio (Claude Desktop)
  uv run flow-mcp --transport http --port 9000 # HTTP: http://localhost:9000/mcp
"""
import argparse
import inspect
import os
import typing

from fastmcp import FastMCP

from . import a2a, audit
from . import tools as toolreg
from .extensions import db
from .models import AgentConfig, AuditEvent, Sprint


def _acting_agent() -> AgentConfig:
    slug = os.environ.get("FLOW_MCP_AGENT", "product-owner")
    agent = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not agent:
        raise toolreg.ToolError(f"agente '{slug}' não existe (FLOW_MCP_AGENT)")
    return agent


def _wrap(t: toolreg.Tool, app):
    """Cria função com a assinatura da tool (sem `ctx`) que roda dentro do app context."""
    sig = inspect.signature(t.fn)
    params = list(sig.parameters.values())[1:]
    hints = typing.get_type_hints(t.fn)

    def wrapper(**kwargs):
        with app.app_context():
            try:
                ctx = toolreg.ToolContext(agent=_acting_agent(), workflow_id="mcp", user_email=os.environ.get("FLOW_MCP_USER", "mcp-client"))
                audit.record(workflow_id="mcp", actor=ctx.agent.slug, event_type="mcp_request", target=t.name, payload={"args": kwargs})
                return toolreg.call(t.name, ctx, {k: v for k, v in kwargs.items() if v is not None})
            finally:
                db.session.remove()

    wrapper.__name__, wrapper.__doc__ = t.name, t.description
    wrapper.__signature__ = inspect.Signature([p.replace(annotation=hints.get(p.name, str)) for p in params])
    wrapper.__annotations__ = {p.name: hints.get(p.name, str) for p in params}
    return wrapper


def build_server(app) -> FastMCP:
    mcp = FastMCP(name="AI Scrum Flow", instructions="Time Scrum de agentes: backlog, sprints, memória, skills, auditoria e A2A.")
    for t in toolreg.REGISTRY.values():
        mcp.tool(_wrap(t, app), name=t.name)

    @mcp.tool()
    def flow_status() -> dict:
        """Resumo do time: agentes, sprint atual e total de eventos auditados."""
        with app.app_context():
            try:
                sp = db.session.query(Sprint).order_by(Sprint.id.desc()).first()
                return {"agents": [{"slug": a.slug, "role": a.scrum_role, "model": f"{a.provider}:{a.model}", "enabled": a.enabled}
                                   for a in db.session.query(AgentConfig).order_by(AgentConfig.id)],
                        "sprint": sp and {"id": sp.id, "name": sp.name, "status": sp.status, "goal": sp.goal},
                        "audit_events": db.session.query(AuditEvent).count()}
            finally:
                db.session.remove()

    @mcp.tool()
    def ask_product_owner(message: str, context_id: str = "") -> str:
        """Fala com o time pela porta de entrada (Product Owner) via A2A; devolve a resposta."""
        with app.app_context():
            try:
                task = a2a.send_message("system", "product-owner", message, context_id=context_id or None, enforce=False,
                                        ctx=toolreg.ToolContext(agent=db.session.query(AgentConfig).filter_by(slug="product-owner").first(), workflow_id="mcp"))
                return a2a.reply_text(task)
            finally:
                db.session.remove()

    @mcp.tool()
    def consultar_auditoria(nome_usuario: str = "", limit: int = 5) -> str:
        """Últimos eventos de auditoria (compatível com o server.py original); filtra por ator/alvo."""
        with app.app_context():
            try:
                evs = db.session.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(200).all()
                if nome_usuario:
                    evs = [e for e in evs if nome_usuario.lower() in (e.actor + e.target + str(e.payload)).lower()]
                return "\n".join(f"#{e.id} [{e.created_at:%Y-%m-%d %H:%M}] {e.actor} {e.event_type} {e.target} ({e.status})"
                                 for e in evs[:limit]) or "Nenhum registro de auditoria encontrado."
            finally:
                db.session.remove()

    @mcp.tool()
    def ajustar_permissao_colaborador(comando_natural: str, usuario: str = "") -> str:
        """Executa comando de permissão em linguagem natural (RBAC + auditoria), como no projeto original."""
        from .web.gateway import ValidationError, run

        with app.app_context():
            try:
                res = run(comando_natural, usuario or os.environ.get("FLOW_MCP_USER", "anonymous"), {"source": "mcp"})
                a = res["action_result"]
                return f"✅ {a.get('acao', 'ok')}: {a.get('usuario') or a.get('nome')} ({a.get('perfil')})"
            except ValidationError as e:
                return f"❌ {e}"
            finally:
                db.session.remove()

    return mcp


def main():
    from . import create_app

    ap = argparse.ArgumentParser(description="Servidor MCP do AI Scrum Flow")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()
    server = build_server(create_app())
    if args.transport == "http":
        server.run(transport="http", host=args.host, port=args.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
