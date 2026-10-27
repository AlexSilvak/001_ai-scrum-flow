"""Sandbox de testes do QA: pytest dentro de um container descartável (uv), sem rede e com limites.

Fluxo de `run(item)`:
  1. snapshot  — copia a pasta da história para uma pasta temporária (o código do LLM nunca é executado "no lugar").
  2. instalar  — só se a história declara dependências: valida contra a allowlist `[sandbox].allowed_packages`
                 e instala com `uv pip install` (ÚNICA etapa com rede). Pacote fora da lista bloqueia a execução.
  3. testar    — `uv run … pytest` com `--network none`, raiz somente leitura, memória/CPU/pids limitados, sem capabilities.
  4. relatório — exit code + saída do pytest; vira evidência do veredito do QA e evento `sandbox_run` na auditoria.

Sem Docker (mode = "auto"), cai para um subprocesso local — NÃO isolado — e o relatório diz isso.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from flask import current_app

from . import audit

UV_VERSION = "0.11.32"
DOCKERFILE = f"""FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_NO_CACHE_DIR=1
RUN pip install uv=={UV_VERSION} \\
 && uv venv /opt/venv \\
 && uv pip install --python /opt/venv/bin/python flask pytest httpx
RUN useradd -m -u 1000 sbx
USER sbx
WORKDIR /work
"""
PLUGIN_B64 = "IiIiUGx1Z2luIHB5dGVzdCBkbyBzYW5kYm94IGRvIEFJIFNjcnVtIEZsb3cgKG7Do28gZWRpdGFyKTogY2hyb21pdW0gc2VtIHNhbmRib3ggZGUgU08gKG8gY29udGFpbmVyIGrDoSBpc29sYSkgZSB1bSBmaXh0dXJlIHBhcmEgc3ViaXIgbyBhcHAgdGVzdGFkby4iIiIKaW1wb3J0IG9zCmltcG9ydCBzb2NrZXQKaW1wb3J0IHN1YnByb2Nlc3MKaW1wb3J0IHN5cwppbXBvcnQgdGltZQppbXBvcnQgdXJsbGliLnJlcXVlc3QKCmltcG9ydCBweXRlc3QKCgpAcHl0ZXN0LmZpeHR1cmUoc2NvcGU9InNlc3Npb24iKQpkZWYgYnJvd3Nlcl90eXBlX2xhdW5jaF9hcmdzKCk6CiAgICByZXR1cm4geyJoZWFkbGVzcyI6IFRydWUsICJhcmdzIjogWyItLW5vLXNhbmRib3giLCAiLS1kaXNhYmxlLWRldi1zaG0tdXNhZ2UiLCAiLS1kaXNhYmxlLWdwdSJdfQoKCkBweXRlc3QuZml4dHVyZShzY29wZT0ic2Vzc2lvbiIpCmRlZiBzdGFydF9hcHAoKToKICAgICIiInN0YXJ0X2FwcCgiYXBwLnB5IikgLT4gImh0dHA6Ly8xMjcuMC4wLjE6PHBvcnRhPiI7IHNvYmUgYHB5dGhvbiA8YXJncz5gIGNvbSBQT1JUL0hPU1QsIGVzcGVyYSBhIHBvcnRhIGFicmlyIGUgZW5jZXJyYSBubyBmaW0uIiIiCiAgICBwcm9jcyA9IFtdCgogICAgZGVmIHN0YXJ0KCphcmdzLCBoZWFsdGg9Ii8iLCB0aW1lb3V0PTIwKToKICAgICAgICBzID0gc29ja2V0LnNvY2tldCgpOyBzLmJpbmQoKCIxMjcuMC4wLjEiLCAwKSk7IHBvcnQgPSBzLmdldHNvY2tuYW1lKClbMV07IHMuY2xvc2UoKQogICAgICAgIGVudiA9IHsqKm9zLmVudmlyb24sICJQT1JUIjogc3RyKHBvcnQpLCAiSE9TVCI6ICIxMjcuMC4wLjEifQogICAgICAgIHAgPSBzdWJwcm9jZXNzLlBvcGVuKFtzeXMuZXhlY3V0YWJsZSwgKmFyZ3NdLCBlbnY9ZW52LCBzdGRvdXQ9c3VicHJvY2Vzcy5ERVZOVUxMLCBzdGRlcnI9c3VicHJvY2Vzcy5ERVZOVUxMKQogICAgICAgIHByb2NzLmFwcGVuZChwKQogICAgICAgIHVybCwgZW5kID0gZiJodHRwOi8vMTI3LjAuMC4xOntwb3J0fSIsIHRpbWUudGltZSgpICsgdGltZW91dAogICAgICAgIHdoaWxlIHRpbWUudGltZSgpIDwgZW5kOgogICAgICAgICAgICBpZiBwLnBvbGwoKSBpcyBub3QgTm9uZToKICAgICAgICAgICAgICAgIHJhaXNlIFJ1bnRpbWVFcnJvcihmIm8gYXBwIHRlcm1pbm91IChleGl0IHtwLnJldHVybmNvZGV9KSBhbnRlcyBkZSBhYnJpciBhIHBvcnRhIHtwb3J0fSIpCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHVybGxpYi5yZXF1ZXN0LnVybG9wZW4odXJsICsgaGVhbHRoLCB0aW1lb3V0PTEpCiAgICAgICAgICAgICAgICByZXR1cm4gdXJsCiAgICAgICAgICAgIGV4Y2VwdCBPU0Vycm9yIGFzIGU6CiAgICAgICAgICAgICAgICBpZiBnZXRhdHRyKGUsICJjb2RlIiwgTm9uZSk6ICAjIHJlc3BvbmRldSBIVFRQIChtZXNtbyA0eHgvNXh4KSA9IGVzdMOhIGRlIHDDqQogICAgICAgICAgICAgICAgICAgIHJldHVybiB1cmwKICAgICAgICAgICAgICAgIHRpbWUuc2xlZXAoMC4yKQogICAgICAgIHJhaXNlIFJ1bnRpbWVFcnJvcihmIm8gYXBwIG7Do28gYWJyaXUgYSBwb3J0YSB7cG9ydH0gZW0ge3RpbWVvdXR9cyIpCgogICAgeWllbGQgc3RhcnQKICAgIGZvciBwIGluIHByb2NzOgogICAgICAgIHAudGVybWluYXRlKCkK"
DOCKERFILE_E2E = """FROM {base}
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONPATH=/opt/flow
RUN uv pip install --python /opt/venv/bin/python pytest-playwright \\
 && /opt/venv/bin/playwright install --with-deps chromium \\
 && chmod -R a+rX /ms-playwright \\
 && mkdir -p /opt/flow && echo {plugin} | base64 -d > /opt/flow/flow_sbx.py
USER sbx
"""
BASE_LOCAL = ["pytest", "flask", "httpx"]
PRE = {"flask", "pytest", "httpx", "werkzeug", "jinja2", "itsdangerous", "markupsafe", "click", "blinker"}
PRE_E2E = PRE | {"playwright", "pytest-playwright"}
DOC_EXT = {".md", ".txt", ".rst"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".sandbox"}
SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[A-Za-z0-9_,\-]+\])?(\s*[<>=!~]=?\s*[A-Za-z0-9_.*+!\-]+(\s*,\s*[<>=!~]=?\s*[A-Za-z0-9_.*+!\-]+)*)?$")
MAX_FILES, MAX_BYTES = 400, 5_000_000
_DOCKER = {"t": 0.0, "ok": False, "why": ""}


class SandboxError(Exception):
    def __init__(self, msg, fault="env"):
        super().__init__(msg)
        self.fault = fault   # env (ambiente) | dev (código do dev) | qa (teste do QA)


# ------------------------------------------------------------------ configuração
def _c(key, default=None):
    return current_app.config.get(key, default)


def enabled() -> bool:
    return bool(_c("FLOW_SANDBOX_ENABLED", True))


def docker_ok(force=False) -> bool:
    """O daemon responde? (cache de 30 s para não chamar `docker info` a cada ferramenta)."""
    if not force and time.time() - _DOCKER["t"] < 30:
        return _DOCKER["ok"]
    ok, why = False, "comando `docker` não encontrado no PATH de quem roda o worker (Windows x WSL?)"
    if shutil.which("docker"):
        try:
            p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=8, stdin=subprocess.DEVNULL)
            ok = p.returncode == 0
            why = "" if ok else "o daemon não respondeu: " + (p.stderr or p.stdout).decode("utf-8", "replace").strip()[:140]
        except (subprocess.TimeoutExpired, OSError) as e:
            why = f"`docker info` falhou ({type(e).__name__})"
    _DOCKER.update(t=time.time(), ok=ok, why=why)
    return ok


def mode() -> str:
    """docker | local. `auto` usa Docker se o daemon responder; `docker` exige; `local` nunca usa."""
    want = str(_c("FLOW_SANDBOX_MODE", "auto")).lower()
    if want == "local":
        return "local"
    if docker_ok():
        return "docker"
    if want == "docker":
        raise SandboxError("[sandbox].mode = \"docker\", mas o daemon do Docker não responde (abra o Docker Desktop / inicie o serviço)")
    if not _c("FLOW_SANDBOX_ALLOW_LOCAL", True):
        raise SandboxError("Docker indisponível e o fallback local está desligado ([sandbox].allow_local_fallback = false)")
    return "local"


def docker_why() -> str:
    return _DOCKER.get("why", "")


# ------------------------------------------------------------------ a história no disco
def story_dir(item) -> Path:
    return Path(_c("FLOW_WORKSPACE")).resolve() / item.sprint.workflow_id / item.key


def _walk(base: Path):
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if p.is_symlink() or any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        yield p, rel


def code_files(item) -> list[str]:
    """Arquivos que contam como implementação: tudo que não é documento, manifesto ou teste."""
    base = story_dir(item)
    if not base.is_dir():
        return []
    out = []
    for _, rel in _walk(base):
        if rel.suffix.lower() in DOC_EXT or rel.name == "flow-app.toml" or rel.parts[0] == "tests" or rel.name.startswith("test_"):
            continue
        out.append(rel.as_posix())
    return out


def needs_qa_tests(item) -> bool:
    """Há código Python e o dev não trouxe testes próprios: o QA escreve `tests/test_qa.py` a partir dos critérios."""
    base = story_dir(item)
    has_py = any(f.endswith(".py") for f in code_files(item))
    own = [p for p in (base / "tests").glob("test_*.py")] if (base / "tests").is_dir() else []
    return has_py and not [p for p in own if p.name != "test_qa.py"]


def syntax_errors(item) -> list[tuple[str, int, str]]:
    """[(arquivo relativo, linha, mensagem)] de todo .py da história que não compila (inclui tests/test_qa.py do QA)."""
    import ast
    base, out = story_dir(item), []
    for p, rel in _walk(base):
        if rel.suffix != ".py":
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=rel.as_posix())
        except SyntaxError as e:
            out.append((rel.as_posix(), e.lineno or 0, str(e.msg)))
    return out


def qa_test_error(item) -> str | None:
    """Problema no teste escrito pelo QA (ausente ou com erro de sintaxe) — o dev não pode corrigir isso."""
    f = story_dir(item) / "tests" / "test_qa.py"
    if not f.is_file():
        return "o QA não escreveu tests/test_qa.py"
    for rel, line, msg in syntax_errors(item):
        if rel.endswith("tests/test_qa.py"):
            return f"tests/test_qa.py tem erro de sintaxe (linha {line}: {msg})"
    return None


def precheck(item) -> str | None:
    """Mensagem de rejeição automática (sem acionar o QA) se a história não tem código; None se pode seguir."""
    if not enabled() or not _c("FLOW_SANDBOX_REQUIRE_CODE", True) or code_files(item):
        return None
    return (f"{item.key}: a entrega não contém código — só documentação (entrega.md). Escreva os arquivos reais da implementação "
            f"(ex.: {item.key}/app.py, {item.key}/templates/login.html) com write_workspace_file; descrever o que foi feito não conta.")


# ------------------------------------------------------------------ dependências (allowlist)
def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", re.split(r"[\[<>=!~\s]", name, 1)[0]).lower()


def requirements(base: Path) -> list[str]:
    """Especificadores declarados (requirements.txt e/ou [project].dependencies). Linhas de opção/URL são bloqueadas."""
    specs, bad = [], []
    req = base / "requirements.txt"
    if req.is_file():
        for raw in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            (specs if SPEC.match(line) else bad).append(line)
    pp = base / "pyproject.toml"
    if pp.is_file():
        try:
            for line in tomllib.loads(pp.read_text(encoding="utf-8")).get("project", {}).get("dependencies", []):
                (specs if SPEC.match(line.strip()) else bad).append(line.strip())
        except tomllib.TOMLDecodeError:
            bad.append("pyproject.toml inválido")
    if bad:
        raise SandboxError("dependências com formato não permitido (URL, -r, -e, git+, opções): " + "; ".join(bad[:5]))
    return list(dict.fromkeys(specs))


def check_packages(specs: list[str]) -> list[str]:
    allowed = {_norm(x) for x in (_c("FLOW_SANDBOX_ALLOWED_PACKAGES") or [])}
    return sorted({_norm(s) for s in specs} - allowed)


# ------------------------------------------------------------------ snapshot
def snapshot(item) -> Path:
    src = story_dir(item)
    if not src.is_dir():
        raise SandboxError(f"pasta {item.key}/ não existe no workspace — nada para testar")
    root = Path(_c("FLOW_WORKSPACE")).resolve() / ".sandbox"
    root.mkdir(parents=True, exist_ok=True)
    dst = Path(tempfile.mkdtemp(prefix=f"{item.key}-", dir=root))
    n = total = 0
    for p, rel in _walk(src):
        n, total = n + 1, total + p.stat().st_size
        if n > MAX_FILES or total > MAX_BYTES:
            shutil.rmtree(dst, ignore_errors=True)
            raise SandboxError(f"entrega grande demais para o sandbox (> {MAX_FILES} arquivos ou {MAX_BYTES // 1_000_000} MB)")
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst / rel)
    for q in [dst, *dst.rglob("*")]:  # o container roda como uid 1000: precisa LER o snapshot (mkdtemp cria 0700)
        q.chmod(0o755 if q.is_dir() else 0o644)
    return dst


# ------------------------------------------------------------------ docker
def _docker(args, timeout, name=None):
    """Roda `docker <args>`; em timeout mata o container pelo nome. Devolve (exit_code, saída)."""
    try:
        p = subprocess.run(["docker", *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        return 124, f"timeout de {timeout}s — container encerrado"


def _build(img: str, dockerfile: str):
    if _docker(["image", "inspect", img], 20)[0] == 0:
        return img
    p = subprocess.run(["docker", "build", "-t", img, "-"], input=dockerfile.encode(), capture_output=True, timeout=int(_c("FLOW_SANDBOX_INSTALL_TIMEOUT", 300)) * 3)
    if p.returncode != 0:
        raise SandboxError(f"falha ao construir a imagem {img} (precisa de rede para baixar as bases/pacotes): " + (p.stdout + p.stderr).decode("utf-8", "replace")[-400:])
    return img


def ensure_image(e2e: bool = False) -> str:
    base = _build(_c("FLOW_SANDBOX_IMAGE", "flow-sandbox:1"), DOCKERFILE)
    if not e2e:
        return base
    return _build(_c("FLOW_SANDBOX_E2E_IMAGE", "flow-sandbox-e2e:1"), DOCKERFILE_E2E.replace("{base}", base).replace("{plugin}", PLUGIN_B64))


def uses_playwright(base: Path, specs: list[str] | None = None) -> bool:
    """A história tem teste de navegador? (importa playwright em tests/ ou declara o pacote)."""
    if any(_norm(x) in ("playwright", "pytest-playwright") for x in (specs or [])):
        return True
    t = base / "tests"
    return t.is_dir() and any("playwright" in f.read_text(encoding="utf-8", errors="replace") for f in t.rglob("*.py"))


def _hardening(name: str, net: str, e2e: bool = False):
    mem = str(_c("FLOW_SANDBOX_E2E_MEMORY", "1g")) if e2e else str(_c("FLOW_SANDBOX_MEMORY", "512m"))
    cpus = str(_c("FLOW_SANDBOX_E2E_CPUS", "2")) if e2e else str(_c("FLOW_SANDBOX_CPUS", "1"))
    pids = int(_c("FLOW_SANDBOX_E2E_PIDS", 512)) if e2e else int(_c("FLOW_SANDBOX_PIDS", 128))
    return ["run", "--rm", "--name", name, "--label", "flow=sandbox", "--network", net,
            "--memory", mem, "--memory-swap", mem, "--cpus", cpus, "--pids-limit", str(pids),
            *(["--shm-size", "512m"] if e2e else []),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,size=256m,mode=1777", "--user", "1000:1000", "-e", "HOME=/tmp", "-e", "UV_CACHE_DIR=/tmp/uv", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUTF8=1"]


PYTEST = ["pytest", "-q", "-x", "--tb=short", "-p", "no:cacheprovider"]


def _run_docker(snap: Path, specs: list[str], say=lambda t: None) -> tuple[int, str, str, dict]:
    e2e = uses_playwright(snap, specs)
    say(f"preparando a imagem do container{' (com Chromium/Playwright)' if e2e else ''}")
    img = ensure_image(e2e)
    tag = uuid.uuid4().hex[:8]
    py, extra = "/opt/venv/bin/python", {"e2e": e2e}
    need = [x for x in specs if _norm(x) not in (PRE_E2E if e2e else PRE)]  # o que a imagem já tem não precisa de rede
    if need:  # única etapa com rede: instala as dependências já validadas num venv dentro do snapshot
        say(f"📦 instalando dependências no sandbox: {_names_list(need)}")
        os.chmod(snap, 0o777)
        t0 = time.time()
        code, out = _docker([*_hardening(f"flow-sbx-i-{tag}", "bridge"), "-v", f"{snap}:/work:rw", "-w", "/work", img,
                             "sh", "-c", 'uv venv /work/.venv --python /opt/venv/bin/python -q && uv pip install -q --python /work/.venv/bin/python pytest "$@"', "sh",
                             *(["pytest-playwright"] if e2e else []), *need], int(_c("FLOW_SANDBOX_INSTALL_TIMEOUT", 300)), f"flow-sbx-i-{tag}")
        extra["install"] = {"ok": code == 0, "seconds": round(time.time() - t0, 1), "packages": need, "output": out[-600:]}
        if code != 0:
            return code, out, "install", extra
        py = "/work/.venv/bin/python"
    say(f"🧪 rodando pytest no container (sem rede{', com navegador Chromium' if e2e else ''})")
    name = f"flow-sbx-{tag}"
    code, out = _docker([*_hardening(name, "none", e2e), "-v", f"{snap}:/work:ro", "-w", "/work", img,
                         "uv", "run", "--no-project", "--offline", "--python", py, "--", *PYTEST, *(["-p", "flow_sbx"] if e2e else [])],
                        int(_c("FLOW_SANDBOX_E2E_TIMEOUT", 180)) if e2e else int(_c("FLOW_SANDBOX_TIMEOUT", 120)), name)
    return code, out, "test", extra


def _names_list(x, n=6):
    return ", ".join(x[:n]) + (f" (+{len(x) - n})" if len(x) > n else "")


# ------------------------------------------------------------------ fallback local (sem isolamento)
def _run_local(snap: Path, specs: list[str], say=lambda t: None) -> tuple[int, str, str, dict]:
    from .cd import _env
    env, extra, py = {**_env(), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, {}, sys.executable
    e2e = uses_playwright(snap, specs)
    if e2e and not any(_norm(x) == "pytest-playwright" for x in specs):
        specs = [*specs, "pytest-playwright"]
    if specs:
        say(f"📦 instalando dependências (local, SEM isolamento): {_names_list(specs)}")
        t0 = time.time()
        uv = shutil.which("uv")
        mk = [uv, "venv", "-q", ".venv"] if uv else [sys.executable, "-m", "venv", ".venv"]
        venv_py = str(snap / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python"))
        base = [b for b in BASE_LOCAL if _norm(b) not in {_norm(x) for x in specs}]  # o venv é limpo: leva o mesmo básico da imagem (flask, pytest, httpx)
        inst = [uv, "pip", "install", "-q", "--python", venv_py, *base, *specs] if uv else [venv_py, "-m", "pip", "install", "-q", *base, *specs]
        out = ""
        for cmd in (mk, inst):
            p = subprocess.run(cmd, cwd=snap, env=env, capture_output=True, timeout=int(_c("FLOW_SANDBOX_INSTALL_TIMEOUT", 300)), stdin=subprocess.DEVNULL)
            out += (p.stdout + p.stderr).decode("utf-8", "replace")
            if p.returncode != 0:
                return p.returncode, out, "install", {"install": {"ok": False, "packages": specs, "output": out[-600:]}}
        extra, py = {"install": {"ok": True, "seconds": round(time.time() - t0, 1), "packages": specs}}, venv_py
    say("🧪 rodando pytest (local, SEM isolamento)" + (" — com Playwright: precisa de `playwright install chromium`" if e2e else ""))
    plug = None
    if e2e:  # mesmo plugin da imagem (chromium sem sandbox de SO + fixture start_app), fora da pasta testada
        import base64
        plug = Path(tempfile.mkdtemp(prefix="flow-plug-"))
        (plug / "flow_sbx.py").write_bytes(base64.b64decode(PLUGIN_B64))
        env["PYTHONPATH"] = str(plug)
    try:
        p = subprocess.run([py, "-m", *PYTEST, *(["-p", "flow_sbx"] if e2e else [])], cwd=snap, env=env, capture_output=True, timeout=int(_c("FLOW_SANDBOX_TIMEOUT", 120)), stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"), "test", extra
    except subprocess.TimeoutExpired:
        return 124, f"timeout de {_c('FLOW_SANDBOX_TIMEOUT', 120)}s", "test", extra
    finally:
        if plug:
            shutil.rmtree(plug, ignore_errors=True)


# ------------------------------------------------------------------ API
def _summary(code: int, out: str, phase: str) -> str:
    if phase == "install":
        return "falha ao instalar as dependências"
    if code == 124:
        return "timeout"
    if code == 5:
        return "nenhum teste encontrado (escreva tests/test_*.py)"
    mm = re.search(r"No module named '([\w.]+)'", out)
    if mm:
        return f"falta o módulo '{mm.group(1).split('.')[0]}': declare-o em requirements.txt (nome do pacote no PyPI)"
    lines = [ln.strip(" =") for ln in out.splitlines() if ln.strip()]
    return next((ln for ln in reversed(lines) if re.search(r"\b(passed|failed|error|errors)\b", ln)), lines[-1] if lines else "sem saída")[:200]


def run(item, actor: str = "qa", own: bool = False) -> dict:
    """Executa o pytest da história no sandbox e devolve o relatório (também registrado na auditoria).
    Cada fase (imagem, instalação, pytest) aparece ao vivo no feed e na linha do agente; `own=True` = o runner chamou (libera o agente no fim)."""
    from . import activity, trace
    t0 = time.time()
    run_id = trace.begin(actor, "sandbox", item.key, title=f"sandbox · testes de {item.key}", body=f"{item.key}: {item.title}") if own else trace.latest_run(actor)
    cur = {"row": None}

    def say(text):
        activity.note(actor, f"{text} — {item.key}", item=item.key)
        audit.record(workflow_id=item.sprint.workflow_id, actor=actor, event_type="sandbox_phase", target=item.key, payload={"text": text})
        trace.update(cur["row"], state="ok")
        cur["row"] = trace.add(run_id, actor, "phase", text, state="running", meta={"story": item.key})
    rep = None
    try:
        rep = _run(item, actor, say, t0)
        return rep
    finally:
        trace.update(cur["row"], state="ok")
        if rep is not None:
            inst = rep.get("install") or {}
            if inst:
                trace.add(run_id, actor, "test", f"dependências {'instaladas' if inst.get('ok') else 'FALHARAM'}: {', '.join(inst.get('packages', []))}",
                          result=inst.get("output", ""), state="ok" if inst.get("ok") else "error", ms=int(float(inst.get("seconds", 0)) * 1000), meta={"packages": inst.get("packages", [])})
            trace.add(run_id, actor, "test", f"pytest {'passou' if rep['ok'] else 'FALHOU'}: {rep['summary']}", result=rep.get("output", ""), state="ok" if rep["ok"] else "error",
                      ms=int(float(rep.get("seconds", 0)) * 1000), meta={"isolation": rep.get("isolation"), "exit_code": rep.get("exit_code"), "e2e": bool(rep.get("e2e")), "skipped": bool(rep.get("skipped"))})
        if own:
            trace.end(run_id, actor, bool(rep and rep["ok"]), rep["summary"] if rep else "falhou")
            activity.release(actor)


def _run(item, actor, say, t0):
    iso = "-"
    base = {"story": item.key, "sprint": item.sprint_id, "round": item.rework_count}
    try:
        if not enabled():
            raise SandboxError("sandbox desabilitado ([sandbox].enabled = false)")
        if not code_files(item):
            raise SandboxError(precheck(item) or f"{item.key} não tem código para testar")
        if not any(rel.suffix == ".py" for _, rel in _walk(story_dir(item))):  # HTML/CSS/JS: pytest não se aplica; o QA verifica lendo os arquivos
            rep = {**base, "ok": True, "skipped": True, "isolation": "-", "phase": "skip", "exit_code": 0, "seconds": 0.0, "output": "",
                   "summary": "entrega sem código Python: pytest não se aplica (o QA verifica os critérios lendo os arquivos)"}
            audit.record(workflow_id=item.sprint.workflow_id, actor=actor, event_type="sandbox_run", status="ok", target=item.key, payload=rep)
            return rep
        for rel, line, msg in syntax_errors(item):  # erro de sintaxe: relatório claro em vez de "1 error" do pytest
            who = "do QA" if rel.endswith("tests/test_qa.py") else "do dev"
            raise SandboxError(f"erro de sintaxe {who} em {rel} (linha {line}): {msg}", fault="qa" if who == "do QA" else "dev")
        specs = requirements(story_dir(item))
        blocked = check_packages(specs)
        if blocked:
            raise SandboxError(f"pacotes fora da allowlist: {blocked}. Um humano precisa incluí-los em [sandbox].allowed_packages (flow.toml) antes de rodar")
        iso = mode()
        if iso == "local" and str(_c("FLOW_SANDBOX_MODE", "auto")).lower() == "auto":
            say(f"⚠️ Docker indisponível — {docker_why()}. Rodando SEM isolamento (use [sandbox].mode = \"docker\" para exigir o container)")
        snap = snapshot(item)
        try:
            code, out, phase, extra = (_run_docker if iso == "docker" else _run_local)(snap, specs, say)
        finally:
            shutil.rmtree(snap, ignore_errors=True)
        rep = {**base, "ok": code == 0 and phase == "test", "isolation": "container" if iso == "docker" else "subprocesso (SEM isolamento)", "phase": phase,
               "exit_code": code, "summary": _summary(code, out, phase), "seconds": round(time.time() - t0, 1), "output": out[-2500:], **extra}
    except (SandboxError, subprocess.SubprocessError, OSError) as e:
        fault = getattr(e, "fault", "env")
        rep = {**base, "ok": False, "isolation": iso, "phase": "setup" if fault == "env" else "test", "fault": fault, "exit_code": -1, "summary": str(e)[:300],
               "seconds": round(time.time() - t0, 1), "output": str(e)[:600]}
    audit.record(workflow_id=item.sprint.workflow_id, actor=actor, event_type="sandbox_run", status="ok" if rep["ok"] else "error", target=item.key, payload=rep)
    return rep


def last_report(item) -> dict | None:
    """Último `sandbox_run` desta história NESTA rodada de verificação (sprint + nº de retrabalhos)."""
    for ev in db_events(item):
        p = ev.payload or {}
        if p.get("sprint") == item.sprint_id and p.get("round") == item.rework_count:
            return p
    return None


def db_events(item):
    from .extensions import db
    from .models import AuditEvent
    return db.session.query(AuditEvent).filter_by(event_type="sandbox_run", target=item.key).order_by(AuditEvent.id.desc()).limit(8).all()


def setup_failure(rep: dict) -> bool:
    """Falha do ambiente (Docker fora, pacote fora da allowlist…) — não é culpa do código do dev."""
    return rep.get("phase") == "setup" and not rep.get("ok")


def passed_this_round(item) -> bool:
    r = last_report(item)
    return bool(r and r.get("ok"))


def feedback(item) -> str:
    """Trecho da saída do pytest para o dev (anexado ao feedback do retrabalho)."""
    r = last_report(item)
    if not r or r.get("ok"):
        return ""
    return f"\n\nResultado do pytest no sandbox ({r.get('isolation')}): {r.get('summary')}\n{(r.get('output') or '')[-1200:]}"
