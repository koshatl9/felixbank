from __future__ import annotations

from backend.felixbank import create_app
from backend.felixbank.config import env, env_bool


app = create_app()


if __name__ == "__main__":
    debug_enabled = env_bool("APP_DEBUG", False)
    app.run(
        host="0.0.0.0",
        port=int(env("APP_PORT", "8000")),
        debug=debug_enabled,
        use_reloader=env_bool("APP_RELOAD", debug_enabled),
    )
