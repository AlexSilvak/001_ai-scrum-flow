"""Compatibilidade com o projeto `flow` original (Open WebUI → /api/mcp/execute/).

Mantém o fluxo heurística → (fallback LLM Ollama) → RBAC → validação → execução → auditoria,
agora gravando na mesma trilha de auditoria (PostgreSQL) dos agentes.
"""
import json
import re
import uuid

from flask import Blueprint, jsonify, request

from .. import audit, llm
from ..domain.permissoes import conceder_permissao, revogar_permissao, verificar_autoridade
from ..domain.usuarios import criar_usuario
from ..models import AuditEvent
from ..extensions import db

bp = Blueprint("gateway", __name__, url_prefix="/api/mcp")

PERFIS = ["financeiro", "rh", "fiscal", "ti", "compras"]
INTENTS = ("criar_usuario", "conceder_permissao", "revogar_permissao")


class ValidationError(Exception):
    pass


def detect_intent(msg: str) -> str:
    m = msg.lower()
    if "cadastrar" in m or "criar usuário" in m or "criar usuario" in m:
        return "criar_usuario"
    if "conceder" in m or "liberar acesso" in m:
        return "conceder_permissao"
    if "revogar" in m or "remover acesso" in m:
        return "revogar_permissao"
    return "desconhecida"


def extract_entities(msg: str) -> dict:
    ent = {}
    for perfil in PERFIS:
        if re.search(rf"\b{perfil}\b", msg, re.I):
            ent["perfil"] = perfil.capitalize()
            break
    else:
        if re.search(r"\b(no|na|em|para)\s+\w+", msg, re.I):
            ent["_perfil_invalido"] = True
    texto = re.sub(rf"\b(no|na|em|para|como|de)\s+({'|'.join(PERFIS)})\b", "", msg, flags=re.I)
    for pat in (r"cadastrar\s+(.+)$", r"para\s+([A-ZÁÉÍÓÚÂÊÔÃÕ][^,]+)$", r"de\s+([A-ZÁÉÍÓÚÂÊÔÃÕ][^,]+)$"):
        if m := re.search(pat, texto, re.I):
            ent["nome"] = m.group(1).strip()
            break
    return ent


def llm_extract(message: str) -> dict:
    prompt = ("Extraia INTENT e ENTIDADES. Responda só JSON: "
              '{"intent":"criar_usuario|conceder_permissao|revogar_permissao|desconhecida","entities":{"nome":"","perfil":""}}\n'
              f"Texto: {message}")
    try:
        raw, _ = llm.chat([{"role": "user", "content": prompt}], llm.LLMConfig("ollama", "gemma3:4b"))
        return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001
        return {"intent": "desconhecida", "entities": {}}


def run(message: str, user: str, context: dict) -> dict:
    wf = str(uuid.uuid4())
    intent, ent = detect_intent(message), extract_entities(message)
    try:
        if intent == "desconhecida" or not ent.get("nome"):
            r = llm_extract(message)
            intent = r.get("intent") or intent
            ent.update({k: v for k, v in (r.get("entities") or {}).items() if v})
        if not verificar_autoridade(user, intent):
            raise ValidationError(f"Acesso Negado: o usuário {user} não tem permissão para '{intent}'")
        if intent not in INTENTS:
            raise ValidationError("Intenção não reconhecida")
        if not ent.get("nome"):
            raise ValidationError("Nome é obrigatório")
        if not ent.get("perfil") or ent.get("_perfil_invalido"):
            raise ValidationError("Perfil é obrigatório e deve ser válido")
        fn = {"criar_usuario": criar_usuario, "conceder_permissao": conceder_permissao, "revogar_permissao": revogar_permissao}[intent]
        result = fn(ent["nome"], ent["perfil"])
        audit.record(workflow_id=wf, actor=f"human:{user}", event_type="gateway_exec", target=intent,
                     payload={"entities": ent, "result": result, **context})
        return {"workflow_id": wf, "intent": intent, "entities": ent, "action_result": result, "status": "executado"}
    except ValidationError as e:
        audit.record(workflow_id=wf, actor=f"human:{user}", event_type="gateway_exec", status="denied", target=intent,
                     payload={"entities": ent, "error": str(e), **context})
        raise


@bp.post("/execute/")
def execute():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Mensagem vazia"), 400
    user = request.headers.get("X-User-Email") or payload.get("user_id") or payload.get("user_email") or "anonymous"
    try:
        res = run(message, user, {"source": "open_webui", "ip": request.remote_addr})
        return jsonify(ok=True, result=res)
    except ValidationError as e:
        return jsonify(ok=False, error={"type": "validation_error", "message": str(e)}), 422


@bp.get("/audit/json/")
def audit_json():
    evs = db.session.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(50).all()
    return jsonify([e.to_dict() for e in evs])
