"""Loop de execução de um agente: contexto → LLM → tool → ... → resposta final."""
import hashlib
import json
import time
from dataclasses import dataclass, field

from . import audit
from . import activity, trace
from . import context as ctxmod
from . import llm
from . import mcp_client
from . import tools as toolreg
from .extensions import db


@dataclass
class RunResult:
    text: str
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: list = field(default_factory=list)
    ok: bool = True
    error: str = ""


def _first_json(text: str):
    """Extrai o 1º objeto JSON balanceado (modelos pequenos às vezes cercam com ```)."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _recover(text: str, task, res: "RunResult", ctx, agent, force: bool = False):
    """Modelo pequeno ANUNCIA a tool ("Chamando work_item_accept…") sem emitir o JSON: o runner executa a chamada
    padrão da cerimônia (`task["recover"]`), sempre auditada como `tool_recovered`. Frases de recusa em `recover_unless` impedem a recuperação."""
    rec = (task.get("recover") if isinstance(task, dict) else None) or {}
    low = text.lower()
    if not rec or any(w in low for w in task.get("recover_unless", [])):
        return None
    for name, args in rec.items():
        if name not in res.tool_calls and (name in text or force):
            audit.record(workflow_id=ctx.workflow_id, actor=agent.slug, event_type="tool_recovered", target=name,
                         payload={"model_text": text[:300], "args": args, "why": "anunciou a tool sem emitir a chamada" if name in text else "ignorou a instrução de agir"})
            return {"tool": name, "args": dict(args)}
    return None


def run(agent, task, *, ctx: toolreg.ToolContext, context_id: str, task_id: str | None = None) -> RunResult:
    """Executa o agente e mantém o painel “quem está fazendo o quê” atualizado."""
    res = None
    activity.start(agent, task)
    cer = task.get("ceremony", "chat") if isinstance(task, dict) else "chat"
    item = (task.get("item") or {}).get("key", "") if isinstance(task, dict) else ""
    run_id = trace.begin(agent.slug, cer, item, title=activity.CEREMONY.get(cer, cer) + (f" · {item}" if item else ""),
                         body=task if isinstance(task, str) else json.dumps(task, ensure_ascii=False, indent=2))
    try:
        res = _run(agent, task, ctx=ctx, context_id=context_id, task_id=task_id)
        return res
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        trace.add(run_id, agent.slug, "error", f"exceção: {type(e).__name__}", body=str(e), state="error")
        raise
    finally:
        if res is not None:
            trace.end(run_id, agent.slug, res.ok, res.text or res.error or "")
        activity.finish(agent, (res.text if res else "falhou"))


def _run(agent, task, *, ctx: toolreg.ToolContext, context_id: str, task_id: str | None = None) -> RunResult:
    """`task` pode ser str (conversa) ou dict (cerimônia: vai dentro de <task>…</task>)."""
    user_text = task if isinstance(task, str) else f"Cerimônia: {task.get('ceremony')}\n<task>{json.dumps(task, ensure_ascii=False)}</task>"
    query = task if isinstance(task, str) else json.dumps(task, ensure_ascii=False)[:500]

    external = mcp_client.list_tools(agent.mcp_servers) if agent.mcp_servers else {}
    external = {k: v for k, v in external.items() if v["tool"] == "__error__" or mcp_client.allowed(agent.tools or [], k)}
    system, hist, meta = ctxmod.build(agent, query=query, external_tools=external, context_id=context_id, task_id=task_id)
    messages = [{"role": "system", "content": system}, *hist, {"role": "user", "content": user_text}]
    cfg = llm.config_for(agent)
    res = RunResult(text="")
    required = list(task.get("requires", [])) if isinstance(task, dict) else []
    seen, repeats, nudges, last_out = set(), 0, 0, ""

    for step in range(1, max(1, agent.max_steps) + 1):
        res.steps = step
        activity.update(agent, f"pensando ({cfg.provider}:{cfg.model}) — passo {step}/{agent.max_steps}", step=step)
        trow, t_llm = trace.add(trace.CURRENT.get(), agent.slug, "think", f"pensando — passo {step}/{agent.max_steps} ({cfg.provider}:{cfg.model})",
                                meta={"step": step, "provider": cfg.provider, "model": cfg.model}, state="running"), time.time()
        try:
            raw, usage = llm.chat(messages, cfg, on_chunk=lambda t, _r=trow: trace.stream(_r, t))
        except llm.LLMError as e:
            trace.update(trow, state="error", title=f"falha no modelo — passo {step}", result=str(e), ms=int((time.time() - t_llm) * 1000))
            audit.record(workflow_id=ctx.workflow_id, actor=agent.slug, event_type="llm_call", status="error",
                         target=f"{cfg.provider}:{cfg.model}", payload={"step": step, "error": str(e)})
            res.ok, res.error, res.text = False, str(e), f"Falha no modelo ({cfg.provider}:{cfg.model}): {e}"
            return res
        res.tokens_in += usage.get("in", 0)
        res.tokens_out += usage.get("out", 0)
        audit.record(workflow_id=ctx.workflow_id, actor=agent.slug, event_type="llm_call", target=f"{cfg.provider}:{cfg.model}",
                     payload={"step": step, "usage": usage, "context": meta,
                              "prompt_sha": hashlib.sha256(system.encode()).hexdigest()[:12], "response": raw})
        messages.append({"role": "assistant", "content": raw})
        obj = _first_json(raw)
        trace.update(trow, state="ok", body=raw, ms=int((time.time() - t_llm) * 1000), meta={"tokens_in": usage.get("in", 0), "tokens_out": usage.get("out", 0)},
                     title=f"modelo respondeu (passo {step}): " + (f"vai usar {obj['tool']}" if obj and "tool" in obj else "vai finalizar" if obj and "final" in obj else "resposta fora do formato JSON"))

        is_chat = isinstance(task, str)
        if obj is None and step == 1 and is_chat:  # conversa livre: texto puro vale como resposta
            res.text = raw.strip()
            return res
        if obj is None or ("final" not in obj and "tool" not in obj):
            obj = _recover(raw, task, res, ctx, agent)
            if obj is None:
                messages.append({"role": "user", "content": "TOOL_RESULT erro: sua resposta não foi um JSON válido. Responda APENAS com um objeto JSON, "
                                 'por exemplo {"tool": "<nome>", "args": {...}} para agir, ou {"final": "<resumo>"} ao terminar. Nada fora do JSON.'})
                continue
        elif "final" in obj and [t for t in required if t not in res.tool_calls]:
            obj = _recover(str(obj["final"]), task, res, ctx, agent, force=nudges >= 1) or obj
        if "final" in obj:
            missing = [t for t in required if t not in res.tool_calls]
            if missing and step < agent.max_steps:  # cerimônia exige agir: não aceita "terminei" sem ter chamado a tool
                nudges += 1
                messages.append({"role": "user", "content": f"TOOL_RESULT erro: você ainda não chamou {missing}. Chame a ferramenta (uma vez por item) e só então finalize."})
                continue
            res.text = str(obj["final"])
            if missing:
                res.ok, res.error = False, f"não executou as ações exigidas: {missing}"
            return res
        name, args = str(obj["tool"]), obj.get("args") or {}
        only = task.get("tools_only") if isinstance(task, dict) else None
        if only is not None and name not in only:  # cerimônia sem ferramentas (ex.: meta da sprint): só a resposta final vale
            messages.append({"role": "user", "content": "TOOL_RESULT erro: nesta tarefa " + (f"só use as ferramentas {only}. " if only else "NÃO use ferramentas. ") +
                             'Responda agora com {"final": "<sua resposta>"}.'})
            continue
        if not isinstance(args, dict):
            args = {}
        spec = toolreg.REGISTRY.get(name)
        key = name + json.dumps(args, sort_keys=True, default=str)
        if spec is not None and not spec.mutating and key in seen:  # leitura idêntica sem nada ter mudado = loop (ex.: SM relistando o backlog)
            repeats += 1
            if repeats >= 2 and all(t in res.tool_calls for t in required):
                res.text = f"(encerrado: a ação '{name}' se repetiu sem novidade) {last_out[:200]}"
                return res
            messages.append({"role": "user", "content": f"TOOL_RESULT erro: você já chamou {name} com estes argumentos e nada mudou desde então. "
                             'Use o resultado que já tem: chame OUTRA ferramenta se faltar algo ou finalize com {"final": "<resumo>"}.'})
            continue
        if spec is not None and not spec.mutating:
            seen.add(key)
        activity.update(agent, activity.describe_tool(name, args))
        trow2, t_tool = trace.add(trace.CURRENT.get(), agent.slug, "tool", activity.describe_tool(name, args),
                                  body=str(args.get("content")) if name == "write_workspace_file" and "content" in args else json.dumps(args, ensure_ascii=False, indent=2, default=str),
                                  meta={"tool": name, **trace.file_meta(name, args)}, state="running"), time.time()
        try:
            if "." in name and name in external:
                if not mcp_client.allowed(agent.tools or [], name):
                    raise toolreg.ToolError(f"tool MCP '{name}' não habilitada")
                result = mcp_client.call_tool(agent.mcp_servers, name, args)
                audit.record(workflow_id=ctx.workflow_id, actor=agent.slug, event_type="mcp_call", target=name,
                             payload={"args": args, "result": result})
            else:
                result = toolreg.call(name, ctx, args)
            res.tool_calls.append(name)
            out = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            last_out = out
            if spec is None or spec.mutating:
                seen.clear()
                repeats = 0
        except toolreg.ToolError as e:
            db.session.rollback()
            out = f"ERRO: {e}"
        except Exception as e:  # noqa: BLE001 — tool com bug não derruba o loop
            db.session.rollback()
            audit.record(workflow_id=ctx.workflow_id, actor=agent.slug, event_type="tool_call", status="error",
                         target=name, payload={"args": args, "exception": repr(e)})
            out = f"ERRO interno: {e}"
        trace.update(trow2, state="error" if out.startswith("ERRO") else "ok", result=out, ms=int((time.time() - t_tool) * 1000))
        messages.append({"role": "user", "content": f"TOOL_RESULT {name}: {out[:6000]}"})

    res.ok, res.error = False, "max_steps atingido"
    res.text = f"Não concluí em {agent.max_steps} passos."
    return res
