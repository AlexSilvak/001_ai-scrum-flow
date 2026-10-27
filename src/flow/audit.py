"""API da auditoria: `record()` encadeia hashes; `verify_chain()` detecta adulteração."""
import hashlib
import json
import threading

from sqlalchemy import text

from .extensions import db
from .models import AuditEvent, now

_lock = threading.Lock()


def _canon(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _trunc(v, limit=4000):
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"…[+{len(v) - limit} chars]"
    if isinstance(v, dict):
        return {k: _trunc(x, limit) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_trunc(x, limit) for x in v[:100]]
    return v


def compute_hash(ev: AuditEvent) -> str:
    ts = ev.created_at.replace(tzinfo=None).isoformat() if ev.created_at else ""
    body = _canon({"prev": ev.prev_hash, "workflow_id": ev.workflow_id, "actor": ev.actor, "event_type": ev.event_type,
                   "status": ev.status, "target": ev.target, "payload": ev.payload, "created_at": ts})
    return hashlib.sha256(body.encode()).hexdigest()


def record(*, workflow_id, actor, event_type, status="ok", target="", payload=None) -> AuditEvent:
    with _lock:
        db.session.execute(text("SELECT pg_advisory_xact_lock(7001)"))  # serializa a cadeia entre processos (API + worker)
        last = db.session.query(AuditEvent).order_by(AuditEvent.id.desc()).first()
        ev = AuditEvent(workflow_id=str(workflow_id), actor=actor, event_type=event_type, status=status,
                        target=str(target)[:200], payload=_trunc(json.loads(_canon(payload or {}))),
                        prev_hash=last.hash if last else "", created_at=now())
        ev.hash = compute_hash(ev)
        db.session.add(ev)
        db.session.commit()
        return ev


def verify_chain():
    """(ok, id_do_primeiro_evento_inválido | None, total)."""
    prev, total = "", 0
    for ev in db.session.query(AuditEvent).order_by(AuditEvent.id).yield_per(500):
        total += 1
        if ev.prev_hash != prev or compute_hash(ev) != ev.hash:
            return False, ev.id, total
        prev = ev.hash
    return True, None, total
