from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .config import TWOPLACES


def decimal_to_str(value: Decimal | float | int) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value.quantize(TWOPLACES, rounding=ROUND_HALF_UP), "f")


def decimal_input(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None
    if not value.is_finite():
        return None
    return value


def money_filter(value: Any) -> str:
    try:
        normalized = Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return "0.00"
    return f"{normalized:,.2f}".replace(",", " ")
