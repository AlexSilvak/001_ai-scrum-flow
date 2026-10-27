"""Modelos SQLAlchemy 2.0 (PostgreSQL)."""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db


def now():
    """UTC *naive*: as colunas são TIMESTAMP sem fuso e o PostgreSQL devolve exatamente o que gravamos
    (um datetime aware seria convertido para o fuso da sessão e quebraria o hash da auditoria)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(str, enum.Enum):
    PO = "product_owner"
    SM = "scrum_master"
    DEV = "developer"


ROLE_LABEL = {Role.PO.value: "Product Owner", Role.SM.value: "Scrum Master", Role.DEV.value: "Developer"}

DEFAULT_CONTEXT = {
    "max_tokens": 6000,           # orçamento (estimado) do prompt montado
    "history_messages": 6,         # turnos anteriores do mesmo contextId
    "memory_k": 5,                 # memórias recuperadas por chamada
    "include_shared_memory": True,
    "context_paths": [],           # arquivos/pastas injetados no prompt
}


class AgentConfig(db.Model):
    __tablename__ = "agent_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    emoji: Mapped[str] = mapped_column(String(8), default="🤖")
    scrum_role: Mapped[str] = mapped_column(String(20), default=Role.DEV.value)
    specialty: Mapped[str] = mapped_column(String(60), default="")
    reports_to_id: Mapped[int | None] = mapped_column(ForeignKey("agent_config.id"), nullable=True)
    can_delegate_to: Mapped[list] = mapped_column(JSON, default=list)  # slugs permitidos via A2A
    system_prompt: Mapped[str] = mapped_column(Text, default="")

    # modelo configurável (Ollama; "mock" roda offline para testes/demo)
    provider: Mapped[str] = mapped_column(String(20), default="ollama")
    model: Mapped[str] = mapped_column(String(120), default="gemma3:4b")
    base_url: Mapped[str] = mapped_column(String(200), default="")
    temperature: Mapped[float] = mapped_column(default=0.2)
    max_output_tokens: Mapped[int] = mapped_column(default=1500)
    timeout_s: Mapped[int] = mapped_column(default=120)

    # tools / skills / contexto configuráveis
    tools: Mapped[list] = mapped_column(JSON, default=list)          # allowlist de tools internas
    mcp_servers: Mapped[dict] = mapped_column(JSON, default=dict)    # servidores MCP externos
    max_steps: Mapped[int] = mapped_column(default=6)
    skills_path: Mapped[str] = mapped_column(String(500), default="")  # pastas separadas por ';' ou ':'
    skills_allow: Mapped[list] = mapped_column(JSON, default=list)   # [] = todas
    context: Mapped[dict] = mapped_column(JSON, default=dict)

    a2a_url: Mapped[str] = mapped_column(String(300), default="")    # vazio = agente local
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    def ctx(self) -> dict:
        return {**DEFAULT_CONTEXT, **(self.context or {})}

    @property
    def role_label(self):
        return ROLE_LABEL.get(self.scrum_role, self.scrum_role)

    def to_dict(self):
        cols = ["id", "slug", "name", "emoji", "scrum_role", "specialty", "reports_to_id", "can_delegate_to", "system_prompt",
                "provider", "model", "base_url", "temperature", "max_output_tokens", "timeout_s", "tools", "mcp_servers",
                "max_steps", "skills_path", "skills_allow", "context", "a2a_url", "enabled"]
        d = {c: getattr(self, c) for c in cols}
        d["role_label"], d["context"] = self.role_label, self.ctx()
        return d


class Sprint(db.Model):
    __tablename__ = "sprint"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="planning")  # planning|active|review|done
    capacity: Mapped[int] = mapped_column(default=13)
    velocity: Mapped[int] = mapped_column(default=0)
    retro: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    items = relationship("WorkItem", back_populates="sprint")

    @property
    def workflow_id(self):
        return f"sprint-{self.id}"


class WorkItem(db.Model):
    __tablename__ = "work_item"
    BACKLOG, SPRINT, PROGRESS, QA, DONE, REJECTED = "backlog", "sprint", "progress", "qa", "done", "rejected"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    acceptance_criteria: Mapped[list] = mapped_column(JSON, default=list)
    points: Mapped[int] = mapped_column(default=3)
    rank: Mapped[int] = mapped_column(default=0, index=True)
    status: Mapped[str] = mapped_column(String(10), default="backlog", index=True)
    sprint_id: Mapped[int | None] = mapped_column(ForeignKey("sprint.id"), nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("agent_config.id"), nullable=True)
    progress: Mapped[int] = mapped_column(default=0)
    rework_count: Mapped[int] = mapped_column(default=0)
    qa_verdict: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {passed, notes, by}
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(120), default="human")
    created_at: Mapped[datetime] = mapped_column(default=now)
    sprint = relationship("Sprint", back_populates="items")
    assignee = relationship("AgentConfig")

    @property
    def key(self):
        return f"US-{self.id:03d}"

    def to_dict(self):
        return {"id": self.id, "key": self.key, "title": self.title, "description": self.description,
                "acceptance_criteria": self.acceptance_criteria, "points": self.points, "rank": self.rank,
                "status": self.status, "sprint_id": self.sprint_id,
                "assignee": self.assignee.slug if self.assignee else None,
                "assignee_name": self.assignee.name if self.assignee else None,
                "assignee_emoji": self.assignee.emoji if self.assignee else None,
                "progress": self.progress, "rework_count": self.rework_count, "qa_verdict": self.qa_verdict,
                "notes": self.notes}


class AgentMemory(db.Model):
    __tablename__ = "agent_memory"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent_config.id", ondelete="CASCADE"), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(10), default="agent")  # agent (privada) | shared (time)
    kind: Mapped[str] = mapped_column(String(20), default="fact")    # fact|episode|learning|decision
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    importance: Mapped[int] = mapped_column(default=3)
    sprint_id: Mapped[int | None] = mapped_column(ForeignKey("sprint.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    agent = relationship("AgentConfig")


class A2ATask(db.Model):
    """Task do protocolo A2A (o agente é o servidor; quem enviou a mensagem é o cliente)."""
    __tablename__ = "a2a_task"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    context_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent_config.id"), index=True)
    state: Mapped[str] = mapped_column(String(30), default="TASK_STATE_SUBMITTED", index=True)
    status_message: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    agent = relationship("AgentConfig")


class AuditEvent(db.Model):
    """Auditoria append-only com cadeia de hash (tamper-evident)."""
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ok")  # ok | denied | error
    target: Mapped[str] = mapped_column(String(200), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    def to_dict(self):
        return {"id": self.id, "workflow_id": self.workflow_id, "actor": self.actor, "event_type": self.event_type,
                "status": self.status, "target": self.target, "payload": self.payload,
                "created_at": self.created_at.isoformat(), "hash": self.hash[:12]}


class ItemStage(db.Model):
    """Histórico das etapas de cada item (alimenta a linha do tempo). Cada transição é uma linha; vale a mais recente de cada etapa."""
    __tablename__ = "item_stage"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("work_item.id", ondelete="CASCADE"), index=True)
    sprint_id: Mapped[int | None] = mapped_column(ForeignKey("sprint.id"), nullable=True)  # None = refinamento (vale para sempre)
    stage: Mapped[str] = mapped_column(String(20))      # refine|plan|implement|verify|accept
    state: Mapped[str] = mapped_column(String(10))      # active|done|failed
    actor: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=now)


class AgentActivity(db.Model):
    """O que cada agente está fazendo AGORA (uma linha por agente; funciona com vários processos/workers)."""
    __tablename__ = "agent_activity"
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent_config.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(String(10), default="idle")   # idle | working
    ceremony: Mapped[str] = mapped_column(String(20), default="")
    item_key: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    step: Mapped[int] = mapped_column(default=0)
    max_steps: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=now)
    last_result: Mapped[str] = mapped_column(Text, default="")


class AgentTrace(db.Model):
    """Passo a passo de cada execução de um agente (o que o modelo gera, arquivos lidos/escritos, instalação, testes) — alimenta o painel “Detalhe da tarefa”."""
    __tablename__ = "agent_trace"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_slug: Mapped[str] = mapped_column(String(60), index=True)
    ceremony: Mapped[str] = mapped_column(String(20), default="")
    item_key: Mapped[str] = mapped_column(String(20), default="")
    kind: Mapped[str] = mapped_column(String(12), default="tool")    # task | think | tool | phase | test | final | error
    state: Mapped[str] = mapped_column(String(10), default="ok")     # running | ok | error
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")              # o que foi enviado/gerado (conteúdo do arquivo, JSON do modelo, argumentos)
    result: Mapped[str] = mapped_column(Text, default="")            # o que voltou (resultado da ferramenta, saída do pytest)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)           # path, name, ext, bytes, step, tokens, isolamento…
    ms: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, index=True)


class Deployment(db.Model):
    """Uma release (tag `sprint-N`) indo pelo CI/CD local: CI → aprovação → agente devops sobe a aplicação."""
    __tablename__ = "deployment"
    id: Mapped[int] = mapped_column(primary_key=True)
    sprint_id: Mapped[int | None] = mapped_column(ForeignKey("sprint.id"), nullable=True, index=True)
    tag: Mapped[str] = mapped_column(String(60), default="")
    commit: Mapped[str] = mapped_column(String(40), default="")
    app: Mapped[str] = mapped_column(String(80), default="")
    manifest_path: Mapped[str] = mapped_column(String(300), default="")   # caminho do flow-app.toml no repositório
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    # ci_pending|ci_running|ci_failed|awaiting_approval|deploying|running|failed|stopped|skipped
    status: Mapped[str] = mapped_column(String(20), default="ci_pending", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    ci_report: Mapped[list] = mapped_column(JSON, default=list)
    workdir: Mapped[str] = mapped_column(String(500), default="")
    port: Mapped[int | None] = mapped_column(nullable=True)
    pid: Mapped[int | None] = mapped_column(nullable=True)
    url: Mapped[str] = mapped_column(String(200), default="")
    approved_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    def to_dict(self):
        return {"id": self.id, "sprint_id": self.sprint_id, "tag": self.tag, "commit": self.commit[:7], "app": self.app, "status": self.status,
                "detail": self.detail, "ci_report": self.ci_report, "port": self.port, "pid": self.pid, "url": self.url,
                "approved_by": self.approved_by, "manifest_path": self.manifest_path, "run": (self.manifest or {}).get("run", {}).get("command"),
                "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat()}
