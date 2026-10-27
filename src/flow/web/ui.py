from pathlib import Path

from flask import Blueprint, render_template

bp = Blueprint("ui", __name__)
STATIC = Path(__file__).resolve().parents[1] / "static"


@bp.get("/")
def index():
    # ?v=<mtime> evita o navegador servir app.js/app.css antigos do cache
    v = max(int((STATIC / f).stat().st_mtime) for f in ("js/app.js", "css/app.css"))
    return render_template("dashboard.html", v=v)
