from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from ..auth import current_user, login_required
from ..config import (
    DEFAULT_TRANSFER_PIN,
    TRANSFER_DAILY_LIMIT_UAH,
    TRANSFER_MAX_AMOUNT_UAH,
    TRANSFER_MIN_AMOUNT_UAH,
    TWOPLACES,
)
from ..db import (
    create_notification,
    get_all_user_logins,
    get_balances,
    get_recent_notifications,
    get_recent_rates_rows,
    get_recent_rates_rows_in_range,
    get_rates_history,
    get_rates_history_between_dates,
    get_or_create_virtual_card,
    get_sender_daily_transfer_total,
    get_transfer_history_for_user,
    get_user_profile,
    insert_rates_history,
    save_user_profile,
    set_virtual_card_blocked,
    update_balances,
)
from ..rates import rates_payload
from ..utils import decimal_input, decimal_to_str
from .common import ALLOWED_AVATAR_EXT, RATES_UAH_PER_1, avatar_url_for
from .transfer_routes import _history_csv_response, _history_links, _read_history_filters


DASHBOARD_SECTIONS = {
    "overview",
    "profile",
    "rates",
    "transfer-card",
    "transfer-international",
    "virtual-card",
}


def _fallback_rates() -> list[dict[str, object]]:
    return [
        {"code": "USD", "name": "Доллар США", "uah_per_1": 39.50},
        {"code": "EUR", "name": "Евро", "uah_per_1": 43.00},
        {"code": "JPY", "name": "Японская иена", "uah_per_1": 0.2600},
        {"code": "KRW", "name": "Южнокорейская вона", "uah_per_1": 0.0300},
        {"code": "CNY", "name": "Китайский юань", "uah_per_1": 5.40},
    ]


def _rates_for_chart_and_storage(payload: dict[str, object]) -> list[dict[str, object]]:
    rates = payload.get("rates")
    if isinstance(rates, list) and rates:
        return rates
    return _fallback_rates()


def _store_rates_history_safely(rates: list[dict[str, object]]) -> None:
    try:
        insert_rates_history(rates)
    except Exception:
        # История не должна ломать страницу курсов.
        pass


def _default_chart_dates() -> tuple[date, date]:
    end_date = date.today()
    start_date = end_date - timedelta(days=364)
    return start_date, end_date


def _parse_requested_chart_dates() -> tuple[date, date] | None:
    start_raw = str(request.args.get("start_date") or "").strip()
    end_raw = str(request.args.get("end_date") or "").strip()
    if not start_raw or not end_raw:
        return None

    try:
        start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
    except ValueError:
        return None

    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _normalized_dashboard_section(raw_value: str | None, default: str = "overview") -> str:
    candidate = str(raw_value or "").strip().lower()
    if candidate in DASHBOARD_SECTIONS:
        return candidate
    return default


def _dashboard_redirect(section: str, **params: str):
    return redirect(url_for("profile", section=_normalized_dashboard_section(section), **params))


def register_profile_routes(app: Flask) -> None:
    @app.route("/profile/", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.args.get("logout"):
            session.clear()
            return redirect(url_for("login"))

        user = current_user()
        assert user is not None
        user_id = int(user["id"])
        active_section = _normalized_dashboard_section(request.args.get("section"), "overview")

        if request.method == "POST" and request.form.get("action") == "exchange":
            return_section = _normalized_dashboard_section(request.form.get("return_section"), active_section)
            from_code = str(request.form.get("from") or "")
            to_code = str(request.form.get("to") or "")
            amount = decimal_input(request.form.get("amount") or "")
            balances = get_balances(user_id)

            if from_code not in RATES_UAH_PER_1 or to_code not in RATES_UAH_PER_1:
                flash("Выберите валюты обмена.", "error")
                return _dashboard_redirect(return_section)
            elif from_code == to_code:
                flash("Выберите разные валюты.", "error")
                return _dashboard_redirect(return_section)
            elif amount is None or amount <= 0:
                flash("Введите сумму больше нуля.", "error")
                return _dashboard_redirect(return_section)
            elif balances.get(from_code, Decimal("0")) < amount:
                flash("Недостаточно средств для обмена.", "error")
                return _dashboard_redirect(return_section)
            else:
                uah_amount = amount * RATES_UAH_PER_1[from_code]
                to_amount = (uah_amount / RATES_UAH_PER_1[to_code]).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                balances[from_code] = (balances.get(from_code, Decimal("0")) - amount).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                balances[to_code] = (balances.get(to_code, Decimal("0")) + to_amount).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                update_balances(user_id, balances)
                flash(
                    f"Обмен выполнен: {decimal_to_str(amount)} {from_code} -> "
                    f"{decimal_to_str(to_amount)} {to_code}",
                    "ok",
                )
                return _dashboard_redirect(return_section)

        balances = get_balances(user_id)
        profile_data = get_user_profile(user_id)
        users = get_all_user_logins(exclude_user_id=user_id)
        card = get_or_create_virtual_card(user_id, str(user["login"]))
        card_blocked = bool(card.get("blocked"))
        daily_transfer_total = get_sender_daily_transfer_total(user_id, "UAH")
        payload = rates_payload()
        fallback = _fallback_rates()
        chart_start_date, chart_end_date = _default_chart_dates()
        _store_rates_history_safely(_rates_for_chart_and_storage(payload))
        history_filters = _read_history_filters()
        history = get_transfer_history_for_user(
            user_id,
            limit=None if request.args.get("download") == "csv" else 50,
            direction=history_filters["direction"],
            currency_code=None if history_filters["currency"] == "all" else history_filters["currency"],
            search_query=history_filters["search"],
        )

        if request.args.get("download") == "csv" and active_section in {"transfer-card", "transfer-international"}:
            return _history_csv_response(history, user_id, "felixbank-transfer-history")

        return render_template(
            "dashboard.html",
            active_section=active_section,
            login=user["login"],
            balances=balances,
            rates_uah_per_1=RATES_UAH_PER_1,
            payload=payload,
            fallback=fallback,
            chart_default_start_date=chart_start_date.isoformat(),
            chart_default_end_date=chart_end_date.isoformat(),
            users=users,
            card=card,
            card_blocked=card_blocked,
            history=history,
            history_filters=history_filters,
            transfer_card_history_links=_history_links("profile", history_filters, extra_params={"section": "transfer-card"}),
            transfer_international_history_links=_history_links("profile", history_filters, extra_params={"section": "transfer-international"}),
            profile_data=profile_data,
            avatar_url=avatar_url_for(str(profile_data.get("avatar_filename") or "")),
            demo_transfer_pin=DEFAULT_TRANSFER_PIN,
            transfer_min_amount_uah=TRANSFER_MIN_AMOUNT_UAH,
            transfer_max_amount_uah=TRANSFER_MAX_AMOUNT_UAH,
            transfer_daily_limit_uah=TRANSFER_DAILY_LIMIT_UAH,
            daily_transfer_total_uah=daily_transfer_total,
        )

    @app.get("/profile/index.html")
    def profile_legacy():
        return redirect(url_for("profile"))

    @app.route("/profile/details", methods=["GET", "POST"])
    @login_required
    def profile_details():
        if request.method == "GET":
            return _dashboard_redirect("profile")

        user = current_user()
        assert user is not None
        user_id = int(user["id"])
        return_section = _normalized_dashboard_section(request.form.get("return_section"), "profile")
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        age_raw = (request.form.get("age") or "").strip()
        age: int | None = None

        if age_raw:
            try:
                age = int(age_raw)
            except ValueError:
                flash("Возраст должен быть числом.", "error")
                return _dashboard_redirect(return_section)
            if age < 1 or age > 120:
                flash("Возраст должен быть в диапазоне 1-120.", "error")
                return _dashboard_redirect(return_section)

        avatar_file = request.files.get("avatar")
        avatar_filename_to_save: str | None = None
        if avatar_file and avatar_file.filename:
            safe_name = secure_filename(avatar_file.filename)
            ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
            if ext not in ALLOWED_AVATAR_EXT:
                flash("Разрешены только изображения: png, jpg, jpeg, webp, gif.", "error")
                return _dashboard_redirect(return_section)
            avatars_dir = Path(app.static_folder or "") / "assets" / "avatars"
            avatars_dir.mkdir(parents=True, exist_ok=True)
            avatar_filename_to_save = f"user_{user_id}_{uuid4().hex[:10]}.{ext}"
            avatar_file.save(avatars_dir / avatar_filename_to_save)

        save_user_profile(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name,
            age=age,
            avatar_filename=avatar_filename_to_save,
        )
        flash("Профиль обновлен.", "ok")
        return _dashboard_redirect(return_section)

    @app.get("/profile/rates")
    @app.get("/profile/rates.php")
    @login_required
    def rates():
        return _dashboard_redirect("rates")

    @app.get("/profile/rates/data")
    @login_required
    def rates_data():
        user = current_user()
        assert user is not None
        payload = rates_payload()
        rates = _rates_for_chart_and_storage(payload)
        _store_rates_history_safely(rates)
        notify_requested = str(request.args.get("notify") or "").strip().lower() in {"1", "true", "yes", "on"}
        notify_code = str(request.args.get("code") or "USD").strip().upper()[:3]
        if notify_requested and notify_code:
            create_notification(
                int(user["id"]),
                f"Курс {notify_code} обновлен",
                kind="info",
            )
        return jsonify(
            {
                "ok": bool(payload.get("ok")),
                "date": payload.get("date") or "",
                "error": payload.get("error") or "",
                "rates": rates,
            }
        )

    @app.get("/profile/rates/history")
    @login_required
    def rates_history():
        code = str(request.args.get("code") or "USD").upper()[:3]
        requested_dates = _parse_requested_chart_dates()
        range_raw = str(request.args.get("range") or "1y").lower().strip()
        try:
            if requested_dates is not None:
                start_date, end_date = requested_dates
                requested_days = max(1, (end_date - start_date).days + 1)
                aggregate_by_day = requested_days > 45
                limit = 366 if aggregate_by_day else 1500
                points = get_rates_history_between_dates(
                    code,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                    aggregate_by_day=aggregate_by_day,
                )
                recent_rows = get_recent_rates_rows_in_range(code, start_date, end_date, limit=8)
                source_mode = "date_range"
            else:
                range_raw = str(request.args.get("range") or "1y").lower().strip()
                seconds_map = {"1h": 3600, "1y": 365 * 24 * 3600}
                since_seconds = seconds_map.get(range_raw, 365 * 24 * 3600)
                aggregate_by_day = range_raw == "1y"
                limit = 366 if aggregate_by_day else max(2, min(120, since_seconds // 60 + 2))
                points = get_rates_history(
                    code,
                    since_seconds=since_seconds,
                    limit=limit,
                    aggregate_by_day=aggregate_by_day,
                )
                recent_rows = get_recent_rates_rows(code, limit=8)
                source_mode = "predefined_range"
        except Exception:
            points = []
            recent_rows = []
            if requested_dates is not None:
                start_date, end_date = requested_dates
            else:
                start_date, end_date = _default_chart_dates()
            source_mode = "date_range" if requested_dates is not None else "predefined_range"

        def format_recent_row(row: dict[str, object]) -> dict[str, object]:
            created_at = row.get("created_at")
            if isinstance(created_at, datetime):
                created_at_iso = created_at.isoformat()
                created_at_label = created_at.strftime("%d.%m %H:%M:%S")
            else:
                created_at_iso = str(created_at or "")
                created_at_label = str(created_at or "")
            return {
                "currency_code": str(row.get("currency_code") or code),
                "uah_per_1": float(row.get("uah_per_1") or 0),
                "created_at": created_at_iso,
                "created_at_label": created_at_label,
            }

        if requested_dates is None:
            start_date, end_date = _default_chart_dates()

        return jsonify(
            {
                "ok": True,
                "code": code,
                "range": range_raw,
                "points": points,
                "rows": [format_recent_row(row) for row in recent_rows],
                "source_table": "currency_rates_history",
                "source_mode": source_mode,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        )

    @app.get("/profile/virtual-card")
    @login_required
    def virtual_card():
        return _dashboard_redirect("virtual-card")

    @app.post("/profile/virtual-card/lock")
    @login_required
    def virtual_card_lock():
        user = current_user()
        assert user is not None

        payload = request.get_json(silent=True) or {}
        blocked = bool(payload.get("blocked"))
        set_virtual_card_blocked(int(user["id"]), blocked)
        if blocked:
            create_notification(
                int(user["id"]),
                "Карта успешно заблокирована",
                kind="warning",
            )
        return jsonify({"ok": True, "blocked": blocked})

    @app.get("/profile/notifications")
    @login_required
    def notifications_feed():
        user = current_user()
        assert user is not None
        notifications = get_recent_notifications(int(user["id"]), limit=8)

        def serialize(row: dict[str, object]) -> dict[str, object]:
            created_at = row.get("created_at")
            if isinstance(created_at, datetime):
                created_at_label = created_at.strftime("%d.%m %H:%M")
            else:
                created_at_label = str(created_at or "")
            return {
                "id": int(row.get("id") or 0),
                "title": str(row.get("title") or ""),
                "kind": str(row.get("kind") or "info"),
                "created_at_label": created_at_label,
            }

        return jsonify(
            {
                "ok": True,
                "notifications": [serialize(row) for row in notifications],
            }
        )
