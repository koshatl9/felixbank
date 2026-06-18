from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from ..auth import current_user, login_required
from ..config import (
    DEFAULT_AUTOSAVE_PERCENT,
    DEFAULT_TRANSFER_PIN,
    TRANSFER_DAILY_LIMIT_UAH,
    TRANSFER_MAX_AMOUNT_UAH,
    TRANSFER_MIN_AMOUNT_UAH,
    TWOPLACES,
)
from ..db import (
    create_audit_log,
    create_notification,
    create_savings_goal,
    get_all_user_logins,
    get_balances,
    get_financial_history_for_user,
    get_recent_notifications,
    get_recent_rates_rows,
    get_recent_rates_rows_in_range,
    get_rates_history,
    get_rates_history_between_dates,
    get_or_create_virtual_card,
    get_savings_goal_activity,
    get_savings_goals,
    get_savings_settings,
    get_sender_daily_transfer_total,
    get_user_profile,
    insert_rates_history,
    save_user_profile,
    save_savings_settings,
    set_virtual_card_blocked,
    top_up_savings_goal,
    update_balances,
)
from ..rates import rates_payload
from ..utils import decimal_input, decimal_to_str
from .common import ALLOWED_AVATAR_EXT, RATES_UAH_PER_1, avatar_url_for
from .transfer_routes import _history_csv_response, _history_links, _read_history_filters


DASHBOARD_SECTIONS = {
    "overview",
    "goals",
    "profile",
    "rates",
    "transfer-card",
    "transfer-international",
    "virtual-card",
}

GOAL_THEME_OPTIONS = {
    "tech": {
        "key": "tech",
        "label": "Tech / gadgets",
        "badge": "💻",
        "accent": "#b56cff",
        "accent_strong": "#7c3aed",
        "accent_soft": "rgba(181, 108, 255, 0.20)",
    },
    "travel": {
        "key": "travel",
        "label": "Travel / dream",
        "badge": "✈️",
        "accent": "#5dd7ff",
        "accent_strong": "#2563eb",
        "accent_soft": "rgba(93, 215, 255, 0.18)",
    },
    "home": {
        "key": "home",
        "label": "Home / comfort",
        "badge": "🏡",
        "accent": "#ffb86c",
        "accent_strong": "#f97316",
        "accent_soft": "rgba(255, 184, 108, 0.20)",
    },
    "future": {
        "key": "future",
        "label": "Big future",
        "badge": "🚀",
        "accent": "#63f2a7",
        "accent_strong": "#16a34a",
        "accent_soft": "rgba(99, 242, 167, 0.18)",
    },
}


def _goal_theme_meta(theme_key: str) -> dict[str, str]:
    return GOAL_THEME_OPTIONS.get(str(theme_key or "").strip().lower(), GOAL_THEME_OPTIONS["tech"])


def _goal_progress_pct(saved_amount: Decimal, target_amount: Decimal) -> int:
    if target_amount <= 0:
        return 0
    ratio = (saved_amount / target_amount) * Decimal("100")
    rounded = int(ratio.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0, min(100, rounded))


def _decorate_savings_goals(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]:
    goals: list[dict[str, object]] = []
    total_target = Decimal("0.00")
    total_saved = Decimal("0.00")
    total_remaining = Decimal("0.00")
    active_count = 0
    completed_count = 0
    today = date.today()

    for row in rows:
        target_amount = Decimal(str(row.get("target_amount") or "0")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        saved_amount = Decimal(str(row.get("saved_amount") or "0")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        remaining_amount = max(
            Decimal("0.00"),
            (target_amount - saved_amount).quantize(TWOPLACES, rounding=ROUND_HALF_UP),
        )
        progress_pct = _goal_progress_pct(saved_amount, target_amount)
        completed = saved_amount >= target_amount and target_amount > 0
        theme = _goal_theme_meta(str(row.get("theme_key") or "tech"))
        target_date_value = row.get("target_date")
        target_date_label = target_date_value.strftime("%d.%m.%Y") if isinstance(target_date_value, date) else ""
        last_topup_at = row.get("last_topup_at")
        last_topup_label = last_topup_at.strftime("%d.%m.%Y %H:%M") if isinstance(last_topup_at, datetime) else ""

        recommended_monthly: Decimal | None = None
        pace_hint = ""
        if isinstance(target_date_value, date) and not completed and remaining_amount > 0:
            days_left = (target_date_value - today).days
            if days_left >= 0:
                months_left = max(1, (days_left + 29) // 30)
                recommended_monthly = (remaining_amount / Decimal(months_left)).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                pace_hint = (
                    f"Чтобы успеть к {target_date_label}, откладывайте около "
                    f"{decimal_to_str(recommended_monthly)} UAH в месяц."
                )
            else:
                pace_hint = "Срок уже прошел, но цель можно добить в свободном темпе."

        if completed:
            status_label = "Цель закрыта"
            status_class = "goal-status--success"
            completed_count += 1
        else:
            active_count += 1
            if progress_pct >= 75:
                status_label = "Финальный рывок"
                status_class = "goal-status--warning"
            elif progress_pct >= 40:
                status_label = "В хорошем темпе"
                status_class = "goal-status--info"
            else:
                status_label = "На старте"
                status_class = "goal-status--default"

        decorated_goal = {
            **row,
            "theme": theme,
            "target_amount": target_amount,
            "saved_amount": saved_amount,
            "remaining_amount": remaining_amount,
            "progress_pct": progress_pct,
            "completed": completed,
            "status_label": status_label,
            "status_class": status_class,
            "target_date_label": target_date_label,
            "last_topup_label": last_topup_label,
            "topup_count": int(row.get("topup_count") or 0),
            "pace_hint": pace_hint,
            "recommended_monthly": recommended_monthly,
            "milestones": [
                {"value": 25, "active": progress_pct >= 25},
                {"value": 50, "active": progress_pct >= 50},
                {"value": 75, "active": progress_pct >= 75},
                {"value": 100, "active": completed},
            ],
        }
        goals.append(decorated_goal)
        total_target += target_amount
        total_saved += saved_amount
        total_remaining += remaining_amount

    overall_progress_pct = _goal_progress_pct(total_saved, total_target) if total_target > 0 else 0
    active_goals = [goal for goal in goals if not bool(goal["completed"])]
    featured_goal = None
    if active_goals:
        featured_goal = min(
            active_goals,
            key=lambda goal: (
                Decimal(str(goal["remaining_amount"])),
                0 if isinstance(goal.get("target_date"), date) else 1,
                goal.get("target_date") or date.max,
                -int(goal["progress_pct"]),
            ),
        )
    elif goals:
        featured_goal = goals[0]

    summary = {
        "goal_count": len(goals),
        "active_count": active_count,
        "completed_count": completed_count,
        "total_target": total_target.quantize(TWOPLACES, rounding=ROUND_HALF_UP),
        "total_saved": total_saved.quantize(TWOPLACES, rounding=ROUND_HALF_UP),
        "total_remaining": total_remaining.quantize(TWOPLACES, rounding=ROUND_HALF_UP),
        "overall_progress_pct": overall_progress_pct,
        "featured_goal": featured_goal,
    }
    return goals, summary


def _decorate_goal_activity(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    activity: list[dict[str, object]] = []
    for row in rows:
        theme = _goal_theme_meta(str(row.get("theme_key") or "tech"))
        amount = Decimal(str(row.get("amount") or "0")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        target_amount = Decimal(str(row.get("target_amount") or "0")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        saved_amount = Decimal(str(row.get("saved_amount") or "0")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        progress_pct = _goal_progress_pct(saved_amount, target_amount)
        created_at = row.get("created_at")
        created_at_label = created_at.strftime("%d.%m.%Y %H:%M") if isinstance(created_at, datetime) else ""
        event_type = str(row.get("event_type") or "topup")
        event_title = (
            f"Автопополнение цели «{row.get('title') or 'Цель накопления'}»"
            if event_type == "auto_topup"
            else f"Пополнение цели «{row.get('title') or 'Цель накопления'}»"
        )
        event_hint = (
            "Деньги автоматически ушли в копилку после входящего перевода."
            if event_type == "auto_topup"
            else "Средства переведены в цель вручную с UAH-баланса."
        )
        activity.append(
            {
                **row,
                "theme": theme,
                "amount": amount,
                "target_amount": target_amount,
                "saved_amount": saved_amount,
                "progress_pct": progress_pct,
                "created_at_label": created_at_label,
                "event_type": event_type,
                "event_title": event_title,
                "event_hint": event_hint,
            }
        )
    return activity


def _decorate_savings_settings(
    settings_row: dict[str, object],
    goals: list[dict[str, object]],
) -> dict[str, object]:
    auto_percent = Decimal(str(settings_row.get("auto_percent") or DEFAULT_AUTOSAVE_PERCENT)).quantize(
        TWOPLACES,
        rounding=ROUND_HALF_UP,
    )
    auto_percent = min(Decimal("100.00"), max(Decimal("0.00"), auto_percent))
    main_percent = (Decimal("100.00") - auto_percent).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    incoming_example = Decimal("1000.00")
    auto_amount_example = (incoming_example * auto_percent / Decimal("100")).quantize(
        TWOPLACES,
        rounding=ROUND_HALF_UP,
    )
    main_amount_example = (incoming_example - auto_amount_example).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    target_goal_id = settings_row.get("target_goal_id")
    normalized_goal_id = int(target_goal_id) if target_goal_id is not None else None
    goal_map = {int(goal["id"]): goal for goal in goals}
    target_goal = goal_map.get(normalized_goal_id)

    return {
        **settings_row,
        "enabled": bool(settings_row.get("enabled")),
        "auto_percent": auto_percent,
        "auto_percent_label": decimal_to_str(auto_percent),
        "main_percent": main_percent,
        "main_percent_label": decimal_to_str(main_percent),
        "incoming_example": incoming_example,
        "auto_amount_example": auto_amount_example,
        "main_amount_example": main_amount_example,
        "target_goal_id": normalized_goal_id,
        "target_goal": target_goal,
        "target_goal_missing": normalized_goal_id is not None and target_goal is None,
        "status_label": "Включена" if bool(settings_row.get("enabled")) else "Выключена",
        "status_class": "goal-status--success" if bool(settings_row.get("enabled")) else "goal-status--default",
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
            user = current_user()
            if user is not None:
                create_audit_log(
                    int(user["id"]),
                    "logout",
                    f"Пользователь {user['login']} вышел из системы.",
                )
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
        savings_goals_raw = get_savings_goals(user_id)
        savings_goals, goals_summary = _decorate_savings_goals(savings_goals_raw)
        goals_activity = _decorate_goal_activity(get_savings_goal_activity(user_id, limit=10))
        savings_settings = _decorate_savings_settings(get_savings_settings(user_id), savings_goals)
        daily_transfer_total = get_sender_daily_transfer_total(user_id, "UAH")
        payload = rates_payload()
        fallback = _fallback_rates()
        chart_start_date, chart_end_date = _default_chart_dates()
        _store_rates_history_safely(_rates_for_chart_and_storage(payload))
        history_filters = _read_history_filters()
        history = get_financial_history_for_user(
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
            user_id=user_id,
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
            savings_goals=savings_goals,
            goals_summary=goals_summary,
            goals_activity=goals_activity,
            savings_settings=savings_settings,
            goal_theme_options=list(GOAL_THEME_OPTIONS.values()),
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

    @app.get("/profile/goals")
    @login_required
    def goals():
        return _dashboard_redirect("goals")

    @app.post("/profile/goals/create")
    @login_required
    def goal_create():
        user = current_user()
        assert user is not None

        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        target_amount = decimal_input(request.form.get("target_amount") or "")
        theme_key = str(request.form.get("theme_key") or "tech").strip().lower()
        target_date_raw = (request.form.get("target_date") or "").strip()
        target_date_value: date | None = None

        if theme_key not in GOAL_THEME_OPTIONS:
            theme_key = "tech"

        if not title:
            flash("Введите название цели.", "error")
            return _dashboard_redirect("goals")
        if target_amount is None or target_amount <= 0:
            flash("Введите корректную сумму цели больше нуля.", "error")
            return _dashboard_redirect("goals")

        if target_date_raw:
            try:
                target_date_value = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Выберите корректную дату цели.", "error")
                return _dashboard_redirect("goals")
            if target_date_value < date.today():
                flash("Дата цели не может быть в прошлом.", "error")
                return _dashboard_redirect("goals")

        goal_id = create_savings_goal(
            user_id=int(user["id"]),
            title=title,
            description=description,
            theme_key=theme_key,
            target_amount=target_amount,
            target_date=target_date_value,
            currency_code="UAH",
        )
        create_notification(
            int(user["id"]),
            f"Создана цель «{title}»",
            kind="success",
        )
        create_audit_log(
            int(user["id"]),
            "goal_created",
            (
                f"Создана цель накопления '{title}' "
                f"(goal_id={goal_id}) на сумму {decimal_to_str(target_amount)} UAH."
            ),
        )
        flash("Цель накопления создана.", "ok")
        return _dashboard_redirect("goals")

    @app.post("/profile/goals/settings")
    @login_required
    def goal_settings():
        user = current_user()
        assert user is not None

        enabled = str(request.form.get("enabled") or "").strip() == "1"
        auto_percent = decimal_input(request.form.get("auto_percent") or "")
        target_goal_raw = str(request.form.get("target_goal_id") or "").strip()
        target_goal_id: int | None = None

        if target_goal_raw:
            try:
                target_goal_id = int(target_goal_raw)
            except ValueError:
                flash("Выберите корректную цель для автокопилки.", "error")
                return _dashboard_redirect("goals")

        if auto_percent is None:
            auto_percent = DEFAULT_AUTOSAVE_PERCENT

        if enabled:
            if auto_percent <= 0 or auto_percent > Decimal("100"):
                flash("Процент автокопилки должен быть в диапазоне от 0.01% до 100%.", "error")
                return _dashboard_redirect("goals")
            if target_goal_id is None:
                flash("Выберите цель, в которую будет идти автопополнение.", "error")
                return _dashboard_redirect("goals")

        try:
            settings = save_savings_settings(
                int(user["id"]),
                enabled=enabled,
                auto_percent=auto_percent,
                target_goal_id=target_goal_id,
            )
        except ValueError as exc:
            message_map = {
                "goal not found": "Выбранная цель накопления не найдена.",
                "unsupported goal currency": "Автокопилка сейчас работает только с целями в UAH.",
                "goal already completed": "Нельзя привязать автокопилку к уже закрытой цели.",
                "target goal required": "Выберите цель для автокопилки.",
                "invalid auto percent": "Процент автокопилки должен быть в диапазоне от 0.01% до 100%.",
            }
            flash(message_map.get(str(exc), "Не удалось сохранить настройки автокопилки."), "error")
            return _dashboard_redirect("goals")

        if bool(settings.get("enabled")):
            goal_title = str(settings.get("goal_title") or "цель накопления")
            create_notification(
                int(user["id"]),
                (
                    f"Автокопилка включена: {decimal_to_str(settings['auto_percent'])}% "
                    f"в цель «{goal_title}»"
                ),
                kind="success",
            )
            create_audit_log(
                int(user["id"]),
                "savings_settings_updated",
                (
                    f"Автокопилка включена: {decimal_to_str(settings['auto_percent'])}% "
                    f"в goal_id={settings.get('target_goal_id')}."
                ),
            )
            flash(
                (
                    f"Автокопилка включена: {decimal_to_str(settings['auto_percent'])}% "
                    f"каждого входящего перевода в UAH будет идти в цель «{goal_title}»."
                ),
                "ok",
            )
        else:
            create_notification(
                int(user["id"]),
                "Автокопилка выключена",
                kind="info",
            )
            create_audit_log(
                int(user["id"]),
                "savings_settings_updated",
                "Автокопилка выключена.",
            )
            flash("Автокопилка выключена. Новые входящие переводы будут полностью идти на основной счет.", "ok")
        return _dashboard_redirect("goals")

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
        email = (request.form.get("email") or "").strip()
        age_raw = (request.form.get("age") or "").strip()
        age: int | None = None

        if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
            flash("Введите корректный email для кода подтверждения.", "error")
            return _dashboard_redirect(return_section)

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
            email=email,
            age=age,
            avatar_filename=avatar_filename_to_save,
        )
        create_audit_log(
            user_id,
            "profile_updated",
            (
                f"Профиль обновлен: first_name='{first_name}', "
                f"last_name='{last_name}', email='{email}', age='{age if age is not None else ''}'."
            ),
        )
        flash("Профиль обновлен.", "ok")
        return _dashboard_redirect(return_section)

    @app.post("/profile/goals/<int:goal_id>/fund")
    @login_required
    def goal_fund(goal_id: int):
        user = current_user()
        assert user is not None

        amount = decimal_input(request.form.get("amount") or "")
        if amount is None or amount <= 0:
            flash("Введите сумму пополнения больше нуля.", "error")
            return _dashboard_redirect("goals")

        try:
            result = top_up_savings_goal(int(user["id"]), goal_id, amount)
        except ValueError as exc:
            message_map = {
                "goal not found": "Цель накопления не найдена.",
                "goal already completed": "Эта цель уже закрыта.",
                "goal top up exceeds remaining amount": "Сумма пополнения больше, чем осталось до цели.",
                "insufficient funds": "Недостаточно UAH на балансе для пополнения цели.",
                "unsupported goal currency": "Сейчас пополнение целей доступно только в UAH.",
                "amount must be positive": "Введите сумму пополнения больше нуля.",
            }
            flash(message_map.get(str(exc), "Не удалось пополнить цель."), "error")
            return _dashboard_redirect("goals")

        create_notification(
            int(user["id"]),
            f"Цель «{result['title']}» пополнена на {decimal_to_str(result['amount'])} UAH",
            kind="success",
        )
        create_audit_log(
            int(user["id"]),
            "goal_funded",
            (
                f"Цель накопления '{result['title']}' (goal_id={result['goal_id']}) "
                f"пополнена на {decimal_to_str(result['amount'])} UAH."
            ),
        )
        if bool(result.get("completed")):
            create_notification(
                int(user["id"]),
                f"Цель «{result['title']}» достигнута",
                kind="success",
            )
            create_audit_log(
                int(user["id"]),
                "goal_completed",
                f"Цель накопления '{result['title']}' (goal_id={result['goal_id']}) достигнута.",
            )
            flash(f"Цель «{result['title']}» полностью закрыта.", "ok")
        else:
            flash(
                (
                    f"Цель «{result['title']}» пополнена на {decimal_to_str(result['amount'])} UAH. "
                    f"Осталось {decimal_to_str(result['remaining_amount'])} UAH."
                ),
                "ok",
            )
        return _dashboard_redirect("goals")

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
            create_audit_log(
                int(user["id"]),
                "card_blocked",
                f"Виртуальная карта пользователя {user['login']} заблокирована.",
            )
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
