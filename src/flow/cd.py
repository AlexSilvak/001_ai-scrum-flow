"""CI/CD local. O agente `devops` roda o CI e sobe a aplicação ENTREGUE (a versão exata da tag `sprint-N` em main).

Contrato: a história que entrega uma aplicação executável inclui um `flow-app.toml` (veja skills/dev/app-manifest):

    name    = "login"
    workdir = "."                         # relativo à pasta do manifesto
    [ci]
    steps = [["python", "-m", "pytest", "-q"]]   # opcional; padrão: byte-compile dos .py
    [run]
    command = ["python", "app.py"]
    port    = 8101                        # dentro de [cd].port_range; o app recebe PORT e HOST=127.0.0.1
    health  = "/"                         # opcional (GET); sem isso basta a porta abrir

Segurança (o código vem de um LLM e roda na SUA máquina):
 • nada sobe sem aprovação humana (`[cd].auto_deploy = false`, padrão);  • sem shell, só executáveis de `[cd].allowed_commands`;
 • roda numa CÓPIA da tag em workspace/.deploy/…, com venv próprio;  • ambiente sem tokens/segredos (FLOW_*, *KEY*, *TOKEN*…);
 • porta só da faixa configurada;  • só processos que ESTE servidor iniciou são encerrados.
"""
import http.client
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path, PurePosixPath

from flask import current_app

from . import audit, gitflow
from .extensions import db
from .models import Deployment, now

PROCS: dict[int, subprocess.Popen] = {}
WIN = os.name == "nt"
SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|^FLOW_|^A2A_|DATABASE_URL|^SQLALCHEMY)", re.I)
LIVE = ("deploying", "running")


class CDError(Exception):
    pass


def enabled() -> bool:
    return bool(current_app.config.get("FLOW_CD_ENABLED", True))


def _root() -> Path:
    return Path(current_app.config["FLOW_WORKSPACE"]).resolve()


def _log(dep, event, status="ok", **payload):
    audit.record(workflow_id=f"sprint-{dep.sprint_id}" if dep.sprint_id else "cd", actor=payload.pop("actor", "devops"), event_type=event,
                 status=status, target=f"deploy#{dep.id}:{dep.app}", payload=payload)


def _set(dep, status=None, detail=None, **kw):
    if status:
        dep.status = status
    if detail is not None:
        dep.detail = detail[:1500]
    for k, v in kw.items():
        setattr(dep, k, v)
    dep.updated_at = now()
    db.session.commit()


# ------------------------------------------------------------------ manifesto
def validate_manifest(m: dict) -> dict:
    """Valida e normaliza. Levanta CDError com mensagem acionável."""
    cfg = current_app.config
    allowed = set(cfg["FLOW_CD_ALLOWED"])
    lo, hi = cfg["FLOW_CD_PORT_RANGE"]
    run = m.get("run") or {}
    port = run.get("port")
    if not isinstance(port, int) or not (lo <= port <= hi):
        raise CDError(f"[run].port deve ser inteiro entre {lo} e {hi} (faixa configurada em [cd].port_range)")
    if run.get("static") is True:  # site estático (estilo Live Server): sem comando, serve a pasta com http.server em 127.0.0.1
        cmd = ["python", "-m", "http.server", str(port), "--bind", "127.0.0.1"]
        run = {**run, "health": run.get("health") or "/"}
    else:
        cmd = run.get("command")
    if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd)):
        raise CDError("[run].command deve ser uma lista, ex.: [\"python\", \"app.py\"] (ou use [run] static = true para um site HTML)")
    steps = (m.get("ci") or {}).get("steps")
    if steps is not None and not (isinstance(steps, list) and all(isinstance(s, list) and s and all(isinstance(x, str) for x in s) for s in steps)):
        raise CDError("[ci].steps deve ser lista de comandos (cada um uma lista de strings)")
    for c in [cmd, *(steps or [])]:
        _check_exe(c[0], allowed)
    wd = PurePosixPath(m.get("workdir", "."))
    if wd.is_absolute() or ".." in wd.parts:
        raise CDError("workdir deve ser relativo e ficar dentro da pasta do manifesto")
    health = run.get("health", "")
    if health and not str(health).startswith("/"):
        raise CDError("[run].health deve começar com /")
    return {"name": re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(m.get("name") or "app"))[:60], "workdir": str(wd),
            "ci": {"steps": steps}, "run": {"command": cmd, "port": port, "health": health, "static": run.get("static") is True, "env": {str(k): str(v) for k, v in (run.get("env") or {}).items() if not SECRET_ENV.search(str(k))}}}


def _check_exe(exe: str, allowed: set):
    name = exe.lower()
    if any(sep in exe for sep in ("/", "\\")) or name.removesuffix(".exe") not in allowed:
        raise CDError(f"executável '{exe}' não permitido; permitidos: {sorted(allowed)} (ajuste [cd].allowed_commands)")


def find_manifests(tag: str, workflow: str) -> list[str]:
    paths = [p for p in gitflow.ls_tree(tag, workflow) if p.endswith("flow-app.toml")]
    return sorted(paths, key=lambda p: (p.count("/"), p))[:3]


# ------------------------------------------------------------------ release → deployments (1 por manifesto)
def _pick_port() -> int:
    lo, hi = current_app.config["FLOW_CD_PORT_RANGE"]
    used = {d.port for d in db.session.query(Deployment).filter(Deployment.status.in_(LIVE)) if d.port}
    return next((p for p in range(lo, hi + 1) if p not in used and _port_free(p)), lo)


def _static_sites(tag: str, workflow: str) -> list[str]:
    """Sem flow-app.toml: pastas com index.html (o dev entregou uma página) viram um site estático."""
    idx = [p for p in gitflow.ls_tree(tag, workflow) if p.endswith("/index.html")]
    return sorted(idx, key=lambda p: (p.count("/"), p))[:2]


def prepare(sprint, tag: str, sha: str) -> list[Deployment]:
    """Cria o(s) Deployment(s) da release: 1 por `flow-app.toml`; sem manifesto, 1 por pasta com `index.html` (site estático)."""
    if not enabled() or not tag:
        return []
    entries = [(p, None) for p in find_manifests(tag, sprint.workflow_id)]
    if not entries:
        entries = [(p, "static") for p in _static_sites(tag, sprint.workflow_id)]
    if not entries:
        d = Deployment(sprint_id=sprint.id, tag=tag, commit=sha, app="-", status="skipped",
                       detail="Nada para subir: nenhum flow-app.toml nem index.html entregue nesta sprint.")
        db.session.add(d)
        db.session.commit()
        _log(d, "cd_skipped", actor="system", reason=d.detail)
        return [d]
    out = []
    for path, kind in entries:
        folder = PurePosixPath(path).parent
        d = Deployment(sprint_id=sprint.id, tag=tag, commit=sha, manifest_path=path, app=folder.name or "app")
        db.session.add(d)
        db.session.commit()
        try:
            if kind == "static":
                raw = {"name": folder.name.lower(), "run": {"static": True, "port": _pick_port(), "health": "/"}}
            else:
                raw = tomllib.loads(gitflow.read_blob(tag, path) or "")
            d.manifest = validate_manifest(raw)
            d.app = d.manifest["name"]
            dest = gitflow.export_tag(tag, str(folder), _root() / ".deploy" / sprint.workflow_id / f"{tag}-{d.id}")
            d.workdir = str((dest / d.manifest["workdir"]).resolve())
            _set(d, "ci_pending", f"release {tag} pronta para o CI" + (" (site estático, detectado pelo index.html)" if kind == "static" else ""))
        except (CDError, tomllib.TOMLDecodeError, gitflow.GitError) as e:
            _set(d, "failed", f"manifesto inválido ({path}): {e}")
            _log(d, "cd_manifest_invalid", status="error", error=str(e))
        out.append(d)
    return out


# ------------------------------------------------------------------ execução de comandos
def _env(extra=None) -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.update({"PYTHONUNBUFFERED": "1", "HOST": "127.0.0.1", **(extra or {})})
    return env


def _python(workdir: Path) -> str:
    venv = workdir / ".venv" / ("Scripts" if WIN else "bin") / ("python.exe" if WIN else "python")
    return str(venv) if venv.exists() else sys.executable


def _argv(cmd: list[str], workdir: Path) -> list[str]:
    exe = cmd[0].lower().removesuffix(".exe")
    py = _python(workdir)
    if exe in ("python", "python3"):
        return [py, *cmd[1:]]
    if exe in ("pip", "pytest", "flask", "gunicorn", "uvicorn"):
        return [py, "-m", exe, *cmd[1:]]
    found = shutil.which(cmd[0])
    if not found:
        raise CDError(f"executável '{cmd[0]}' não encontrado no PATH desta máquina")
    return [found, *cmd[1:]]


def _step(name: str, cmd: list[str], workdir: Path, timeout: int) -> dict:
    t0 = time.time()
    try:
        p = subprocess.run(_argv(cmd, workdir), cwd=str(workdir), env=_env(), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        ok, out = p.returncode == 0, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        ok, out = False, f"timeout de {timeout}s"
    except (CDError, OSError) as e:
        ok, out = False, str(e)
    return {"name": name, "cmd": " ".join(cmd), "ok": ok, "seconds": round(time.time() - t0, 1), "output": out[-1800:]}


def run_ci(dep: Deployment) -> list[dict]:
    """setup (venv + requirements) → passos do manifesto (ou byte-compile) → relatório. Para no 1º erro."""
    wd, timeout = Path(dep.workdir), int(current_app.config["FLOW_CD_STEP_TIMEOUT"])
    if not wd.is_dir():
        raise CDError("cópia da release não encontrada — rode a release novamente")
    _set(dep, "ci_running", "CI em execução")
    report = []
    req = wd / "requirements.txt"
    if req.is_file():
        if not (wd / ".venv").exists():
            report.append(_step("venv", ["python", "-m", "venv", ".venv"], wd, timeout))
            report[-1]["cmd"] = "python -m venv .venv"
        if report[-1]["ok"] if report else True:
            report.append(_step("dependências", ["pip", "install", "-q", "-r", "requirements.txt"], wd, timeout))
    if all(r["ok"] for r in report):
        steps = (dep.manifest.get("ci") or {}).get("steps")
        if steps is None:
            steps = [["python", "-m", "compileall", "-q", "."]] if any(wd.rglob("*.py")) else []
        for i, c in enumerate(steps, 1):
            report.append(_step(f"ci {i}", c, wd, timeout))
            if not report[-1]["ok"]:
                break
        if not steps:
            report.append({"name": "ci", "cmd": "(nenhum passo declarado)", "ok": True, "seconds": 0, "output": "sem passos de CI no manifesto e sem arquivos .py"})
    ok = all(r["ok"] for r in report)
    approved = bool(current_app.config.get("FLOW_CD_AUTO_DEPLOY"))
    _set(dep, "awaiting_approval" if ok else "ci_failed",
         ("CI passou — aguardando aprovação humana para subir" if not approved else "CI passou — deploy automático habilitado") if ok
         else "CI falhou: " + next(r["name"] + " — " + r["output"][-300:] for r in report if not r["ok"]),
         ci_report=report, approved_by="auto" if (ok and approved) else dep.approved_by)
    _log(dep, "ci_result", status="ok" if ok else "error", steps=[{k: r[k] for k in ("name", "ok", "seconds")} for r in report])
    return report


# ------------------------------------------------------------------ processo
def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _alive(dep: Deployment) -> bool:
    p = PROCS.get(dep.id)
    if p is not None:
        return p.poll() is None
    return bool(dep.port) and not _port_free(dep.port)  # iniciado por outra execução do servidor: só dá para observar a porta


def _kill_tree(p: subprocess.Popen):
    if p.poll() is not None:
        return
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        p.kill()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        pass


def stop(dep: Deployment, actor="human") -> str:
    p = PROCS.pop(dep.id, None)
    if p is not None:
        _kill_tree(p)
        _set(dep, "stopped", "aplicação encerrada", pid=None)
        _log(dep, "deploy_stopped", actor=actor)
        return "encerrada"
    if dep.status in LIVE and dep.port and not _port_free(dep.port):
        return f"o processo (PID {dep.pid}, porta {dep.port}) foi iniciado por outra execução do servidor; por segurança só encerro o que eu mesmo iniciei — finalize-o manualmente"
    _set(dep, "stopped", "processo não estava mais em execução", pid=None)
    return "já estava parada"


def _wait_ready(dep: Deployment, p: subprocess.Popen, port: int, health: str, timeout: int) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if p.poll() is not None:
            return False
        try:
            if health:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", health)
                code = c.getresponse().status
                c.close()
                if code < 500:
                    return True
            elif not _port_free(port):
                return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


def logs_path(dep: Deployment) -> Path:
    d = _root() / ".deploy" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"deploy-{dep.id}.log"


def tail(dep: Deployment, lines: int = 60) -> str:
    f = logs_path(dep)
    return "\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 400)):]) if f.is_file() else ""


def start(dep: Deployment, actor="devops") -> dict:
    """Sobe a aplicação (exige CI ok + aprovação), substitui a versão anterior do mesmo app e espera ficar saudável."""
    if dep.status not in ("awaiting_approval", "stopped", "failed", "running", "deploying"):
        raise CDError(f"não é possível subir uma release em '{dep.status}' (o CI precisa passar antes)")
    if not dep.approved_by:
        raise CDError("aguardando aprovação humana — clique em “Aprovar e subir” no dashboard (ou ligue [cd].auto_deploy)")
    if dep.id in PROCS and PROCS[dep.id].poll() is None:
        return {"status": "running", "url": dep.url}
    run = dep.manifest["run"]
    port = int(run["port"])
    for old in db.session.query(Deployment).filter(Deployment.app == dep.app, Deployment.id != dep.id, Deployment.status.in_(LIVE)):
        stop(old, actor)  # a nova release substitui a anterior
    if not _port_free(port):
        _set(dep, "failed", f"a porta {port} já está em uso por outro programa")
        raise CDError(dep.detail)
    wd = Path(dep.workdir)
    _set(dep, "deploying", f"subindo {' '.join(run['command'])} na porta {port}")
    log = open(logs_path(dep), "wb")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if WIN else {"start_new_session": True}
    try:
        p = subprocess.Popen(_argv(run["command"], wd), cwd=str(wd), env=_env({**run.get("env", {}), "PORT": str(port)}), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    except (CDError, OSError) as e:
        log.close()
        _set(dep, "failed", f"não consegui iniciar: {e}")
        raise CDError(dep.detail)
    PROCS[dep.id] = p
    _set(dep, pid=p.pid, port=port)
    ready = _wait_ready(dep, p, port, run.get("health", ""), int(current_app.config["FLOW_CD_START_TIMEOUT"]))
    log.close()
    if not ready:
        PROCS.pop(dep.id, None)
        _kill_tree(p)
        _set(dep, "failed", "a aplicação não ficou saudável: " + (tail(dep, 12) or "sem saída")[-500:], pid=None)
        _log(dep, "deploy_failed", status="error", actor=actor, log=tail(dep, 12))
        raise CDError(dep.detail)
    _set(dep, "running", f"no ar em http://127.0.0.1:{port}{run.get('health', '')}", url=f"http://127.0.0.1:{port}")
    _log(dep, "deploy_started", actor=actor, url=dep.url, pid=p.pid, commit=dep.commit, tag=dep.tag)
    return {"status": "running", "url": dep.url, "pid": p.pid}


def refresh(dep: Deployment) -> Deployment:
    """Reconcilia o estado salvo com o mundo real (o processo pode ter caído)."""
    if dep.status == "running" and not _alive(dep):
        PROCS.pop(dep.id, None)
        _set(dep, "stopped", "o processo terminou (veja os logs)", pid=None)
    return dep


def approve(dep: Deployment, who: str):
    if dep.status not in ("awaiting_approval", "stopped", "failed"):
        raise CDError(f"release em '{dep.status}' não pode ser aprovada agora")
    if not dep.ci_report or not all(r["ok"] for r in dep.ci_report):
        raise CDError("o CI não passou — não há o que aprovar")
    _set(dep, approved_by=who, detail=f"aprovado por {who}")
    _log(dep, "deploy_approved", actor=who)


def devops_agent():
    from .models import AgentConfig
    return next((a for a in db.session.query(AgentConfig).filter_by(enabled=True) if "devops" in (a.specialty or "").lower()), None)


def ceremony(devops, dep: Deployment, kind: str, ctx, sender: str):
    """Pede ao agente devops (via A2A) que rode o CI (`ci`) ou suba a aplicação (`deploy`). O agente decide e chama as tools."""
    from . import a2a

    tool = "ci_run" if kind == "ci" else "deploy_app"
    verb = "Rode o CI" if kind == "ci" else "Suba a aplicação"
    data = {"ceremony": kind, "deployment": {"id": dep.id, "app": dep.app, "release": dep.tag, "status": dep.status}, "requires": [tool],
            "recover": {tool: {"deployment_id": dep.id}}}
    return a2a.send_message(sender, devops.slug, f"{verb} da release {dep.tag} (deployment_id={dep.id}, app {dep.app}).", data=data,
                            ctx=ctx, context_id=ctx.workflow_id, enforce=False)


def run_ceremony(dep_id: int, kind: str, who: str):
    """Chamado pelo worker (jobs flow.cd_ci / flow.cd_deploy): o agente devops roda a cerimônia."""
    from .tools import ToolContext

    dep = db.session.get(Deployment, dep_id)
    devops = devops_agent()
    if not devops:
        _set(dep, "failed", "nenhum agente devops habilitado — rode `flask --app flow seed --sync`")
        return
    ctx = ToolContext(agent=devops, workflow_id=f"sprint-{dep.sprint_id}", user_email=who, sprint_id=dep.sprint_id)
    ceremony(devops, dep, kind, ctx, "system")
