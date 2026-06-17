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
from ..config import LOGIN_2FA_CODE_TTL_SECONDS, LOGIN_RE
from ..db import create_audit_log, get_user_profile, save_user_profile
from ..two_factor import (
    TwoFactorDeliveryError,
    TwoFactorSetupError,
    issue_login_2fa_code,
    verify_login_2fa_code,
)


def register_auth_routes(app: Flask) -> None:
    @app.get("/")
    def root():
        return redirect(url_for("login"))

    @app.route("/login/", methods=["GET", "POST"])
    def login():
        error = BLOCKED_LOGIN_MESSAGE if request.args.get("blocked") else None
        login_prefill = ""
        email_prefill = ""
        if request.args.get("reset_2fa"):
            for key in (
                "pending_2fa_user_id",
                "pending_2fa_login",
                "pending_2fa_channel",
                "pending_2fa_target_masked",
            ):
                session.pop(key, None)

        user_session = current_user()
        if user_session is not None:
            if is_user_temporarily_blocked(int(user_session["id"])):
                session.clear()
                error = BLOCKED_LOGIN_MESSAGE
            else:
                return redirect(url_for("profile"))

        if request.method == "GET" and session.get("pending_2fa_user_id") and session.get("pending_2fa_login"):
            return redirect(url_for("login_verify"))

        if request.method == "POST":
            login_value = (request.form.get("login") or "").strip()
            login_email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            login_prefill = login_value
            email_prefill = login_email

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
                        normalized_email = str(user.get("email") or "").strip()
                        if not normalized_email:
                            if not login_email:
                                error = "Для старого аккаунта укажите email в форме входа. Мы сохраним его и отправим код."
                            elif "@" not in login_email or login_email.startswith("@") or login_email.endswith("@"):
                                error = "Введите корректный email для получения кода."
                            else:
                                profile = get_user_profile(int(user["id"]))
                                save_user_profile(
                                    user_id=int(user["id"]),
                                    first_name=str(profile.get("first_name") or ""),
                                    last_name=str(profile.get("last_name") or ""),
                                    email=login_email,
                                    age=profile.get("age"),
                                )
                                normalized_email = login_email
                                email_prefill = login_email

                        if error:
                            return render_template(
                                "login.html",
                                error=error,
                                login_value=login_prefill,
                                email_value=email_prefill,
                            )

                        try:
                            issue = issue_login_2fa_code(
                                int(user["id"]),
                                str(user["login"]),
                                normalized_email,
                            )
                        except (TwoFactorSetupError, TwoFactorDeliveryError) as exc:
                            error = str(exc)
                        else:
                            session.clear()
                            session["pending_2fa_user_id"] = int(user["id"])
                            session["pending_2fa_login"] = str(user["login"])
                            session["pending_2fa_channel"] = issue["channel"]
                            session["pending_2fa_target_masked"] = issue["target_masked"]
                            return redirect(url_for("login_verify"))
                except (pymysql.MySQLError, RuntimeError):
                    app.logger.exception("Database error during login for %s", login_value)
                    error = "Не удалось подключиться к базе данных. Проверьте настройки MySQL."
                except ValueError:
                    app.logger.exception("Unsupported password hash for %s", login_value)
                    error = "Формат пароля этого пользователя не поддерживается."

        return render_template(
            "login.html",
            error=error,
            login_value=login_prefill,
            email_value=email_prefill,
        )

    @app.route("/login/verify/", methods=["GET", "POST"])
    def login_verify():
        user_session = current_user()
        if user_session is not None:
            if is_user_temporarily_blocked(int(user_session["id"])):
                session.clear()
                return redirect(url_for("login", blocked=1))
            return redirect(url_for("profile"))

        pending_user_id = session.get("pending_2fa_user_id")
        pending_login = str(session.get("pending_2fa_login") or "").strip()
        if not pending_user_id or not pending_login:
            return redirect(url_for("login"))

        error = None
        info = None

        try:
            user = get_user_by_login(pending_login)
            if user is None or int(user["id"]) != int(pending_user_id):
                session.clear()
                return redirect(url_for("login"))

            if blocked_until_is_active(user.get("blocked_until")):
                session.clear()
                return redirect(url_for("login", blocked=1))

            if request.method == "POST":
                action = str(request.form.get("action") or "verify").strip().lower()
                if action == "resend":
                    try:
                        issue = issue_login_2fa_code(
                            int(user["id"]),
                            str(user["login"]),
                            str(user.get("email") or ""),
                        )
                    except (TwoFactorSetupError, TwoFactorDeliveryError) as exc:
                        error = str(exc)
                    else:
                        session["pending_2fa_channel"] = issue["channel"]
                        session["pending_2fa_target_masked"] = issue["target_masked"]
                        info = "Новый код отправлен на email."
                else:
                    submitted_code = request.form.get("code") or ""
                    if verify_login_2fa_code(int(user["id"]), submitted_code):
                        session.clear()
                        session["user_id"] = int(user["id"])
                        session["login"] = str(user["login"])
                        create_audit_log(
                            int(user["id"]),
                            "login",
                            f"Пользователь {user['login']} вошел в систему через 2FA.",
                        )
                        return redirect(url_for("profile"))
                    error = "Неверный или просроченный код."
        except (pymysql.MySQLError, RuntimeError):
            app.logger.exception("Database error during 2FA verification for %s", pending_login)
            error = "Не удалось проверить код подтверждения."

        channel = str(session.get("pending_2fa_channel") or "email")
        target_masked = str(session.get("pending_2fa_target_masked") or "")
        return render_template(
            "login_verify.html",
            error=error,
            info=info,
            login_value=pending_login,
            channel=channel,
            target_masked=target_masked,
            expires_minutes=max(1, LOGIN_2FA_CODE_TTL_SECONDS // 60),
        )

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
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""

            if not login_value:
                error = "Введите логин."
            elif not email:
                error = "Введите email для подтверждения входа."
            elif "@" not in email or email.startswith("@") or email.endswith("@"):
                error = "Введите корректный email."
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
                        save_user_profile(
                            user_id=user_id,
                            first_name="",
                            last_name="",
                            email=email,
                            age=None,
                        )
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
