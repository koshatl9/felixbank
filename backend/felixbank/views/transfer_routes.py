from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pymysql
from flask import Flask, Response, flash, redirect, request, session, url_for

from ..auth import current_user, get_user_by_login, login_required, verify_transfer_pin
from ..config import (
    ANTIFRAUD_BLOCK_MINUTES,
    ANTIFRAUD_MAX_TOTAL_UAH_PER_WINDOW,
    ANTIFRAUD_MAX_TRANSFERS_PER_WINDOW,
    ANTIFRAUD_WINDOW_SECONDS,
    TRANSFER_DAILY_LIMIT_UAH,
    TRANSFER_MAX_AMOUNT_UAH,
    TRANSFER_MIN_AMOUNT_UAH,
    TRANSFER_PIN_RE,
)
from ..db import (
    create_audit_log,
    create_notification,
    get_balances,
    get_recent_sender_transfers,
    get_sender_daily_transfer_total,
    get_transfer_history_for_user,
    get_user_by_virtual_card_number,
    normalize_virtual_card_number,
    set_user_blocked_until,
    transfer_balance,
)
from ..utils import decimal_input, decimal_to_compact_str, decimal_to_str
from .common import RATES_UAH_PER_1

HISTORY_DIRECTION_VALUES = {"all", "incoming", "outgoing"}
HISTORY_CURRENCY_VALUES = {"all", "UAH", "USD"}


def _amount_to_uah(amount: Decimal, currency_code: str) -> Decimal:
    normalized_code = str(currency_code or "").strip().upper()
    rate = Decimal(str(RATES_UAH_PER_1.get(normalized_code, Decimal("1"))))
    return (Decimal(str(amount)) * rate).quantize(Decimal("0.01"))


def _recent_transfer_projection(
    user_id: int,
    pending_amount: Decimal,
    currency_code: str,
) -> tuple[int, Decimal]:
    recent_transfers = get_recent_sender_transfers(user_id, since_seconds=ANTIFRAUD_WINDOW_SECONDS)
    projected_total_uah = _amount_to_uah(pending_amount, currency_code)

    for transfer_row in recent_transfers:
        projected_total_uah += _amount_to_uah(
            Decimal(str(transfer_row["amount"])),
            str(transfer_row["currency_code"]),
        )

    return len(recent_transfers) + 1, projected_total_uah.quantize(Decimal("0.01"))


def _anti_fraud_block_response(user_id: int, projected_count: int, projected_total_uah: Decimal):
    blocked_until = (
        datetime.now(tz=timezone.utc) + timedelta(minutes=ANTIFRAUD_BLOCK_MINUTES)
    ).replace(tzinfo=None)
    set_user_blocked_until(user_id, blocked_until)
    create_audit_log(
        user_id,
        "antifraud_block",
        (
            f"Подозрительная активность: {projected_count} переводов за "
            f"{ANTIFRAUD_WINDOW_SECONDS} секунд, сумма "
            f"{decimal_to_str(projected_total_uah)} UAH."
        ),
    )
    session.clear()
    return redirect(url_for("login", blocked=1))


def _read_history_filters() -> dict[str, str]:
    direction = str(request.args.get("history_direction") or "all").strip().lower()
    if direction not in HISTORY_DIRECTION_VALUES:
        direction = "all"

    currency = str(request.args.get("history_currency") or "all").strip().upper()
    if currency not in HISTORY_CURRENCY_VALUES:
        currency = "all"

    search = str(request.args.get("history_search") or "").strip()[:64]
    return {
        "direction": direction,
        "currency": currency,
        "search": search,
    }


def _history_url(
    endpoint: str,
    filters: dict[str, str],
    *,
    direction: str | None = None,
    currency: str | None = None,
    search: str | None = None,
    download_csv: bool = False,
    extra_params: dict[str, str] | None = None,
) -> str:
    normalized_direction = filters["direction"] if direction is None else direction
    normalized_currency = filters["currency"] if currency is None else currency
    normalized_search = filters["search"] if search is None else search
    params: dict[str, str] = dict(extra_params or {})

    if normalized_direction and normalized_direction != "all":
        params["history_direction"] = normalized_direction
    if normalized_currency and normalized_currency != "all":
        params["history_currency"] = normalized_currency
    if normalized_search:
        params["history_search"] = normalized_search
    if download_csv:
        params["download"] = "csv"

    return url_for(endpoint, **params)


def _history_links(
    endpoint: str,
    filters: dict[str, str],
    extra_params: dict[str, str] | None = None,
) -> dict[str, str]:
    return {
        "reset": _history_url(endpoint, filters, direction="all", currency="all", extra_params=extra_params),
        "incoming": _history_url(endpoint, filters, direction="incoming", extra_params=extra_params),
        "outgoing": _history_url(endpoint, filters, direction="outgoing", extra_params=extra_params),
        "uah": _history_url(endpoint, filters, currency="UAH", extra_params=extra_params),
        "usd": _history_url(endpoint, filters, currency="USD", extra_params=extra_params),
        "csv": _history_url(endpoint, filters, download_csv=True, extra_params=extra_params),
    }


def _history_csv_response(history: list[dict[str, object]], user_id: int, filename_prefix: str) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Дата", "Отправитель", "Получатель", "Сумма", "Валюта", "Статус"])

    for row in history:
        created_at = row.get("created_at")
        status = "Исходящий" if int(row["sender_id"]) == user_id else "Входящий"
        writer.writerow(
            [
                created_at.strftime("%d.%m.%Y %H:%M") if created_at else "",
                str(row.get("sender_login") or ""),
                str(row.get("recipient_login") or ""),
                decimal_to_str(Decimal(str(row.get("amount") or "0"))),
                str(row.get("currency_code") or ""),
                status,
            ]
        )

    payload = "\ufeff" + output.getvalue()
    return Response(
        payload,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename_prefix}.csv"'},
    )


def _dashboard_redirect(section: str) -> Response:
    return redirect(url_for("profile", section=section))


def register_transfer_routes(app: Flask) -> None:
    @app.route("/profile/transfer", methods=["GET", "POST"])
    @login_required
    def transfer():
        user = current_user()
        assert user is not None
        user_id = int(user["id"])

        if request.method == "GET" and request.args.get("download") != "csv":
            return _dashboard_redirect("transfer-card")

        balances = get_balances(user_id)
        daily_transfer_total = get_sender_daily_transfer_total(user_id, "UAH")
        history_filters = _read_history_filters()
        return_section = str(request.form.get("return_section") or "transfer-card").strip() or "transfer-card"

        if request.method == "POST":
            recipient_card_number_raw = (request.form.get("recipient_card_number") or "").strip()
            recipient_card_digits = "".join(char for char in recipient_card_number_raw if char.isdigit())
            normalized_card_number = normalize_virtual_card_number(recipient_card_number_raw)
            amount = decimal_input(request.form.get("amount") or "")
            transfer_confirmed = (request.form.get("transfer_confirmed") or "").strip() == "1"
            transfer_pin = (request.form.get("transfer_pin") or "").strip()
            available_uah = balances.get("UAH", Decimal("0"))

            if not recipient_card_number_raw:
                flash("Введите номер виртуальной карты получателя.", "error")
                return _dashboard_redirect(return_section)
            elif len(recipient_card_digits) != 16:
                flash("Номер карты должен содержать 16 цифр.", "error")
                return _dashboard_redirect(return_section)
            elif not normalized_card_number:
                flash("Введите номер карты в формате 5412 1234 5678 9012.", "error")
                return _dashboard_redirect(return_section)
            elif amount is None or amount <= 0:
                flash("Введите корректную сумму больше нуля.", "error")
                return _dashboard_redirect(return_section)
            elif amount < TRANSFER_MIN_AMOUNT_UAH:
                flash("Минимальная сумма перевода — 10 UAH", "error")
                return _dashboard_redirect(return_section)
            elif amount > TRANSFER_MAX_AMOUNT_UAH:
                flash("Максимальная сумма перевода — 50 000 UAH", "error")
                return _dashboard_redirect(return_section)
            elif amount > available_uah:
                flash("Недостаточно средств для перевода.", "error")
                return _dashboard_redirect(return_section)
            elif not transfer_confirmed:
                flash("Подтвердите перевод в окне подтверждения.", "error")
                return _dashboard_redirect(return_section)
            elif not TRANSFER_PIN_RE.fullmatch(transfer_pin):
                flash("Введите 4-значный PIN для подтверждения перевода.", "error")
                return _dashboard_redirect(return_section)
            elif not verify_transfer_pin(user_id, transfer_pin):
                flash("Неверный PIN-код подтверждения.", "error")
                return _dashboard_redirect(return_section)
            else:
                recipient = get_user_by_virtual_card_number(normalized_card_number)
                if recipient is None:
                    flash("Карта получателя не найдена.", "error")
                    return _dashboard_redirect(return_section)
                elif int(recipient["id"]) == user_id:
                    flash("Нельзя отправить перевод на свою карту.", "error")
                    return _dashboard_redirect(return_section)
                elif bool(recipient.get("is_blocked")):
                    flash("Карта получателя заблокирована.", "error")
                    return _dashboard_redirect(return_section)
                else:
                    projected_count, projected_total_uah = _recent_transfer_projection(
                        user_id,
                        amount,
                        "UAH",
                    )
                    if (
                        projected_count > ANTIFRAUD_MAX_TRANSFERS_PER_WINDOW
                        or projected_total_uah > ANTIFRAUD_MAX_TOTAL_UAH_PER_WINDOW
                    ):
                        return _anti_fraud_block_response(user_id, projected_count, projected_total_uah)
                    if daily_transfer_total + amount > TRANSFER_DAILY_LIMIT_UAH:
                        flash("Дневной лимит переводов превышен", "error")
                        return _dashboard_redirect(return_section)
                    try:
                        transfer_balance(
                            sender_id=user_id,
                            recipient_id=int(recipient["id"]),
                            amount=amount,
                            currency_code="UAH",
                        )
                        create_notification(
                            user_id,
                            f"Перевод {decimal_to_compact_str(amount)} UAH выполнен",
                            kind="success",
                        )
                        create_notification(
                            int(recipient["id"]),
                            f"Получен перевод +{decimal_to_compact_str(amount)} UAH",
                            kind="success",
                        )
                        masked_card_number = f"**** **** **** {normalized_card_number[-4:]}"
                        flash(
                            f"Перевод выполнен: {decimal_to_str(amount)} UAH на карту {masked_card_number}.",
                            "ok",
                        )
                        return _dashboard_redirect(return_section)
                    except ValueError:
                        flash("Недостаточно средств для перевода.", "error")
                        return _dashboard_redirect(return_section)
                    except (pymysql.MySQLError, RuntimeError):
                        app.logger.exception(
                            "Database error during transfer from %s to card %s",
                            user["login"],
                            normalized_card_number,
                        )
                        flash("Ошибка базы данных при выполнении перевода.", "error")
                        return _dashboard_redirect(return_section)

        history = get_transfer_history_for_user(
            user_id,
            limit=None,
            direction=history_filters["direction"],
            currency_code=None if history_filters["currency"] == "all" else history_filters["currency"],
            search_query=history_filters["search"],
        )
        return _history_csv_response(history, user_id, "felixbank-transfer-history")

    @app.route("/profile/transfer/international", methods=["GET", "POST"])
    @login_required
    def transfer_international():
        user = current_user()
        assert user is not None
        user_id = int(user["id"])

        if request.method == "GET" and request.args.get("download") != "csv":
            return _dashboard_redirect("transfer-international")

        balances = get_balances(user_id)
        history_filters = _read_history_filters()
        return_section = str(request.form.get("return_section") or "transfer-international").strip() or "transfer-international"

        if request.method == "POST":
            recipient_login = (request.form.get("recipient_login") or "").strip()
            currency_code = str(request.form.get("currency_code") or "").strip().upper()
            amount = decimal_input(request.form.get("amount") or "")

            if not recipient_login:
                flash("Выберите получателя.", "error")
                return _dashboard_redirect(return_section)
            elif recipient_login == user["login"]:
                flash("Нельзя отправить перевод самому себе.", "error")
                return _dashboard_redirect(return_section)
            elif currency_code not in balances:
                flash("Выберите валюту списания.", "error")
                return _dashboard_redirect(return_section)
            elif amount is None or amount <= 0:
                flash("Введите корректную сумму больше нуля.", "error")
                return _dashboard_redirect(return_section)
            else:
                recipient = get_user_by_login(recipient_login)
                if recipient is None:
                    flash("Получатель не найден.", "error")
                    return _dashboard_redirect(return_section)
                else:
                    projected_count, projected_total_uah = _recent_transfer_projection(
                        user_id,
                        amount,
                        currency_code,
                    )
                    if (
                        projected_count > ANTIFRAUD_MAX_TRANSFERS_PER_WINDOW
                        or projected_total_uah > ANTIFRAUD_MAX_TOTAL_UAH_PER_WINDOW
                    ):
                        return _anti_fraud_block_response(user_id, projected_count, projected_total_uah)
                    try:
                        transfer_balance(
                            sender_id=user_id,
                            recipient_id=int(recipient["id"]),
                            amount=amount,
                            currency_code=currency_code,
                        )
                        create_notification(
                            user_id,
                            f"Перевод {decimal_to_compact_str(amount)} {currency_code} выполнен",
                            kind="success",
                        )
                        create_notification(
                            int(recipient["id"]),
                            f"Получен перевод +{decimal_to_compact_str(amount)} {currency_code}",
                            kind="success",
                        )
                        flash(
                            f"Международный перевод выполнен: {decimal_to_str(amount)} "
                            f"{currency_code} пользователю {recipient_login}.",
                            "ok",
                        )
                        return _dashboard_redirect(return_section)
                    except ValueError:
                        flash("Недостаточно средств для перевода.", "error")
                        return _dashboard_redirect(return_section)
                    except (pymysql.MySQLError, RuntimeError):
                        app.logger.exception(
                            "Database error during international transfer from %s to %s",
                            user["login"],
                            recipient_login,
                        )
                        flash("Ошибка базы данных при международном переводе.", "error")
                        return _dashboard_redirect(return_section)

        history = get_transfer_history_for_user(
            user_id,
            limit=None,
            direction=history_filters["direction"],
            currency_code=None if history_filters["currency"] == "all" else history_filters["currency"],
            search_query=history_filters["search"],
        )
        return _history_csv_response(history, user_id, "felixbank-transfer-history")
