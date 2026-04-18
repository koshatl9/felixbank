from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pymysql
from flask import Flask, flash, redirect, render_template, request, session, url_for

from .auth import create_user, current_user, get_user_by_login, login_required, verify_password
from .config import LOGIN_RE, TWOPLACES
from .db import get_balances, update_balances
from .rates import rates_payload
from .utils import decimal_input, decimal_to_str


def register_routes(app: Flask) -> None:
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
        return render_template(
            "profile.html",
            login=user["login"],
            balances=balances,
            rates_uah_per_1=rates_uah_per_1,
        )

    @app.get("/profile/index.html")
    def profile_legacy():
        return redirect(url_for("profile"))

    @app.get("/profile/rates")
    @app.get("/profile/rates.php")
    @login_required
    def rates():
        user = current_user()
        assert user is not None

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
