"""Endpoints A2A: Agent Card + JSON-RPC por agente (e card do PO como "porta de entrada" do time)."""
import hmac

from flask import Blueprint, current_app, jsonify, request

from .. import a2a
from ..extensions import db
from ..models import AgentConfig, Role

bp = Blueprint("a2a", __name__)


def _authorized() -> bool:
    token = current_app.config["A2A_BEARER_TOKEN"]
    if not token:
        return True
    got = request.headers.get("Authorization", "")
    return got.startswith("Bearer ") and hmac.compare_digest(got[7:], token)


def _card_response(agent):
    resp = jsonify(a2a.agent_card(agent))
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@bp.get("/.well-known/agent-card.json")
def team_card():
    """O time é visto de fora como UM agente: o Product Owner (orquestrador)."""
    po = db.session.query(AgentConfig).filter_by(scrum_role=Role.PO.value, enabled=True).first()
    if not po:
        return jsonify({"error": "sem Product Owner; rode `flask --app flow seed`"}), 404
    return _card_response(po)


@bp.get("/a2a/<slug>/.well-known/agent-card.json")
def agent_card(slug):
    agent = db.session.query(AgentConfig).filter_by(slug=slug).first()
    if not agent:
        return jsonify({"error": "agente não encontrado"}), 404
    return _card_response(agent)


@bp.post("/a2a/<slug>/")
def rpc(slug):
    if not _authorized():
        return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Unauthorized"}}), 401
    body = request.get_json(silent=True)
    if body is None:
        return jsonify(a2a._error(None, "parse")), 400
    return jsonify(a2a.handle_jsonrpc(slug, body, request.headers.get("A2A-Version", "")))
