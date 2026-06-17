from __future__ import annotations

import pymysql
from flask import Flask, redirect, render_template, request, session, url_for

from ..auth import (
    BLOCKED_LOGIN_MESSAGE,
    blocked_until_is_active,
    create_user,
    current_user,
    get_user_by_login,
    is_user_temporarily_blocked,
    verify_password,
)
from ..config import LOGIN_RE
from ..db import create_audit_log


def register_auth_routes(app: Flask) -> None:
    @app.get("/")
    def root():
        return redirect(url_for("login"))

    @app.route("/login/", methods=["GET", "POST"])
    def login():
        error = BLOCKED_LOGIN_MESSAGE if request.args.get("blocked") else None
        user_session = current_user()
        if user_session is not None:
            if is_user_temporarily_blocked(int(user_session["id"])):
                session.clear()
                error = BLOCKED_LOGIN_MESSAGE
            else:
                return redirect(url_for("profile"))

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
                    elif blocked_until_is_active(user.get("blocked_until")):
                        session.clear()
                        error = BLOCKED_LOGIN_MESSAGE
                    elif not verify_password(str(user["password_hash"]), password):
                        error = "Неверный пароль."
                    else:
                        session.clear()
                        session["user_id"] = int(user["id"])
                        session["login"] = str(user["login"])
                        create_audit_log(
                            int(user["id"]),
                            "login",
                            f"Пользователь {user['login']} вошел в систему.",
                        )
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
