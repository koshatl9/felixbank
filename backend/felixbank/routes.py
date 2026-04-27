from __future__ import annotations

from flask import Flask

from .views.auth_routes import register_auth_routes
from .views.profile_routes import register_profile_routes
from .views.static_routes import register_static_routes
from .views.transfer_routes import register_transfer_routes


def register_routes(app: Flask) -> None:
    register_auth_routes(app)
    register_profile_routes(app)
    register_transfer_routes(app)
    register_static_routes(app)
