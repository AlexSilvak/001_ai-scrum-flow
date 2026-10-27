"""Orquestração Scrum: o Product Owner conduz, o Scrum Master facilita, o Dev Team entrega.

    humano ─▶ PO ──A2A──▶ SM ──A2A──▶ Developers (arquiteto/dev/UI) ──▶ QA ──▶ PO aceita
                                          (tudo via MCP-tools + A2A, tudo auditado)

Cerimônias: refine → plan → execute (implement/verify/accept, com retrabalho) → review → retro.
O fluxo é determinístico (código); as DECISÕES (histórias, seleção, entrega, veredito, aceite) são dos agentes/LLMs.
"""
from . import a2a, audit, cd, gitflow, sandbox
from .extensions import db
from . import stages
from .models import AgentConfig, AuditEvent, Role, Sprint, WorkItem, now
from .tools import ToolContext

MAX_REWORK = 2
SYSTEM = "system"  # o disparo humano/orquestrador externo; conversa com o PO sem restrição de hierarquia


GOAL_STOP = {"meta", "sprint", "desta", "esta", "este", "para", "com", "uma", "que", "dos", "das", "nos", "nas", "entregar", "implementar", "criar", "cria", "fazer",
             "desenvolver", "construir", "usando", "utilizando", "incremental", "valor", "como", "sera", "foco", "objetivo", "entrega", "tela", "sistema"}


def goal_matches(goal: str, items) -> bool:
    """A meta precisa falar das histórias DESTA sprint: >= 40% dos termos relevantes dela aparecem nos títulos/descrições/critérios do backlog."""
    from .memory import _terms
    gt = {t for t in _terms(goal) if len(t) >= 4 and t not in GOAL_STOP}
    if not gt:
        return False
    pool = set()
    for i in items:
        pool |= _terms(f"{i.title} {i.description or ''} {' '.join(i.acceptance_criteria or [])}")
    return len(gt & pool) / len(gt) >= 0.4


class SprintRunner:
    def __init__(self, sprint: Sprint, user_email: str = "system", should_stop=lambda: False):
        self.sprint = sprint
        self.user = user_email
        self.should_stop = should_stop  # pausa cooperativa (no worker: `context.should_abort` do job)
        self.po = self._one(Role.PO)
        self.sm = self._one(Role.SM)

    def _one(self, role) -> AgentConfig:
        a = db.session.query(AgentConfig).filter_by(scrum_role=role.value, enabled=True).first()
        if not a:
            raise RuntimeError(f"nenhum agente {role.value} habilitado — rode `flask --app flow seed`")
        return a

    @property
    def ctx(self):
        return ToolContext(agent=self.po, workflow_id=self.sprint.workflow_id, user_email=self.user, sprint_id=self.sprint.id)

    def _log(self, event, **payload):
        audit.record(workflow_id=self.sprint.workflow_id, actor=self.po.slug, event_type="ceremony", target=event, payload=payload)

    def _send(self, sender, target, text, data, enforce=True):
        task = a2a.send_message(sender, target, text, data=data, ctx=self.ctx, context_id=f"{self.sprint.workflow_id}", enforce=enforce)
        return task

    # ---------------------------------------------------------------- cerimônias
    def refine(self, request: str):
        before = {i for (i,) in db.session.query(WorkItem.id)}
        before_ev = db.session.query(db.func.max(AuditEvent.id)).scalar() or 0
        self._log("refine", request=request)
        t = self._send(SYSTEM, self.po.slug, f"Refine este pedido em user stories: {request}", {"ceremony": "refine", "request": request, "requires": ["backlog_create"]}, enforce=False)
        new = [i for i in db.session.query(WorkItem).order_by(WorkItem.id) if i.id not in before]
        if not new:  # tudo que o PO tentou criar já existia: o pedido já está coberto pelo backlog (não é falha)
            keys = {(e.payload.get("result") or {}).get("duplicate_of") for e in db.session.query(AuditEvent).filter(
                AuditEvent.id > before_ev, AuditEvent.event_type == "tool_call", AuditEvent.target == "backlog_create") if isinstance(e.payload.get("result"), dict)}
            existing = [i for i in db.session.query(WorkItem).order_by(WorkItem.id) if i.key in keys]
            if existing:
                self._log("refine_duplicates", existing=[i.key for i in existing])
                return existing, f"O pedido já está coberto por: {', '.join(i.key for i in existing)}."
            raise RuntimeError(f"o PO não criou nenhuma user story (estado: {t['status']['state'].removeprefix('TASK_STATE_')}; "
                               f"resposta: {a2a.reply_text(t)[:120]!r}). {self._why(before_ev)}Tente de novo ou use um modelo maior.")
        return new, a2a.reply_text(t)

    def _why(self, after_event_id: int) -> str:
        """Diagnóstico para o erro de refinamento: erros de ferramenta e a última resposta bruta do modelo (vem da auditoria)."""
        evs = db.session.query(AuditEvent).filter(AuditEvent.id > after_event_id, AuditEvent.actor == self.po.slug).order_by(AuditEvent.id).all()
        errs = [str((e.payload.get("error") or e.payload.get("reason") or e.payload.get("exception") or ""))[:110] for e in evs
                if e.event_type == "tool_call" and e.status != "ok"]
        last = next((str(e.payload.get("response", ""))[:140] for e in reversed(evs) if e.event_type == "llm_call"), "")
        out = (f"Erros de ferramenta: {errs[-2:]}. " if errs else "") + (f"Última resposta do modelo: {last!r}. " if last else "")
        return out

    def plan(self):
        s = self.sprint
        backlog = db.session.query(WorkItem).filter_by(status=WorkItem.BACKLOG).order_by(WorkItem.rank, WorkItem.id).limit(15).all()
        if not backlog:
            raise RuntimeError("Product Backlog vazio — refine um pedido antes de planejar")
        self._log("plan_start", capacity=s.capacity, candidates=[i.key for i in backlog])
        if not s.goal:
            titles = [i.title for i in backlog]
            t = self._send(SYSTEM, self.po.slug, "Defina a meta (sprint goal) em uma frase.",
                           {"ceremony": "goal", "titles": titles, "tools_only": [],
                            "dica": 'responda direto {"final": "<meta em uma frase>"} usando APENAS estas histórias (titles). Não use memória nem ferramentas.'}, enforce=False)
            goal = a2a.reply_text(t)[:500].strip()
            if not goal_matches(goal, backlog):  # o modelo repetiu a meta de uma sprint antiga / inventou: usa as histórias reais
                fallback = "Entregar: " + "; ".join(titles[:3])
                self._log("goal_overridden", model_goal=goal, goal=fallback, why="a meta não menciona nenhuma das histórias do backlog")
                goal = fallback
            s.goal = goal[:500]
            db.session.commit()
        devs = db.session.query(AgentConfig).filter_by(scrum_role=Role.DEV.value, enabled=True).all()
        team = [{"slug": a.slug, "specialty": a.specialty} for a in devs if not any(w in a.specialty.lower() for w in ("qa", "devops"))]
        fits = [i for i in backlog if i.points <= s.capacity]
        data = {"ceremony": "plan", "sprint_id": s.id, "capacity": s.capacity, "goal": s.goal, "team": team,
                "candidates": [{"id": i.id, "key": i.key, "title": i.title, "points": i.points} for i in backlog],
                "requires": ["sprint_add_item"] if fits else []}
        t = self._send(self.po.slug, self.sm.slug, f"Planeje a sprint (capacidade {s.capacity} pts). Meta: {s.goal}", data)
        db.session.refresh(s)
        if not s.items:  # nunca avança com Sprint Backlog vazio (antes encerrava a sprint “com sucesso” sem fazer nada)
            self._log("plan_failed", sm_reply=a2a.reply_text(t))
            raise RuntimeError("o Scrum Master não montou o Sprint Backlog (nenhum item foi movido para a sprint). "
                               f"Resposta dele: {a2a.reply_text(t)[:120]!r}. Tente de novo ou use um modelo maior." if fits else
                               f"nenhum item do backlog cabe na capacidade de {s.capacity} pts — aumente a capacidade ou quebre as histórias.")
        s.status = "active"
        db.session.commit()
        self._log("plan_done", sm_reply=a2a.reply_text(t), items=[i.key for i in s.items])
        gitflow.begin_sprint(s, list(s.items), self.sm.slug)

    def _qa_for(self, item: WorkItem) -> AgentConfig | None:
        devs = [a for a in db.session.query(AgentConfig).filter_by(scrum_role=Role.DEV.value, enabled=True).order_by(AgentConfig.id)
                if a.id != item.assignee_id and "devops" not in a.specialty.lower()]
        return next((a for a in devs if "qa" in a.specialty.lower()), devs[0] if devs else None)

    def _files(self, item) -> list[str]:
        base = gitflow.root() / self.sprint.workflow_id / item.key if gitflow.enabled() else None
        if base is None:
            from flask import current_app
            from pathlib import Path
            base = Path(current_app.config["FLOW_WORKSPACE"]) / self.sprint.workflow_id / item.key
        return sorted(f"{item.key}/{f.relative_to(base).as_posix()}" for f in base.rglob("*") if f.is_file())[:30] if base.is_dir() else []

    def _item_data(self, item, **extra):
        return {"id": item.id, "key": item.key, "title": item.title, "description": item.description,
                "acceptance_criteria": item.acceptance_criteria, "points": item.points, **extra}

    def execute_item(self, item: WorkItem):
        qa = self._qa_for(item)
        if not item.assignee or not qa:
            item.status, item.notes = WorkItem.REJECTED, "impedimento: sem developer/QA disponível"
            db.session.commit()
            self._log("impediment", item=item.key, reason=item.notes)
            return
        feedback = ""
        gitflow.begin_item(item, self.sm.slug)
        for rework in range(MAX_REWORK + 1):
            db.session.refresh(item)
            item.status, item.qa_verdict = WorkItem.PROGRESS, None
            db.session.commit()
            stages.mark(item, "implement", "active", item.assignee.slug, f"retrabalho {rework}: {feedback}"[:300] if rework else "em desenvolvimento")
            self._send(self.sm.slug, item.assignee.slug, f"Implemente {item.key}: {item.title}",
                       {"ceremony": "implement", "item": self._item_data(item), "rework": rework, "qa_feedback": feedback, "passo_a_passo": [f"1. escreva primeiro os arquivos REAIS da solução em {item.key}/ (ex.: {item.key}/index.html ou {item.key}/app.py), um write_workspace_file por arquivo",
                                         f"2. só no fim, escreva {item.key}/entrega.md curto (o que foi feito e em quais arquivos)", '3. finalize com {"final": "<resumo>"}'],
                        "requires": ["write_workspace_file"],
                        "dica": "se a história entrega uma aplicação executável, inclua também flow-app.toml (skill app-manifest) para o CI/CD subi-la"})
            db.session.refresh(item)
            item.status = WorkItem.QA
            db.session.commit()
            stages.mark(item, "implement", "done", item.assignee.slug, f"entregue para QA" + (f" (retrabalho {rework})" if rework else ""))
            gitflow.commit_stage(item, "implement", item.assignee.slug, (item.notes or "entrega para QA") + (f"\n\nretrabalho {rework}: {feedback}" if rework else ""), rework=rework)
            stages.mark(item, "verify", "active", qa.slug, "verificando critérios de aceite")
            verdict, rep = None, None
            msg = sandbox.precheck(item)
            if msg:  # só documentação: volta ao dev sem gastar o QA
                verdict = {"passed": False, "notes": msg, "by": "sandbox"}
            elif sandbox.enabled():
                if sandbox.needs_qa_tests(item):  # o QA escreve os testes (lendo o código entregue e os critérios) ANTES de rodar o pytest
                    err = None
                    for attempt in range(3):  # teste do QA com erro de sintaxe não é culpa do dev: o QA corrige (até 3 tentativas)
                        self._send(self.sm.slug, qa.slug, f"Escreva os testes pytest de {item.key}" + (f" (tentativa {attempt + 1})" if attempt else ""),
                                   {"ceremony": "tests", "item": self._item_data(item), "arquivos_entregues": self._files(item), "arquivo_de_teste": f"{item.key}/tests/test_qa.py",
                                    "requires": ["write_workspace_file"], **({"erro_da_tentativa_anterior": err} if err else {}),
                                    "dica": "leia o código entregue, depois escreva UM arquivo pytest com um teste por critério de aceite (skill qa-test-writing). Flask: app.test_client(); tela/navegador: Playwright. "
                                            "Python válido: em bytes use só ASCII (b'invalido') ou compare com str (resp.get_data(as_text=True))"},
                                   enforce=False)
                        err = sandbox.qa_test_error(item)
                        if not err:
                            break
                    if err:
                        item.status, item.notes, item.qa_verdict = WorkItem.REJECTED, f"impedimento: o QA não conseguiu escrever um teste válido — {err}", None
                        db.session.commit()
                        stages.mark(item, "verify", "failed", qa.slug, item.notes)
                        self._log("impediment", item=item.key, reason=item.notes)
                        gitflow.reject_item(item, qa.slug, item.notes)
                        return
                rep = sandbox.run(item, actor=qa.slug, own=True)
                if sandbox.setup_failure(rep):  # problema do ambiente, não do código: não adianta retrabalho
                    item.status, item.notes, item.qa_verdict = WorkItem.REJECTED, f"impedimento: sandbox indisponível — {rep['summary']}", None
                    db.session.commit()
                    stages.mark(item, "verify", "failed", qa.slug, item.notes)
                    self._log("impediment", item=item.key, reason=item.notes)
                    gitflow.reject_item(item, qa.slug, item.notes)
                    return
                if not rep["ok"]:
                    verdict = {"passed": False, "by": "sandbox", "notes": f"pytest reprovou ({rep['isolation']}): {rep['summary']}\n{rep['output'][-800:]}"}
            if verdict is None:
                self._send(self.sm.slug, qa.slug, f"Verifique {item.key} contra os critérios de aceite.",
                           {"ceremony": "verify", "item": self._item_data(item), "arquivos_entregues": self._files(item), "pytest": rep and {k: rep.get(k) for k in ("ok", "summary", "isolation")},
                            "dica": "o pytest já passou no sandbox (campo pytest). Leia CADA arquivo listado em arquivos_entregues com read_workspace_file (caminho completo) e confira os critérios; então chame work_item_verdict",
                            "requires": ["work_item_verdict"]})
                db.session.refresh(item)
                verdict = item.qa_verdict or {"passed": False, "notes": "QA não registrou veredito"}
            else:
                item.qa_verdict = verdict
                db.session.commit()
            stages.mark(item, "verify", "done" if verdict["passed"] else "failed", qa.slug, verdict.get("notes", "")[:300])
            gitflow.commit_stage(item, "verify", qa.slug, verdict.get("notes", ""), result="passed" if verdict["passed"] else "failed", rework=rework)
            if verdict["passed"]:
                break
            feedback = verdict.get("notes", "")
            item.rework_count += 1
            db.session.commit()
            self._log("rework", item=item.key, attempt=rework + 1, feedback=feedback)
        db.session.refresh(item)
        if (item.qa_verdict or {}).get("passed"):
            self._send(SYSTEM, self.po.slug, f"Sprint Review: aceite {item.key}?", {"ceremony": "accept", "item": self._item_data(item), "qa": item.qa_verdict, "requires": ["work_item_accept"], "tools_only": ["work_item_accept", "read_workspace_file", "list_workspace"],
                 "recover": {"work_item_accept": {"item_id": item.id, "accepted": True, "reason": "QA aprovou e os critérios de aceite foram atendidos (aceite registrado pelo runner: o PO anunciou o aceite sem emitir a chamada)."}},
                 "recover_unless": ["rejeit", "reprov", "recus", "não aceit", "nao aceit", "não aceito"]}, enforce=False)
            db.session.refresh(item)
            if item.status == WorkItem.DONE:
                gitflow.accept_item(item, self.po.slug, item.notes)
            else:  # PO não aceitou: a branch fica sem merge como evidência
                gitflow.reject_item(item, self.po.slug, item.notes or "PO não registrou o aceite")
        else:
            item.status = WorkItem.REJECTED
            item.notes = f"reprovado pelo QA após {MAX_REWORK} retrabalhos: {feedback}"
            db.session.commit()
            gitflow.reject_item(item, qa.slug, item.notes)

    def execute(self):
        items = db.session.query(WorkItem).filter_by(sprint_id=self.sprint.id, status=WorkItem.SPRINT).order_by(WorkItem.rank, WorkItem.id).all()
        for item in items:
            if self.should_stop():
                self._log("paused")
                return False
            self.execute_item(item)
        return True

    def close(self):
        s = self.sprint
        db.session.refresh(s)
        s.status = "review"
        db.session.commit()
        items = list(s.items)
        done = [i for i in items if i.status == WorkItem.DONE]
        stats = {"total": len(items), "done": len(done), "points": sum(i.points for i in done),
                 "rework": sum(i.rework_count for i in items)}
        default = f"Entregues {stats['done']}/{stats['total']} itens ({stats['points']} pts); retrabalhos: {stats['rework']}."
        self._send(self.po.slug, self.sm.slug, "Facilite a retrospectiva da sprint.",
                   {"ceremony": "retro", "stats": stats, "requires": ["sprint_set_retro"], "tools_only": ["sprint_set_retro", "memory_save"], "recover": {"sprint_set_retro": {"text": default}}})
        db.session.refresh(s)
        if not (s.retro or "").strip():  # o SM não registrou (modelo pequeno): a sprint não fica sem retrospectiva
            s.retro = default + " (registrada automaticamente: o Scrum Master não chamou sprint_set_retro)"
            db.session.commit()
            self._log("retro_autofilled")
        db.session.refresh(s)
        rel = gitflow.close_sprint(s, stats, s.retro, self.sm.slug, self.po.slug)
        for i in items:  # o que não ficou pronto volta ao topo do backlog (decisão do PO na próxima refinement)
            if i.status != WorkItem.DONE:
                i.status, i.sprint_id, i.assignee_id, i.qa_verdict, i.progress = WorkItem.BACKLOG, None, None, None, 0
        db.session.refresh(s)
        s.velocity, s.status, s.ended_at = stats["points"], "done", now()
        db.session.commit()
        self._log("sprint_closed", **stats)
        if rel and stats["done"]:
            self.release(rel["tag"], rel["sha"])
        return stats

    # ---------------------------------------------------------------- release (CI/CD local, agente devops)
    def release(self, tag: str, sha: str):
        """Tag da sprint em main → CI pelo agente devops → (aprovação humana) → o agente sobe a aplicação."""
        try:
            deps = cd.prepare(self.sprint, tag, sha)
            devops = cd.devops_agent()
            for d in deps:
                if d.status != "ci_pending":
                    continue
                if not devops:
                    cd._set(d, "skipped", "nenhum agente devops habilitado — rode `flask --app flow seed --sync`")
                    continue
                cd.ceremony(devops, d, "ci", self.ctx, SYSTEM)
                db.session.refresh(d)
                if d.status == "awaiting_approval" and d.approved_by == "auto":
                    cd.ceremony(devops, d, "deploy", self.ctx, SYSTEM)
        except Exception as e:  # noqa: BLE001 — o CD nunca invalida a sprint já encerrada
            db.session.rollback()
            audit.record(workflow_id=self.sprint.workflow_id, actor="system", event_type="cd_error", status="error", payload={"error": repr(e)})

    def run(self):
        """Planeja (se preciso), executa e encerra a sprint. Idempotente: o job pode ser repetido (retry/worker caiu) e retoma."""
        if self.sprint.status == "planning":
            self.plan()
        elif self.sprint.status == "active":  # retomada: o que estava no meio (progress/qa) volta para o Sprint Backlog
            for i in db.session.query(WorkItem).filter(WorkItem.sprint_id == self.sprint.id, WorkItem.status.in_([WorkItem.PROGRESS, WorkItem.QA])):
                i.status = WorkItem.SPRINT
            db.session.commit()
        if self.execute():
            return self.close()


# ---------------------------------------------------------------- estado vindo da fila (procrastinate)
def is_running(sprint_id: int) -> bool:
    from . import queue
    return queue.is_running(sprint_id)


def refining(sprint_id: int) -> bool:
    from . import queue
    return queue.refining(sprint_id)


def busy() -> bool:
    from . import queue
    return queue.busy()
