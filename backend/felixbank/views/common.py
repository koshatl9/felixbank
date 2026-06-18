from __future__ import annotations

from decimal import Decimal
from flask import url_for


ALLOWED_AVATAR_EXT = {"png", "jpg", "jpeg", "webp", "gif"}

RATES_UAH_PER_1 = {
    "UAH": Decimal("1.0"),
    "USD": Decimal("39.5"),
}


def avatar_url_for(avatar_filename: str) -> str:
    normalized = (avatar_filename or "").strip()
    if not normalized:
        return url_for("serve_assets", filename="mascot.png")
    return url_for("serve_assets", filename=f"avatars/{normalized}")
