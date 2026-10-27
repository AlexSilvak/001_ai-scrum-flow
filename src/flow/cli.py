"""Comandos:  uv run flask --app flow <comando>"""
import click
from flask import current_app


def register_cli(app):
    @app.cli.command("seed")
    @click.option("--reset", is_flag=True, help="apaga e recria o time padrão")
    @click.option("--sync", is_flag=True, help="atualiza tools/prompts/delegação do time existente a partir do team.toml, sem mexer em provedor/modelo")
    @click.option("--provider", default=None, help="ollama | mock")
    @click.option("--model", default=None, help="ex.: gemma3:4b")
    def seed_cmd(reset, sync, provider, model):
        """Cria o time Scrum padrão (PO → SM → Developers)."""
        from .seed import seed

        n = seed(reset=reset, provider=provider, model=model, sync=sync)
        click.echo(f"{n} agentes prontos.")

    @app.cli.command("worker")
    @click.option("--concurrency", type=int, default=None, help="jobs simultâneos (padrão: [worker] concurrency)")
    def worker_cmd(concurrency):
        """Worker da fila (refinamento, sprint, CI e deploy). Rode UM por máquina — quem sobe a aplicação é o processo dono dela."""
        from . import queue

        click.echo("worker ouvindo a fila (Ctrl+C encerra: jobs em andamento terminam ou voltam para a fila)…")
        queue.run_worker(concurrency)

    @app.cli.command("queue-status")
    def queue_status_cmd():
        """Mostra os últimos jobs da fila e se há worker ativo."""
        from . import queue

        st = queue.worker_status()
        click.echo(f"worker ativo: {'sim' if st['alive'] else 'NÃO'} · jobs pendentes: {st['pending']}")
        for j in queue.recent_jobs(15):
            click.echo(f"  #{j['id']:<4} {j['task']:<14} {j['status']:<10} tentativas={j['attempts']} {j['args']}")

    @app.cli.command("verify-audit")
    def verify_audit_cmd():
        """Verifica a integridade da cadeia de hash da auditoria."""
        from .audit import verify_chain

        ok, bad, total = verify_chain()
        if not ok:
            raise click.ClickException(f"cadeia ADULTERADA a partir do evento #{bad} ({total} verificados)")
        click.echo(f"cadeia íntegra: {total} eventos")

    @app.cli.command("run-sprint")
    @click.argument("request")
    @click.option("--capacity", default=13)
    @click.option("--name", default="Sprint CLI")
    def run_sprint_cmd(request, capacity, name):
        """Fluxo completo em primeiro plano: refine → plan → execute → review → retro."""
        from .extensions import db
        from .models import Sprint
        from .scrum import SprintRunner

        sp = Sprint(name=name, capacity=capacity)
        db.session.add(sp)
        db.session.commit()
        runner = SprintRunner(sp)
        items, reply = runner.refine(request)
        click.echo(f"PO: {reply} ({len(items)} stories)")
        stats = runner.run()
        click.echo(f"Sprint #{sp.id} encerrada: {stats}")
        for i in sp.items:
            click.echo(f"  {i.key} [{i.status}] {i.title}")
