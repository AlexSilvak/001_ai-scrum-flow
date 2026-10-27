"""AI Scrum Flow — time Scrum de agentes (Flask)."""
from pathlib import Path

from flask import Flask
from .config import Config
from .extensions import db


def create_app(overrides: dict | None = None) -> Flask:
    cfg = Config().as_flask()
    cfg.update(overrides or {})
    app = Flask(__name__)
    app.config.update(cfg)
    if not str(app.config["SQLALCHEMY_DATABASE_URI"]).startswith("postgresql"):
        raise RuntimeError("AI Scrum Flow usa somente PostgreSQL. Defina DATABASE_URL=postgresql://usuario:senha@host:5432/banco "
                           "(ou [database] url no flow.toml). Veja o README, seção “Banco”.")
    app.config["FLOW_WORKSPACE"] = Path(app.config["FLOW_WORKSPACE"])
    app.config["FLOW_WORKSPACE"].mkdir(parents=True, exist_ok=True)
    db.init_app(app)

    from . import models  # noqa: F401 — registra as tabelas
    from .cli import register_cli
    from .web import register_blueprints

    from . import queue

    queue.bind(app)
    with app.app_context():
        db.create_all()
        queue.ensure_schema()
        try:
            from .seed import ensure_required_tools
            ensure_required_tools()
        except Exception:  # noqa: BLE001 — banco ainda sem seed/tabelas: nada a corrigir
            db.session.rollback()
    queue.start_embedded_on_first_request(app)
    register_blueprints(app)
    register_cli(app)
    return app
