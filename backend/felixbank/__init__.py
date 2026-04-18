from __future__ import annotations

import logging
import os

from flask import Flask, request

from .config import FRONTEND_DIR
from .db import init_storage
from .routes import register_routes
from .utils import money_filter


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(FRONTEND_DIR),
        template_folder=str(FRONTEND_DIR / "templates"),
    )
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
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
