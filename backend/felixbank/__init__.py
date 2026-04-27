from __future__ import annotations

import logging
import os

from flask import Flask, request

from .config import FRONTEND_DIR, env_bool
from .db import init_storage
from .routes import register_routes
from .utils import money_filter


def create_app() -> Flask:
    debug_enabled = env_bool("APP_DEBUG", False)
    app = Flask(
        __name__,
        static_folder=str(FRONTEND_DIR),
        template_folder=str(FRONTEND_DIR / "templates"),
    )
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
    app.config["TEMPLATES_AUTO_RELOAD"] = debug_enabled
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0 if debug_enabled else None
    app.jinja_env.auto_reload = debug_enabled
    app.logger.setLevel(logging.INFO)
    app.jinja_env.filters["money"] = money_filter

    @app.after_request
    def log_response(response):
        app.logger.info("%s %s -> %s", request.method, request.full_path.rstrip("?"), response.status_code)
        return response

    with app.app_context():
        init_storage()

    register_routes(app)
    return app
