"""Configuração via variáveis de ambiente (ou arquivo .env). Somente PostgreSQL."""
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path = ROOT / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def normalize_db_url(url: str) -> str:
    """postgres://… e postgresql://… → driver psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def load_toml(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("FLOW_CONFIG", ROOT / "flow.toml"))
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _abs(p: str) -> str:
    return str(p if Path(p).is_absolute() else ROOT / p)


class Config:
    """Precedência: variável de ambiente > .env > flow.toml > padrão."""

    def __init__(self):
        load_dotenv()
        t = load_toml()
        T = lambda sec, key, default="": t.get(sec, {}).get(key, default)  # noqa: E731
        env = lambda name, default="": os.environ.get(name, default)  # noqa: E731
        self.SECRET_KEY = env("SECRET_KEY", "dev-only-change-me")
        self.SQLALCHEMY_DATABASE_URI = normalize_db_url(env("DATABASE_URL", T("database", "url", "postgresql://flow:flow@localhost:5432/flow")))
        self.SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
        self.FLOW_WORKSPACE = Path(_abs(env("FLOW_WORKSPACE", T("paths", "workspace", "workspace"))))
        self.FLOW_SKILLS_PATH = _abs(env("FLOW_SKILLS_PATH", T("paths", "skills_path", "skills")))
        self.FLOW_TEAM_FILE = _abs(env("FLOW_TEAM_FILE", T("paths", "team", "team.toml")))
        self.FLOW_PUBLIC_URL = env("FLOW_PUBLIC_URL", T("a2a", "public_url", "http://localhost:5000"))
        self.A2A_BEARER_TOKEN = env("A2A_BEARER_TOKEN", T("a2a", "bearer_token", ""))
        self.OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", T("llm", "ollama_base_url", "http://localhost:11434"))
        self.FLOW_DEFAULT_PROVIDER = env("FLOW_DEFAULT_PROVIDER", T("llm", "default_provider", "ollama"))
        self.FLOW_DEFAULT_MODEL = env("FLOW_DEFAULT_MODEL", T("llm", "default_model", "gemma3:4b"))
        flag = lambda v: str(v).strip().lower() in ("1", "true", "yes", "sim", "on")  # noqa: E731
        self.FLOW_GIT_ENABLED = flag(env("FLOW_GIT_ENABLED", T("git", "enabled", True)))
        self.FLOW_CD_ENABLED = flag(env("FLOW_CD_ENABLED", T("cd", "enabled", True)))
        self.FLOW_CD_AUTO_DEPLOY = flag(env("FLOW_CD_AUTO_DEPLOY", T("cd", "auto_deploy", False)))  # False = o humano aprova a subida
        allowed = env("FLOW_CD_ALLOWED", "") or T("cd", "allowed_commands", ["python", "python3", "pip", "uv", "pytest", "node", "npm", "npx", "flask", "gunicorn", "uvicorn"])
        self.FLOW_CD_ALLOWED = [x.strip() for x in (allowed.split(",") if isinstance(allowed, str) else allowed) if x.strip()]
        self.FLOW_CD_PORT_RANGE = tuple(T("cd", "port_range", [8100, 8199]))
        self.FLOW_CD_START_TIMEOUT = int(env("FLOW_CD_START_TIMEOUT", T("cd", "start_timeout_s", 25)))
        self.FLOW_CD_STEP_TIMEOUT = int(env("FLOW_CD_STEP_TIMEOUT", T("cd", "step_timeout_s", 300)))
        self.FLOW_SANDBOX_ENABLED = flag(env("FLOW_SANDBOX_ENABLED", T("sandbox", "enabled", True)))
        self.FLOW_SANDBOX_MODE = env("FLOW_SANDBOX_MODE", T("sandbox", "mode", "auto"))  # auto | docker | local
        self.FLOW_SANDBOX_ALLOW_LOCAL = flag(env("FLOW_SANDBOX_ALLOW_LOCAL", T("sandbox", "allow_local_fallback", True)))
        self.FLOW_SANDBOX_REQUIRE_CODE = flag(env("FLOW_SANDBOX_REQUIRE_CODE", T("sandbox", "require_code", True)))
        self.FLOW_SANDBOX_IMAGE = env("FLOW_SANDBOX_IMAGE", T("sandbox", "image", "flow-sandbox:1"))
        self.FLOW_SANDBOX_MEMORY = env("FLOW_SANDBOX_MEMORY", T("sandbox", "memory", "512m"))
        self.FLOW_SANDBOX_CPUS = env("FLOW_SANDBOX_CPUS", T("sandbox", "cpus", "1"))
        self.FLOW_SANDBOX_PIDS = int(env("FLOW_SANDBOX_PIDS", T("sandbox", "pids", 128)))
        self.FLOW_SANDBOX_TIMEOUT = int(env("FLOW_SANDBOX_TIMEOUT", T("sandbox", "test_timeout_s", 120)))
        self.FLOW_SANDBOX_INSTALL_TIMEOUT = int(env("FLOW_SANDBOX_INSTALL_TIMEOUT", T("sandbox", "install_timeout_s", 300)))
        self.FLOW_SANDBOX_E2E_IMAGE = env("FLOW_SANDBOX_E2E_IMAGE", T("sandbox", "e2e_image", "flow-sandbox-e2e:1"))
        self.FLOW_SANDBOX_E2E_MEMORY = env("FLOW_SANDBOX_E2E_MEMORY", T("sandbox", "e2e_memory", "1g"))
        self.FLOW_SANDBOX_E2E_CPUS = env("FLOW_SANDBOX_E2E_CPUS", T("sandbox", "e2e_cpus", "2"))
        self.FLOW_SANDBOX_E2E_PIDS = int(env("FLOW_SANDBOX_E2E_PIDS", T("sandbox", "e2e_pids", 512)))
        self.FLOW_SANDBOX_E2E_TIMEOUT = int(env("FLOW_SANDBOX_E2E_TIMEOUT", T("sandbox", "e2e_timeout_s", 180)))
        self.FLOW_SANDBOX_ALLOWED_PACKAGES = T("sandbox", "allowed_packages", ["flask", "pytest", "httpx", "requests", "jinja2", "werkzeug", "itsdangerous", "markupsafe", "click",
                                                                               "flask-sqlalchemy", "sqlalchemy", "flask-login", "flask-wtf", "wtforms", "email-validator", "bcrypt",
                                                                               "pydantic", "python-dotenv", "pytest-flask", "playwright", "pytest-playwright"])
        self.FLOW_EMBEDDED_WORKER = flag(env("FLOW_EMBEDDED_WORKER", T("worker", "embedded", True)))  # False = rode `flask --app flow worker` à parte
        self.FLOW_WORKER_CONCURRENCY = int(env("FLOW_WORKER_CONCURRENCY", T("worker", "concurrency", 4)))
        self.ROOT = ROOT

    def as_flask(self):
        return {k: v for k, v in vars(self).items() if k.isupper()}
