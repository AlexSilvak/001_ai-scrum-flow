"""Provedores de LLM por agente: `ollama` (modelos locais) e `mock` (offline, determinístico — testes/demo).

Contrato único: `chat(messages, cfg) -> (texto, usage)`. Cada agente escolhe provider/model/base_url/temperature.
"""
import json
import time
import os
import re
from dataclasses import dataclass

import httpx
from flask import current_app, has_app_context


@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str = ""
    temperature: float = 0.2
    max_tokens: int = 1500
    timeout: int = 120


class LLMError(RuntimeError):
    pass


def default_ollama_url() -> str:
    return current_app.config["OLLAMA_BASE_URL"] if has_app_context() else "http://localhost:11434"


def config_for(agent) -> LLMConfig:
    return LLMConfig(provider=agent.provider, model=agent.model, base_url=agent.base_url,
                     temperature=agent.temperature, max_tokens=agent.max_output_tokens, timeout=agent.timeout_s)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _ollama(messages, c: LLMConfig, on_chunk=None):
    base = (c.base_url or default_ollama_url()).rstrip("/")
    body = {"model": c.model, "messages": messages, "stream": bool(on_chunk),
            "format": "json",  # força saída JSON (o protocolo de tools é JSON) — ajuda muito modelos pequenos
            "options": {"temperature": c.temperature, "num_predict": c.max_tokens}}
    try:
        if not on_chunk:
            r = httpx.post(f"{base}/api/chat", timeout=c.timeout, json=body)
            r.raise_for_status()
            d = r.json()
            return d["message"]["content"], {"in": d.get("prompt_eval_count", 0), "out": d.get("eval_count", 0)}
        text, last = "", {}
        with httpx.stream("POST", f"{base}/api/chat", timeout=c.timeout, json=body) as r:  # streaming: o painel mostra o que o modelo está gerando
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise LLMError(f"Ollama: {d['error']}")
                text += (d.get("message") or {}).get("content", "")
                if d.get("done"):
                    last = d
                else:
                    on_chunk(text)
        return text, {"in": last.get("prompt_eval_count", 0), "out": last.get("eval_count", 0)}
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama indisponível em {base} ({type(e).__name__}: {e}). Rode `ollama serve` e `ollama pull {c.model}`.") from e


def list_ollama_models(base_url: str = "") -> list[str]:
    """Modelos instalados no Ollama (GET /api/tags) — alimenta o seletor de modelo da UI."""
    base = (base_url or default_ollama_url()).rstrip("/")
    try:
        r = httpx.get(f"{base}/api/tags", timeout=5)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))
    except httpx.HTTPError:
        return []


# ------------------------------------------------------------------ mock (offline)
_TASK = re.compile(r"<task>(.*?)</task>", re.S)


def _split_stories(text: str) -> list[str]:
    parts = [p.strip(" -•*\t") for p in re.split(r"[\n;]+", text) if p.strip(" -•*\t")]
    return parts[:5] or [text.strip()[:120]]


def _guess_points(t: str) -> int:
    n = len(t)
    return 2 if n < 40 else 3 if n < 80 else 5 if n < 140 else 8


def _pick_assignee(title: str, team: list[dict]) -> str:
    t = title.lower()
    want = "dev"
    if re.search(r"layout|tela|ui\b|ux|design|visual|responsiv", t):
        want = "ui"
    elif re.search(r"arquitet|pipeline|ci/cd|infra|deploy|api\b|integra", t):
        want = "arch"
    keys = {"ui": ("ui-ux", "uiux"), "arch": ("arquitetura", "architect"), "dev": ("fullstack", "dev")}[want]
    for m in team:
        if any(k in (m.get("specialty", "") + " " + m["slug"]).lower() for k in keys):
            return m["slug"]
    return team[0]["slug"] if team else ""


_DEMO_APP = '''import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "<h1>__TITLE__</h1><p>no ar pelo CI/CD local do AI Scrum Flow</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8120"))), H).serve_forever()
'''
_DEMO_MANIFEST = '''name = "demo-app"
[ci]
steps = [["python", "-m", "compileall", "-q", "."]]
[run]
command = ["python", "app.py"]
port = 8120
health = "/"
'''


_DEMO_TEST = """import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import app


def test_handler_exists():
    assert hasattr(app, "H")
"""


def _mock_plan(role: str, task: dict, results: list[str]):
    """Devolve a próxima ação do mock: ('tool', nome, args) ou ('final', texto)."""
    cer, n = task.get("ceremony", "chat"), len(results)

    if cer == "refine":  # PO
        stories = _split_stories(task.get("request", ""))
        if n < len(stories):
            s = stories[n]
            return ("tool", "backlog_create", {
                "title": s[:90], "description": f"Como usuário, quero: {s}",
                "acceptance_criteria": [f"{s[:60]} funciona de ponta a ponta", "Há testes cobrindo o fluxo principal",
                                        "Ações relevantes ficam auditadas"],
                "points": _guess_points(s),
            })
        if n == len(stories):
            return ("tool", "memory_save", {"content": f"Pedido refinado em {len(stories)} histórias: {task.get('request','')[:120]}",
                                           "kind": "decision", "scope": "shared"})
        return ("final", f"Refinei o pedido em {len(stories)} user stories no Product Backlog.")

    if cer == "plan":  # SM
        cap, used, picks = task.get("capacity", 13), 0, []
        for c in task.get("candidates", []):
            if used + c["points"] <= cap:
                picks.append(c); used += c["points"]
        if n < len(picks):
            c = picks[n]
            return ("tool", "sprint_add_item", {"item_id": c["id"], "assignee": _pick_assignee(c["title"], task.get("team", []))})
        return ("final", f"Sprint Backlog montado: {len(picks)} itens, {used}/{cap} pontos.")

    if cer == "implement":  # Developer
        item = task["item"]
        crit = list(item.get("acceptance_criteria", []))
        if crit and (("#qa-fail-once" in item.get("description", "") and task.get("rework", 0) == 0) or "#qa-fail-always" in item.get("description", "")):
            crit = crit[:-1]  # demo/teste: entrega incompleta na 1ª vez para exercitar o retrabalho
        body = f"# {item['key']} — {item['title']}\n\n{item.get('description','')}\n\n## Critérios de aceite atendidos\n" + \
               "\n".join(f"- [x] {c}" for c in crit)
        if task.get("qa_feedback") and "#qa-fail-always" not in item.get("description", ""):
            body += f"\n\n## Correções após QA\n{task['qa_feedback']}"
        app = "#app" in item.get("description", "")  # demo/teste do CI/CD: entrega também uma aplicação executável
        calls = [("write_workspace_file", {"path": f"{item['key']}/entrega.md", "content": body}),
                 ("write_workspace_file", {"path": f"{item['key']}/status.json", "content": json.dumps({"item": item["key"], "criterios": crit}, ensure_ascii=False)})]
        if app:
            calls += [("write_workspace_file", {"path": f"{item['key']}/app.py", "content": _DEMO_APP.replace("__TITLE__", item["title"].replace('"', "'"))}),
                      ("write_workspace_file", {"path": f"{item['key']}/flow-app.toml", "content": _DEMO_MANIFEST}),
                      ("write_workspace_file", {"path": f"{item['key']}/tests/test_app.py", "content": _DEMO_TEST})]
        if "#py" in item.get("description", ""):  # demo/teste: código Python SEM testes (o QA escreve tests/test_qa.py)
            calls.append(("write_workspace_file", {"path": f"{item['key']}/app.py", "content": "def saudacao(nome):\n    return f'Olá, {nome}'\n"}))
        if "#site" in item.get("description", ""):  # demo/teste do deploy estático: só HTML, sem manifesto
            calls.append(("write_workspace_file", {"path": f"{item['key']}/index.html", "content": f"<!doctype html><title>{item['key']}</title><h1>{item['title'].replace('<', '')}</h1>"}))
        calls.append(("work_item_update", {"item_id": item["id"], "progress": 100, "notes": "implementação entregue para QA"}))
        if n < len(calls):
            return ("tool", *calls[n])
        return ("final", f"{item['key']} implementado em {item['key']}/entrega.md.")

    if cer == "tests":  # QA escreve os testes
        item = task["item"]
        if n == 0:
            return ("tool", "write_workspace_file", {"path": task["arquivo_de_teste"], "content":
                    "import pathlib, py_compile\n\n\ndef test_python_compila():\n    for f in pathlib.Path(__file__).resolve().parent.parent.rglob('*.py'):\n        if 'tests' not in f.parts:\n            py_compile.compile(str(f), doraise=True)\n"})
        return ("final", f"Testes de {item['key']} escritos.")

    if cer == "verify":  # QA
        item = task["item"]
        if n == 0:
            return ("tool", "read_workspace_file", {"path": f"{item['key']}/entrega.md"})
        if n == 1:
            content = results[0]
            missing = [c for c in item.get("acceptance_criteria", []) if c not in content]
            ok = bool(content) and not missing and "ERRO" not in content[:20]
            notes = "Todos os critérios de aceite evidenciados." if ok else "Critérios sem evidência: " + "; ".join(missing or ["entregável ausente"])
            return ("tool", "work_item_verdict", {"item_id": item["id"], "passed": ok, "notes": notes})
        return ("final", f"Verificação de {item['key']} concluída.")

    if cer in ("ci", "deploy"):  # DevOps
        dep, tool = task["deployment"], "ci_run" if cer == "ci" else "deploy_app"
        if n == 0:
            return ("tool", tool, {"deployment_id": dep["id"]})
        try:
            r = json.loads(results[0])
            msg = r.get("url") or r.get("detail") or r.get("status", "")
        except (ValueError, AttributeError):
            msg = results[0][:160]
        return ("final", f"{'CI' if cer == 'ci' else 'Deploy'} de {dep['app']} ({dep['release']}): {msg}")

    if cer == "goal":  # PO
        return ("final", "Entregar valor incremental: " + "; ".join(task.get("titles", [])[:3]))

    if cer == "accept":  # PO
        item = task["item"]
        if n == 0:
            return ("tool", "work_item_accept", {"item_id": item["id"], "accepted": True,
                                                "reason": "QA aprovou e os critérios de aceite foram atendidos."})
        return ("final", f"{item['key']} aceito pelo PO.")

    if cer == "retro":  # SM
        st = task.get("stats", {})
        text = f"Entregues {st.get('done',0)}/{st.get('total',0)} itens ({st.get('points',0)} pts); retrabalhos: {st.get('rework',0)}."
        if n == 0:
            return ("tool", "sprint_set_retro", {"text": text})
        if n == 1:
            return ("tool", "memory_save", {"content": "Retro: " + text, "kind": "learning", "scope": "shared", "importance": 4})
        return ("final", "Retrospectiva registrada.")

    return ("final", f"[{role}] mensagem recebida: {str(task.get('text', ''))[:200]}")


def _mock(messages, c: LLMConfig, on_chunk=None):
    if os.environ.get("FLOW_MOCK_DELAY"):  # demo: simula a latência de um modelo real (segundos)
        time.sleep(float(os.environ["FLOW_MOCK_DELAY"]))
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    role = (re.search(r"\[\[role:(\w+)", system) or [None, "developer"])[1]
    idx = max((i for i, m in enumerate(messages) if m["role"] == "user" and not m["content"].startswith("TOOL_RESULT")), default=-1)
    task_msg = messages[idx]["content"] if idx >= 0 else ""
    m = _TASK.search(task_msg)
    try:
        task = json.loads(m.group(1)) if m else {"ceremony": "chat", "text": task_msg}
    except json.JSONDecodeError:
        task = {"ceremony": "chat", "text": task_msg}
    results = [x["content"].split(":", 1)[1].strip() if ":" in x["content"] else x["content"]
               for x in messages[idx + 1:] if x["role"] == "user" and x["content"].startswith("TOOL_RESULT")]
    kind, *rest = _mock_plan(role, task, results)
    out = json.dumps({"tool": rest[0], "args": rest[1]}, ensure_ascii=False) if kind == "tool" else json.dumps({"final": rest[0]}, ensure_ascii=False)
    return out, {"in": sum(estimate_tokens(x["content"]) for x in messages), "out": estimate_tokens(out)}


PROVIDERS = {"ollama": _ollama, "mock": _mock}


def chat(messages: list[dict], cfg: LLMConfig, on_chunk=None):
    """`on_chunk(texto_parcial)` (opcional): chamado enquanto o modelo gera, para mostrar ao vivo o que ele está escrevendo."""
    fn = PROVIDERS.get(cfg.provider)
    if not fn:
        raise LLMError(f"provedor desconhecido: {cfg.provider} (use {', '.join(PROVIDERS)})")
    import inspect
    if on_chunk and "on_chunk" in inspect.signature(fn).parameters:  # provedores simples (ou de teste) não precisam suportar streaming
        return fn(messages, cfg, on_chunk=on_chunk)
    return fn(messages, cfg)
