def register_blueprints(app):
    from .a2a_bp import bp as a2a_bp
    from .api import bp as api_bp
    from .gateway import bp as gateway_bp
    from .ui import bp as ui_bp

    for b in (ui_bp, api_bp, a2a_bp, gateway_bp):
        app.register_blueprint(b)
