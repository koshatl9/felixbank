from __future__ import annotations

from functools import wraps
from typing import Any

from flask import current_app, redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .config import DEFAULT_TRANSFER_PIN, TRANSFER_PIN_RE
from .db import get_db, seed_balances


def current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    login = session.get("login")
    if not user_id or not login:
        return None
    return {"id": int(user_id), "login": str(login)}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def get_user_by_login(login: str) -> dict[str, Any] | None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, login, password_hash
                FROM users
                WHERE login = %s
                LIMIT 1
                """,
                (login,),
            )
            return cursor.fetchone()


def create_user(login: str, password: str) -> int:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (login, password_hash, transfer_pin_hash)
                VALUES (%s, %s, %s)
                """,
                (login, generate_password_hash(password), generate_password_hash(DEFAULT_TRANSFER_PIN)),
            )
            user_id = int(cursor.lastrowid)
            seed_balances(cursor, user_id)
        connection.commit()
    return user_id


def verify_password(password_hash: str, password: str) -> bool:
    normalized_hash = password_hash.strip()
    if normalized_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            import bcrypt
        except ImportError:
            current_app.logger.exception("bcrypt is required to verify legacy password hashes")
            return False

        bcrypt_hash = normalized_hash.replace("$2y$", "$2b$", 1).encode("utf-8")
        return bcrypt.checkpw(password.encode("utf-8"), bcrypt_hash)

    return check_password_hash(normalized_hash, password)


def verify_transfer_pin(user_id: int, pin: str) -> bool:
    normalized_pin = str(pin or "").strip()
    if not TRANSFER_PIN_RE.fullmatch(normalized_pin):
        return False

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT transfer_pin_hash
                FROM users
                WHERE id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()

    pin_hash = str((row or {}).get("transfer_pin_hash") or "").strip()
    if not pin_hash:
        return normalized_pin == DEFAULT_TRANSFER_PIN
    return check_password_hash(pin_hash, normalized_pin)
