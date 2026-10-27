"""Git por etapa. O workspace dos agentes é um repositório; o runner (não o LLM) versiona cada etapa Scrum.

    main ◀── merge --no-ff ── sprint/N ◀── merge --no-ff (aceite do PO) ── feature/US-001
                                 ▲                                              │ commits: implement · verify · (rework…) · accept
                    backlog ─────┘ (histórias refinadas, 1 commit por história)

* branch `backlog`      — worktree próprio: o refinamento pode acontecer enquanto uma sprint roda, sem trocar o checkout.
* branch `sprint/N`     — criada no planejamento (PLANNING.md + histórias); recebe só o que o PO aceitou.
* branch `feature/US-x` — 1 por história; 1 commit por etapa/retrabalho; rejeitada = branch fica sem merge (evidência).
* tag `story/US-x`      — no merge de aceite;  tag `sprint-N` — no merge da sprint em `main` (a "release").
* autor de cada commit = o agente que agiu (nome + slug@agents.flow.local) e trailers `Flow-*`; cada commit também vai à auditoria.

Git nunca derruba a sprint: qualquer falha vira evento `git_error` na auditoria e o fluxo segue.
"""
import functools
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

from flask import current_app

from . import audit

LOCK = threading.RLock()
MAIN, BACKLOG = "main", "backlog"
SYS = ("Flow System", "system@agents.flow.local")
GITIGNORE = ".worktrees/\n.deploy/\n__pycache__/\n*.pyc\n.venv/\nnode_modules/\n*.log\n.DS_Store\n"
SEP = "\x1f"


class GitError(Exception):
    pass


def enabled() -> bool:
    return bool(current_app.config.get("FLOW_GIT_ENABLED", True)) and shutil.which("git") is not None


def root() -> Path:
    return Path(current_app.config["FLOW_WORKSPACE"]).resolve()


def _run(args, cwd=None, author=None, check=True, text=True):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C.UTF-8"}
    name, email = author or SYS
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    cmd = ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false",
           "-c", "core.quotepath=off", "-c", "init.defaultBranch=" + MAIN, *args]
    p = subprocess.run(cmd, cwd=str(cwd or root()), env=env, capture_output=True, timeout=120)
    out = p.stdout.decode("utf-8", "replace") if text else p.stdout
    if check and p.returncode:
        raise GitError(f"git {' '.join(args[:3])}: {p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return out.strip() if text else out


def safe(fn):
    """Decorator: serializa as operações e converte qualquer falha em auditoria (nunca propaga)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not enabled():
            return None
        try:
            with LOCK:
                ensure()
                return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            try:
                audit.record(workflow_id=kw.get("workflow") or "git", actor="system", event_type="git_error", status="error",
                             target=fn.__name__, payload={"error": str(e)[:400]})
            except Exception:  # noqa: BLE001
                pass
            return None
    return wrapper


def ensure():
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    if (r / ".git").exists():
        return
    _run(["init", "-q", "-b", MAIN])
    (r / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    _run(["add", ".gitignore"])
    _run(["commit", "-q", "-m", "chore: inicializa o workspace do AI Scrum Flow\n\nFlow-Stage: init"])
    _run(["branch", BACKLOG, MAIN])


def _exists(ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=str(root()), capture_output=True).returncode == 0


def _agent(slug: str) -> tuple[str, str]:
    from .extensions import db
    from .models import AgentConfig
    a = db.session.query(AgentConfig).filter_by(slug=slug).first() if slug else None
    return ((a.name if a else slug or SYS[0]), f"{slug or 'system'}@agents.flow.local")


def _trailers(**kv) -> str:
    return "\n".join(f"Flow-{k.capitalize()}: {v}" for k, v in kv.items() if v not in (None, ""))


def _checkout(branch: str, create_from: str | None = None):
    if _exists(f"refs/heads/{branch}"):
        _run(["checkout", "-q", branch])
    else:
        _run(["checkout", "-q", "-b", branch, create_from or MAIN])


def _commit(message: str, slug: str, paths=None, cwd=None, allow_empty=True) -> str:
    _run(["add", "-A", *(paths or ["."])], cwd=cwd)
    args = ["commit", "-q", "-m", message] + (["--allow-empty"] if allow_empty else [])
    _run(args, cwd=cwd, author=_agent(slug))
    return _run(["rev-parse", "HEAD"], cwd=cwd)


def _record(workflow, slug, sha, branch, stage, subject, **extra):
    audit.record(workflow_id=workflow, actor=slug or "system", event_type="git_commit", target=sha[:10],
                 payload={"branch": branch, "stage": stage, "subject": subject, "sha": sha, **extra})


def _slug(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40]


def sprint_branch(sprint) -> str:
    return f"sprint/{sprint.id}"


def feature_branch(item) -> str:
    return f"feature/{item.key}"


# --------------------------------------------------------------------------- refinamento (branch `backlog`, worktree próprio)
def _backlog_wt() -> Path:
    wt = root() / ".worktrees" / BACKLOG
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        _run(["worktree", "prune"])
        _run(["worktree", "add", "-q", str(wt), BACKLOG])
    return wt


@safe
def commit_story(item, slug: str, workflow: str = "backlog"):
    """Refinamento: a user story vira `backlog/US-xxx.md` na branch `backlog` (1 commit por história)."""
    wt = _backlog_wt()
    crit = "\n".join(f"- [ ] {c}" for c in item.acceptance_criteria or [])
    (wt / "backlog").mkdir(exist_ok=True)
    (wt / "backlog" / f"{item.key}.md").write_text(
        f"# {item.key} — {item.title}\n\n**Pontos:** {item.points}  \n**Criada por:** {item.created_by}\n\n"
        f"{item.description}\n\n## Critérios de aceite\n{crit}\n", encoding="utf-8")
    msg = f"refine({item.key}): {item.title[:70]}\n\n{_trailers(story=item.key, stage='refine', agent=slug, points=item.points)}"
    sha = _commit(msg, slug, ["backlog"], cwd=wt)
    _record(workflow, slug, sha, BACKLOG, "refine", item.title[:70], story=item.key)
    return sha


# --------------------------------------------------------------------------- sprint
@safe
def begin_sprint(sprint, items, sm_slug: str):
    """Planning: cria `sprint/N` a partir de main, traz as histórias refinadas e registra o PLANNING.md."""
    br, wf = sprint_branch(sprint), sprint.workflow_id
    _checkout(br, MAIN)
    _run(["merge", "-q", "--no-edit", BACKLOG], check=False, author=_agent(sm_slug))
    rows = "\n".join(f"| {i.key} | {i.title} | {i.points} | {i.assignee.slug if i.assignee else '-'} |" for i in items)
    d = root() / wf
    d.mkdir(parents=True, exist_ok=True)
    (d / "PLANNING.md").write_text(f"# {sprint.name} — Planning\n\n**Meta:** {sprint.goal}\n\n**Capacidade:** {sprint.capacity} pts\n\n"
                                   f"| História | Título | Pts | Responsável |\n|---|---|---|---|\n{rows}\n", encoding="utf-8")
    msg = f"plan({sprint.name}): {len(items)} itens, meta: {sprint.goal[:60]}\n\n" + _trailers(sprint=sprint.id, stage="plan", agent=sm_slug, items=",".join(i.key for i in items))
    sha = _commit(msg, sm_slug, [wf])
    _record(wf, sm_slug, sha, br, "plan", f"{len(items)} itens")
    return sha


@safe
def begin_item(item, sm_slug: str):
    """Cria a branch `feature/US-xxx` (a partir da sprint) e deixa o checkout nela: os agentes escrevem ali."""
    _checkout(sprint_branch(item.sprint), None)
    _checkout(feature_branch(item), sprint_branch(item.sprint))
    return feature_branch(item)


def _commit_stage(item, stage: str, slug: str, summary: str = "", result: str = "", rework: int = 0):
    """Um commit por etapa (implement/verify/…): inclui os arquivos novos; se nada mudou, vira commit vazio com o registro da etapa."""
    br, wf = feature_branch(item), item.sprint.workflow_id
    _checkout(br, sprint_branch(item.sprint))
    subject = f"{stage}({item.key}): {summary.splitlines()[0][:70] if summary else item.title[:70]}"
    msg = f"{subject}\n\n{summary[:800]}\n\n" + _trailers(sprint=item.sprint_id, story=item.key, stage=stage, agent=slug, result=result, rework=rework or "")
    sha = _commit(msg, slug, [wf])
    _record(wf, slug, sha, br, stage, subject, story=item.key, result=result)
    return sha


commit_stage = safe(_commit_stage)


@safe
def accept_item(item, po_slug: str, reason: str = ""):
    """Aceite do PO: merge --no-ff da feature na sprint + tag `story/US-xxx`."""
    br, sb, wf = feature_branch(item), sprint_branch(item.sprint), item.sprint.workflow_id
    _checkout(br, sb)
    acc = _commit(f"accept({item.key}): aceito pelo PO\n\n{reason[:400]}\n\n" + _trailers(sprint=item.sprint_id, story=item.key, stage="accept", agent=po_slug, result="accepted"), po_slug, [wf])
    _record(wf, po_slug, acc, br, "accept", f"{item.key} aceito pelo PO", story=item.key, result="accepted")
    _checkout(sb)
    _run(["merge", "-q", "--no-ff", "-m", f"merge({item.key}): {item.title[:60]}\n\n" + _trailers(sprint=item.sprint_id, story=item.key, stage="merge", agent=po_slug), br], author=_agent(po_slug))
    _run(["tag", "-f", f"story/{item.key}", "HEAD"], author=_agent(po_slug))
    sha = _run(["rev-parse", "HEAD"])
    _record(wf, po_slug, sha, sb, "merge", f"{item.key} → {sb}", story=item.key, tag=f"story/{item.key}")
    return sha


@safe
def reject_item(item, slug: str, reason: str = ""):
    """Rejeitado/impedido: registra na feature e NÃO faz merge; a branch fica como evidência."""
    sha = _commit_stage(item, "reject", slug, reason or "rejeitado", result="rejected")
    _checkout(sprint_branch(item.sprint))
    return sha


@safe
def close_sprint(sprint, stats: dict, retro: str, sm_slug: str, po_slug: str):
    """Review/retro: REVIEW.md na sprint, merge --no-ff em `main` e tag `sprint-N` (a release)."""
    sb, wf = sprint_branch(sprint), sprint.workflow_id
    _checkout(sb, MAIN)
    (root() / wf / "REVIEW.md").write_text(
        f"# {sprint.name} — Review & Retro\n\n- Itens aceitos: {stats['done']}/{stats['total']}\n- Pontos entregues: {stats['points']}\n"
        f"- Retrabalhos: {stats['rework']}\n\n## Retrospectiva\n{retro or '(sem registro)'}\n", encoding="utf-8")
    rev = _commit(f"review({sprint.name}): {stats['done']}/{stats['total']} itens, {stats['points']} pts\n\n" + _trailers(sprint=sprint.id, stage="review", agent=sm_slug), sm_slug, [wf])
    _record(wf, sm_slug, rev, sb, "review", f"{stats['done']}/{stats['total']} itens", **stats)
    _checkout(MAIN)
    _run(["merge", "-q", "--no-ff", "-m", f"release({sprint.name}): {stats['done']}/{stats['total']} itens, {stats['points']} pts\n\n" + _trailers(sprint=sprint.id, stage="release", agent=po_slug), sb], author=_agent(po_slug))
    tag = f"sprint-{sprint.id}"
    _run(["tag", "-f", "-a", tag, "-m", f"{sprint.name}: {stats['done']}/{stats['total']} itens, {stats['points']} pts, velocidade {stats['points']}"], author=_agent(po_slug))
    sha = _run(["rev-parse", "HEAD"])
    _record(wf, po_slug, sha, MAIN, "release", f"tag {tag}", tag=tag, **stats)
    return {"sha": sha, "tag": tag}


# --------------------------------------------------------------------------- leitura (dashboard / API / CD)
def _parse(raw: str) -> list[dict]:
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        f = rec.split(SEP)
        if len(f) < 8:
            continue
        body = f[7]
        tr = dict(re.findall(r"^Flow-(\w+): (.+)$", body, re.M))
        out.append({"sha": f[0], "short": f[0][:7], "parents": f[1].split(), "author": f[2], "email": f[3], "date": f[4], "refs": [r.strip() for r in f[5].split(",") if r.strip()],
                    "subject": f[6], "stage": tr.get("Stage", ""), "story": tr.get("Story", ""), "sprint": tr.get("Sprint", ""), "agent": tr.get("Agent", ""), "result": tr.get("Result", "")})
    return out


FMT = "%x1e%H" + "%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%D%x1f%s%x1f%b"


def log(branch: str = "", story: str = "", limit: int = 60) -> list[dict]:
    if not enabled() or not (root() / ".git").exists():
        return []
    with LOCK:
        args = ["log", f"--max-count={min(int(limit), 300)}", f"--pretty=format:{FMT}"]
        args += ([branch] if branch else ["--all"])
        if story:
            args += ["-E", f"--grep=^Flow-Story: {re.escape(story)}$"]
        try:
            return _parse(_run(args))
        except GitError:
            return []


def branches() -> list[dict]:
    if not enabled() or not (root() / ".git").exists():
        return []
    with LOCK:
        try:
            raw = _run(["for-each-ref", "--sort=-committerdate", f"--format=%(refname:short){SEP}%(objectname:short){SEP}%(subject){SEP}%(committerdate:iso-strict){SEP}%(HEAD)", "refs/heads"])
            merged = set(_run(["branch", "--merged", MAIN, "--format=%(refname:short)"]).split())
        except GitError:
            return []
    out = []
    for line in raw.splitlines():
        n, sha, sub, date, head = (line.split(SEP) + [""] * 5)[:5]
        kind = "main" if n == MAIN else "sprint" if n.startswith("sprint/") else "story" if n.startswith("feature/") else "backlog"
        out.append({"name": n, "sha": sha, "subject": sub, "date": date, "current": head.strip() == "*", "merged": n in merged, "kind": kind})
    return out


def tags() -> list[dict]:
    if not enabled() or not (root() / ".git").exists():
        return []
    with LOCK:
        try:
            raw = _run(["for-each-ref", "--sort=-creatordate", f"--format=%(refname:short){SEP}%(objectname:short){SEP}%(creatordate:iso-strict){SEP}%(contents:subject)", "refs/tags"])
        except GitError:
            return []
    return [dict(zip(("name", "sha", "date", "subject"), l.split(SEP))) for l in raw.splitlines() if l]


def show(sha: str) -> dict | None:
    if not enabled() or not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha or ""):
        return None
    with LOCK:
        try:
            c = _parse(_run(["show", "-s", f"--pretty=format:{FMT}", sha]))
            stat = _run(["show", "--stat", "--format=", sha])
            diff = _run(["show", "--format=", "--no-color", "-m", "--first-parent", sha])
        except GitError:
            return None
    return {**(c[0] if c else {}), "stat": stat, "diff": diff[:30000], "truncated": len(diff) > 30000}


def status() -> dict:
    if not enabled():
        return {"enabled": False, "reason": "git desativado ou não instalado"}
    if not (root() / ".git").exists():
        return {"enabled": True, "initialized": False, "branches": 0, "commits": 0, "tags": 0}
    with LOCK:
        try:
            head = _run(["rev-parse", "--abbrev-ref", "HEAD"])
            n = int(_run(["rev-list", "--all", "--count"]))
        except GitError:
            return {"enabled": True, "initialized": False}
    return {"enabled": True, "initialized": True, "head": head, "commits": n, "branches": len(branches()), "tags": len(tags()), "path": str(root())}


def ls_tree(ref: str, prefix: str) -> list[str]:
    with LOCK:
        try:
            return [l for l in _run(["ls-tree", "-r", "--name-only", ref, "--", prefix]).splitlines() if l]
        except GitError:
            return []


def read_blob(ref: str, path: str, limit: int = 60000) -> str | None:
    with LOCK:
        try:
            return _run(["show", f"{ref}:{path}"])[:limit]
        except GitError:
            return None


def export_tag(ref: str, subdir: str, dest: Path) -> Path:
    """Extrai `subdir` exatamente como está em `ref` (tag da release) para `dest` — é isso que o CD sobe. Python puro (funciona no Windows)."""
    with LOCK:
        raw = _run(["archive", "--format=tar", ref, subdir], text=False)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for m in tf.getmembers():
            if not m.isfile():  # sem links/dispositivos
                continue
            target = (dest / m.name).resolve()
            if base not in target.parents:  # sem path traversal
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tf.extractfile(m).read())
    return dest / subdir
