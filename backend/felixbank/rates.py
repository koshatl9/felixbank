from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from flask import session


def _trend_from_percent(percent_change: Decimal) -> str:
    if percent_change > Decimal("0"):
        return "up"
    if percent_change < Decimal("0"):
        return "down"
    return "flat"


def fetch_cbr_rates() -> dict[str, Any]:
    request_obj = Request(
        "https://www.cbr-xml-daily.ru/daily_json.js",
        headers={"User-Agent": "Mozilla/5.0 (CurrencyRatesDemo)"},
    )
    try:
        with urlopen(request_obj, timeout=3) as response:
            raw = response.read().decode("utf-8")
    except (TimeoutError, URLError, OSError):
        return {"ok": False, "error": "Не удалось получить курсы (нет соединения)."}

    try:
        payload = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "Ответ сервера с курсами повреждён."}

    valute = payload.get("Valute")
    if not isinstance(valute, dict):
        return {"ok": False, "error": "Ответ сервера с курсами повреждён."}

    rub_per_1 = {"RUB": Decimal("1")}
    previous_rub_per_1 = {"RUB": Decimal("1")}
    name_map = {"RUB": "Российский рубль"}

    for code, item in valute.items():
        if not isinstance(item, dict):
            continue
        char_code = str(item.get("CharCode") or code)
        name = str(item.get("Name") or char_code)
        try:
            value = Decimal(str(item.get("Value") or "0"))
            previous = Decimal(str(item.get("Previous") or "0"))
            nominal = Decimal(str(item.get("Nominal") or "1"))
        except InvalidOperation:
            continue
        if nominal <= 0 or value <= 0:
            continue
        rub_per_1[char_code] = value / nominal
        if previous > 0:
            previous_rub_per_1[char_code] = previous / nominal
        name_map[char_code] = name

    if "UAH" not in rub_per_1 or rub_per_1["UAH"] <= 0:
        return {"ok": False, "error": "В источнике нет UAH для пересчёта."}
    if "UAH" not in previous_rub_per_1 or previous_rub_per_1["UAH"] <= 0:
        return {"ok": False, "error": "В источнике нет предыдущего курса UAH."}

    uah_rate = rub_per_1["UAH"]
    previous_uah_rate = previous_rub_per_1["UAH"]
    rates = []
    for code, rub in rub_per_1.items():
        if code == "RUB":
            continue
        previous_rub = previous_rub_per_1.get(code)
        if previous_rub is None or previous_rub <= 0:
            previous_uah_per_1 = rub / uah_rate
            percent_change = Decimal("0")
        else:
            previous_uah_per_1 = previous_rub / previous_uah_rate
            percent_change = (
                (rub / uah_rate - previous_uah_per_1) / previous_uah_per_1
            ) * Decimal("100")
        rates.append(
            {
                "code": code,
                "name": name_map.get(code, code),
                "uah_per_1": float(rub / uah_rate),
                "previous_uah_per_1": float(previous_uah_per_1),
                "percent_change": float(percent_change),
                "trend": _trend_from_percent(percent_change),
            }
        )

    rates.sort(key=lambda item: item["code"])
    return {
        "ok": True,
        "date": str(payload.get("Date") or ""),
        "rates": rates,
        "base": "UAH",
    }


def rates_payload() -> dict[str, Any]:
    cache = session.get("rates_cache")
    if isinstance(cache, dict):
        ts = int(cache.get("ts", 0))
        payload = cache.get("payload")
        if isinstance(payload, dict) and time.time() - ts < 60:
            return payload

    payload = fetch_cbr_rates()
    session["rates_cache"] = {"ts": int(time.time()), "payload": payload}
    return payload
