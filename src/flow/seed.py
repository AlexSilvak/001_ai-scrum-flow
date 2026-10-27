"""Time Scrum padrão (PO → SM → Developers), definido em `team.toml`."""
import tomllib
from pathlib import Path

from flask import current_app

from .extensions import db
from .models import AgentConfig


def load_team(path=None) -> list[dict]:
    path = Path(path or current_app.config["FLOW_TEAM_FILE"])
    return tomllib.loads(path.read_text(encoding="utf-8")).get("agent", [])


def seed(reset=False, provider=None, model=None, sync=False) -> int:
    provider = provider or current_app.config["FLOW_DEFAULT_PROVIDER"]
    model = model or current_app.config["FLOW_DEFAULT_MODEL"]
    base_url = ""  # vazio = usa OLLAMA_BASE_URL da configuração
    team = load_team()
    if reset:
        db.session.query(AgentConfig).update({AgentConfig.reports_to_id: None})
        db.session.query(AgentConfig).delete()
        db.session.commit()
    agents = {}
    for spec in team:
        spec = dict(spec)
        spec.pop("reports_to", None)
        agent = db.session.query(AgentConfig).filter_by(slug=spec["slug"]).first()
        if agent and sync:  # só campos de “papel” do team.toml; provider/model/base_url/temperature (sua config) ficam intactos
            for k in ("name", "emoji", "specialty", "system_prompt", "tools", "can_delegate_to", "skills_path"):
                if k in spec:
                    setattr(agent, k, spec[k])
        if not agent:
            agent = AgentConfig(**{"provider": provider, "model": model, "base_url": base_url, **spec})
            db.session.add(agent)
            db.session.flush()
        agents[spec["slug"]] = agent
    for spec in team:
        boss = spec.get("reports_to")
        if boss and boss in agents and not agents[spec["slug"]].reports_to_id:
            agents[spec["slug"]].reports_to_id = agents[boss].id
    db.session.commit()
    return len(agents)


# Ferramentas sem as quais uma etapa do pipeline falha em silêncio. Bancos criados por versões anteriores não as têm
# (o time mora no banco; o team.toml só semeia). Só ADICIONA — nunca remove o que você configurou.
REQUIRED_TOOLS = {"qa": ["write_workspace_file", "qa_run_tests", "read_workspace_file", "work_item_verdict"]}


MIN_DEV_STEPS = 12


def ensure_required_tools() -> list[str]:
    from . import audit
    added = []
    for a in db.session.query(AgentConfig):
        want = [t for key, tools in REQUIRED_TOOLS.items() if key in (a.specialty or "").lower() for t in tools]
        missing = [t for t in want if t not in (a.tools or [])]
        if missing:
            a.tools = [*(a.tools or []), *missing]
            db.session.commit()
            audit.record(workflow_id="system", actor="system", event_type="agent_config_change", target=a.slug,
                         payload={"auto": "ferramentas exigidas pelo pipeline adicionadas", "added": missing})
            added += [f"{a.slug}:{t}" for t in missing]
    for a in db.session.query(AgentConfig).filter_by(scrum_role="developer"):  # 6 passos não bastam para app.py + templates + requirements + manifesto + entrega.md
        spec = (a.specialty or "").lower()
        if not any(w in spec for w in ("qa", "devops")) and (a.max_steps or 0) < MIN_DEV_STEPS:
            old, a.max_steps = a.max_steps, MIN_DEV_STEPS
            db.session.commit()
            audit.record(workflow_id="system", actor="system", event_type="agent_config_change", target=a.slug,
                         payload={"auto": "máx. de passos do desenvolvedor elevado (só aumenta)", "max_steps": [old, MIN_DEV_STEPS]})
            added.append(f"{a.slug}:max_steps={MIN_DEV_STEPS}")
    return added
