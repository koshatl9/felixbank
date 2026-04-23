from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4

import pymysql
from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from .auth import create_user, current_user, get_user_by_login, login_required, verify_password
from .config import LOGIN_RE, TWOPLACES
from .db import (
    get_all_user_logins,
    get_balances,
    get_transfer_history_for_user,
    get_user_profile,
    save_user_profile,
    transfer_balance,
    update_balances,
)
from .rates import rates_payload
from .utils import decimal_input, decimal_to_str


def register_routes(app: Flask) -> None:
    allowed_avatar_ext = {"png", "jpg", "jpeg", "webp", "gif"}

    def avatar_url_for(avatar_filename: str) -> str:
        normalized = (avatar_filename or "").strip()
        if not normalized:
            return url_for("serve_assets", filename="mascot.png")
        return url_for("serve_assets", filename=f"avatars/{normalized}")

    @app.get("/")
    def root():
        return redirect(url_for("login"))

    @app.route("/login/", methods=["GET", "POST"])
    def login():
        if current_user() is not None:
            return redirect(url_for("profile"))

        error = None
        if request.method == "POST":
            login_value = (request.form.get("login") or "").strip()
            password = request.form.get("password") or ""

            if not login_value:
                error = "Введите логин."
            elif not password:
                error = "Введите пароль."
            else:
                try:
                    user = get_user_by_login(login_value)
                    if user is None:
                        error = "Пользователь не найден. Зарегистрируйтесь."
                    elif not verify_password(str(user["password_hash"]), password):
                        error = "Неверный пароль."
                    else:
                        session.clear()
                        session["user_id"] = int(user["id"])
                        session["login"] = str(user["login"])
                        return redirect(url_for("profile"))
                except (pymysql.MySQLError, RuntimeError):
                    app.logger.exception("Database error during login for %s", login_value)
                    error = "Не удалось подключиться к базе данных. Проверьте настройки MySQL."
                except ValueError:
                    app.logger.exception("Unsupported password hash for %s", login_value)
                    error = "Формат пароля этого пользователя не поддерживается."

        return render_template("login.html", error=error)

    @app.get("/login/login.php")
    @app.get("/login/index.html")
    def login_legacy():
        return redirect(url_for("login"))

    @app.route("/login/register/", methods=["GET", "POST"])
    def register():
        if current_user() is not None:
            return redirect(url_for("profile"))

        error = None
        if request.method == "POST":
            login_value = (request.form.get("login") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""

            if not login_value:
                error = "Введите логин."
            elif not LOGIN_RE.fullmatch(login_value):
                error = "Логин: 3-32 символа (латиница/цифры/._-)."
            elif len(password) < 8:
                error = "Пароль должен быть не короче 8 символов."
            elif password != confirm:
                error = "Пароли не совпадают."
            else:
                try:
                    if get_user_by_login(login_value) is not None:
                        error = "Пользователь с таким логином уже существует."
                    else:
                        user_id = create_user(login_value, password)
                        session.clear()
                        session["user_id"] = user_id
                        session["login"] = login_value
                        return redirect(url_for("profile"))
                except (pymysql.MySQLError, RuntimeError):
                    app.logger.exception("Database error during registration for %s", login_value)
                    error = "Не удалось сохранить пользователя в базе."

        return render_template("register.html", error=error)

    @app.get("/login/register/index.html")
    def register_legacy():
        return redirect(url_for("register"))

    @app.route("/profile/", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.args.get("logout"):
            session.clear()
            return redirect(url_for("login"))

        user = current_user()
        assert user is not None

        rates_uah_per_1 = {
            "UAH": Decimal("1.0"),
            "USD": Decimal("39.5"),
        }

        if request.method == "POST" and request.form.get("action") == "exchange":
            from_code = str(request.form.get("from") or "")
            to_code = str(request.form.get("to") or "")
            amount = decimal_input(request.form.get("amount") or "")
            balances = get_balances(user["id"])

            if from_code not in rates_uah_per_1 or to_code not in rates_uah_per_1:
                flash("Выберите валюты обмена.", "error")
            elif from_code == to_code:
                flash("Выберите разные валюты.", "error")
            elif amount is None or amount <= 0:
                flash("Введите сумму больше нуля.", "error")
            elif balances.get(from_code, Decimal("0")) < amount:
                flash("Недостаточно средств для обмена.", "error")
            else:
                uah_amount = amount * rates_uah_per_1[from_code]
                to_amount = (uah_amount / rates_uah_per_1[to_code]).quantize(
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
                update_balances(user["id"], balances)
                flash(
                    f"Обмен выполнен: {decimal_to_str(amount)} {from_code} -> "
                    f"{decimal_to_str(to_amount)} {to_code}",
                    "ok",
                )
                return redirect(url_for("profile"))

        balances = get_balances(user["id"])
        profile_data = get_user_profile(int(user["id"]))
        return render_template(
            "profile.html",
            login=user["login"],
            balances=balances,
            rates_uah_per_1=rates_uah_per_1,
            profile_data=profile_data,
            avatar_url=avatar_url_for(str(profile_data.get("avatar_filename") or "")),
        )

    @app.get("/profile/index.html")
    def profile_legacy():
        return redirect(url_for("profile"))

    @app.route("/profile/details", methods=["GET", "POST"])
    @login_required
    def profile_details():
        user = current_user()
        assert user is not None
        user_id = int(user["id"])
        profile_data = get_user_profile(user_id)

        if request.method == "POST":
            first_name = (request.form.get("first_name") or "").strip()
            last_name = (request.form.get("last_name") or "").strip()
            age_raw = (request.form.get("age") or "").strip()
            age: int | None = None

            if age_raw:
                try:
                    age = int(age_raw)
                except ValueError:
                    flash("Возраст должен быть числом.", "error")
                    return redirect(url_for("profile_details"))
                if age < 1 or age > 120:
                    flash("Возраст должен быть в диапазоне 1-120.", "error")
                    return redirect(url_for("profile_details"))

            avatar_file = request.files.get("avatar")
            avatar_filename_to_save: str | None = None
            if avatar_file and avatar_file.filename:
                safe_name = secure_filename(avatar_file.filename)
                ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
                if ext not in allowed_avatar_ext:
                    flash("Разрешены только изображения: png, jpg, jpeg, webp, gif.", "error")
                    return redirect(url_for("profile_details"))
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
            return redirect(url_for("profile_details"))

        balances = get_balances(user_id)
        users = get_all_user_logins(exclude_user_id=user_id)
        profile_data = get_user_profile(user_id)
        return render_template(
            "profile_details.html",
            login=user["login"],
            balances=balances,
            users=users,
            profile_data=profile_data,
            avatar_url=avatar_url_for(str(profile_data.get("avatar_filename") or "")),
        )

    @app.get("/profile/rates")
    @app.get("/profile/rates.php")
    @login_required
    def rates():
        user = current_user()
        assert user is not None
        user_profile = get_user_profile(int(user["id"]))

        fallback = [
            {"code": "USD", "name": "Доллар США", "uah_per_1": 39.50},
            {"code": "EUR", "name": "Евро", "uah_per_1": 43.00},
            {"code": "JPY", "name": "Японская иена", "uah_per_1": 0.2600},
            {"code": "KRW", "name": "Южнокорейская вона", "uah_per_1": 0.0300},
            {"code": "CNY", "name": "Китайский юань", "uah_per_1": 5.40},
        ]
        payload = rates_payload()
        return render_template(
            "rates.html",
            login=user["login"],
            payload=payload,
            fallback=fallback,
            profile_data=user_profile,
            avatar_url=avatar_url_for(str(user_profile.get("avatar_filename") or "")),
        )

    @app.route("/profile/transfer", methods=["GET", "POST"])
    @login_required
    def transfer():
        user = current_user()
        assert user is not None

        if request.method == "POST":
            recipient_login = (request.form.get("recipient_login") or "").strip()
            amount = decimal_input(request.form.get("amount") or "")

            if not recipient_login:
                flash("Введите логин получателя.", "error")
            elif recipient_login == user["login"]:
                flash("Нельзя отправить перевод самому себе.", "error")
            elif amount is None or amount <= 0:
                flash("Введите корректную сумму больше нуля.", "error")
            else:
                recipient = get_user_by_login(recipient_login)
                if recipient is None:
                    flash("Получатель не найден.", "error")
                else:
                    try:
                        transfer_balance(
                            sender_id=int(user["id"]),
                            recipient_id=int(recipient["id"]),
                            amount=amount,
                            currency_code="UAH",
                        )
                        flash(
                            f"Перевод выполнен: {decimal_to_str(amount)} UAH пользователю {recipient_login}.",
                            "ok",
                        )
                        return redirect(url_for("transfer"))
                    except ValueError:
                        flash("Недостаточно средств для перевода.", "error")
                    except (pymysql.MySQLError, RuntimeError):
                        app.logger.exception(
                            "Database error during transfer from %s to %s",
                            user["login"],
                            recipient_login,
                        )
                        flash("Ошибка базы данных при выполнении перевода.", "error")

        balances = get_balances(user["id"])
        history = get_transfer_history_for_user(int(user["id"]), limit=20)
        users = get_all_user_logins(exclude_user_id=int(user["id"]))
        user_profile = get_user_profile(int(user["id"]))
        return render_template(
            "transfer.html",
            login=user["login"],
            balances=balances,
            history=history,
            user_id=int(user["id"]),
            users=users,
            profile_data=user_profile,
            avatar_url=avatar_url_for(str(user_profile.get("avatar_filename") or "")),
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

    @app.get("/login/style.css")
    def serve_login_css():
        return app.send_static_file("login/style.css")

    @app.get("/profile/profile.css")
    def serve_profile_css():
        return app.send_static_file("profile/profile.css")

    @app.get("/assets/<path:filename>")
    def serve_assets(filename: str):
        return app.send_static_file(f"assets/{filename}")
