from __future__ import annotations

from flask import Flask


def register_static_routes(app: Flask) -> None:
    @app.get("/static/<path:filename>")
    def serve_static_file(filename: str):
        return app.send_static_file(f"static/{filename}")

    @app.get("/login/style.css")
    def serve_login_css():
        return app.send_static_file("static/css/login.css")

    @app.get("/profile/profile.css")
    def serve_profile_css():
        return app.send_static_file("static/css/profile.css")

    @app.get("/assets/<path:filename>")
    def serve_assets(filename: str):
        return app.send_static_file(f"assets/{filename}")
