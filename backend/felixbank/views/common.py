from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from flask import url_for


ALLOWED_AVATAR_EXT = {"png", "jpg", "jpeg", "webp", "gif"}

RATES_UAH_PER_1 = {
    "UAH": Decimal("1.0"),
    "USD": Decimal("39.5"),
}


def build_virtual_card(login_value: str) -> dict[str, str]:
    digest = sha256(login_value.encode("utf-8")).hexdigest()
    digits = "".join(str(int(char, 16) % 10) for char in digest)
    card_number = f"5412 {digits[0:4]} {digits[4:8]} {digits[8:12]}"
    exp_month = (int(digits[12:14]) % 12) + 1
    exp_year = 26 + (int(digits[14:16]) % 5)
    cvv = f"{int(digits[16:19]) % 1000:03d}"
    holder = login_value.upper()[:24]
    return {
        "number": card_number,
        "expiry": f"{exp_month:02d}/{exp_year:02d}",
        "cvv": cvv,
        "holder": holder,
    }


def avatar_url_for(avatar_filename: str) -> str:
    normalized = (avatar_filename or "").strip()
    if not normalized:
        return url_for("serve_assets", filename="mascot.png")
    return url_for("serve_assets", filename=f"avatars/{normalized}")
