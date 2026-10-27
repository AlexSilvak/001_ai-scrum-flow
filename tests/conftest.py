import os

import pytest
from sqlalchemy import create_engine, text

from flow import create_app
from flow.extensions import db
from flow.seed import seed

# Os testes rodam no PostgreSQL (o banco é recriado a cada teste; a fila procrastinate é esvaziada).
# Padrão: o cluster de teste local. Outro banco: TEST_DATABASE_URL=postgresql://user:pw@host/db
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://flow:flow@127.0.0.1:5433/flow_test")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_API_TOKEN", raising=False)
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": TEST_DB.replace("postgresql://", "postgresql+psycopg://", 1),
        "FLOW_WORKSPACE": tmp_path / "ws",
        "FLOW_DEFAULT_PROVIDER": "mock",
        "FLOW_EMBEDDED_WORKER": False,
        "A2A_BEARER_TOKEN": "",
    })
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.execute(text("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE"))
        db.session.commit()
        seed()
        yield app
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()
