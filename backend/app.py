from __future__ import annotations

from backend.felixbank import create_app
from backend.felixbank.config import env


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("APP_PORT", "8000")), debug=False)
