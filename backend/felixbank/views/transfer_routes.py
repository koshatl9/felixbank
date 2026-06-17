from __future__ import annotations

from decimal import Decimal

import pymysql
from flask import Flask, flash, redirect, render_template, request, url_for

from ..auth import current_user, get_user_by_login, login_required, verify_transfer_pin
from ..config import (
    DEFAULT_TRANSFER_PIN,
    TRANSFER_DAILY_LIMIT_UAH,
    TRANSFER_MAX_AMOUNT_UAH,
    TRANSFER_MIN_AMOUNT_UAH,
    TRANSFER_PIN_RE,
)
from ..db import (
    get_all_user_logins,
    get_balances,
    get_sender_daily_transfer_total,
    get_transfer_history_for_user,
    get_user_by_virtual_card_number,
    get_user_profile,
    normalize_virtual_card_number,
    transfer_balance,
)
from ..utils import decimal_input, decimal_to_str
from .common import avatar_url_for


def register_transfer_routes(app: Flask) -> None:
    @app.route("/profile/transfer", methods=["GET", "POST"])
    @login_required
    def transfer():
        user = current_user()
        assert user is not None
        user_id = int(user["id"])
        balances = get_balances(user_id)
        daily_transfer_total = get_sender_daily_transfer_total(user_id, "UAH")

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
            elif len(recipient_card_digits) != 16:
                flash("Номер карты должен содержать 16 цифр.", "error")
            elif not normalized_card_number:
                flash("Введите номер карты в формате 5412 1234 5678 9012.", "error")
            elif amount is None or amount <= 0:
                flash("Введите корректную сумму больше нуля.", "error")
            elif amount < TRANSFER_MIN_AMOUNT_UAH:
                flash("Минимальная сумма перевода — 10 UAH", "error")
            elif amount > TRANSFER_MAX_AMOUNT_UAH:
                flash("Максимальная сумма перевода — 50 000 UAH", "error")
            elif amount > available_uah:
                flash("Недостаточно средств для перевода.", "error")
            elif daily_transfer_total + amount > TRANSFER_DAILY_LIMIT_UAH:
                flash("Дневной лимит переводов превышен", "error")
            elif not transfer_confirmed:
                flash("Подтвердите перевод в окне подтверждения.", "error")
            elif not TRANSFER_PIN_RE.fullmatch(transfer_pin):
                flash("Введите 4-значный PIN для подтверждения перевода.", "error")
            elif not verify_transfer_pin(user_id, transfer_pin):
                flash("Неверный PIN-код подтверждения.", "error")
            else:
                recipient = get_user_by_virtual_card_number(normalized_card_number)
                if recipient is None:
                    flash("Карта получателя не найдена.", "error")
                elif int(recipient["id"]) == user_id:
                    flash("Нельзя отправить перевод на свою карту.", "error")
                elif bool(recipient.get("is_blocked")):
                    flash("Карта получателя заблокирована.", "error")
                else:
                    try:
                        transfer_balance(
                            sender_id=user_id,
                            recipient_id=int(recipient["id"]),
                            amount=amount,
                            currency_code="UAH",
                        )
                        masked_card_number = f"**** **** **** {normalized_card_number[-4:]}"
                        flash(
                            f"Перевод выполнен: {decimal_to_str(amount)} UAH на карту {masked_card_number}.",
                            "ok",
                        )
                        return redirect(url_for("transfer"))
                    except ValueError:
                        flash("Недостаточно средств для перевода.", "error")
                    except (pymysql.MySQLError, RuntimeError):
                        app.logger.exception(
                            "Database error during transfer from %s to card %s",
                            user["login"],
                            normalized_card_number,
                        )
                        flash("Ошибка базы данных при выполнении перевода.", "error")

        history = get_transfer_history_for_user(user_id, limit=20)
        user_profile = get_user_profile(user_id)
        return render_template(
            "transfer.html",
            login=user["login"],
            balances=balances,
            history=history,
            user_id=user_id,
            profile_data=user_profile,
            avatar_url=avatar_url_for(str(user_profile.get("avatar_filename") or "")),
            demo_transfer_pin=DEFAULT_TRANSFER_PIN,
            transfer_min_amount_uah=TRANSFER_MIN_AMOUNT_UAH,
            transfer_max_amount_uah=TRANSFER_MAX_AMOUNT_UAH,
            transfer_daily_limit_uah=TRANSFER_DAILY_LIMIT_UAH,
            daily_transfer_total_uah=daily_transfer_total,
        )

    @app.route("/profile/transfer/international", methods=["GET", "POST"])
    @login_required
    def transfer_international():
        user = current_user()
        assert user is not None
        user_id = int(user["id"])
        balances = get_balances(user_id)
        users = get_all_user_logins(exclude_user_id=user_id)

        if request.method == "POST":
            recipient_login = (request.form.get("recipient_login") or "").strip()
            currency_code = str(request.form.get("currency_code") or "").strip().upper()
            amount = decimal_input(request.form.get("amount") or "")

            if not recipient_login:
                flash("Выберите получателя.", "error")
            elif recipient_login == user["login"]:
                flash("Нельзя отправить перевод самому себе.", "error")
            elif currency_code not in balances:
                flash("Выберите валюту списания.", "error")
            elif amount is None or amount <= 0:
                flash("Введите корректную сумму больше нуля.", "error")
            else:
                recipient = get_user_by_login(recipient_login)
                if recipient is None:
                    flash("Получатель не найден.", "error")
                else:
                    try:
                        transfer_balance(
                            sender_id=user_id,
                            recipient_id=int(recipient["id"]),
                            amount=amount,
                            currency_code=currency_code,
                        )
                        flash(
                            f"Международный перевод выполнен: {decimal_to_str(amount)} "
                            f"{currency_code} пользователю {recipient_login}.",
                            "ok",
                        )
                        return redirect(url_for("transfer_international"))
                    except ValueError:
                        flash("Недостаточно средств для перевода.", "error")
                    except (pymysql.MySQLError, RuntimeError):
                        app.logger.exception(
                            "Database error during international transfer from %s to %s",
                            user["login"],
                            recipient_login,
                        )
                        flash("Ошибка базы данных при международном переводе.", "error")

        history = get_transfer_history_for_user(user_id, limit=20)
        user_profile = get_user_profile(user_id)
        return render_template(
            "transfer_international.html",
            login=user["login"],
            balances=balances,
            history=history,
            user_id=user_id,
            users=users,
            profile_data=user_profile,
            avatar_url=avatar_url_for(str(user_profile.get("avatar_filename") or "")),
        )
