from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pymysql
from flask import current_app

from .config import INITIAL_BALANCES, LEGACY_USERS_PATH, LOGIN_RE, SCHEMA_STATEMENTS, db_config
from .utils import decimal_to_str


def get_db():
    return pymysql.connect(**db_config())


def ensure_schema() -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
        connection.commit()


def seed_balances(cursor, user_id: int) -> None:
    cursor.executemany(
        """
        INSERT INTO balances (user_id, currency_code, amount)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE amount = VALUES(amount)
        """,
        [
            (user_id, code, decimal_to_str(amount))
            for code, amount in INITIAL_BALANCES.items()
        ],
    )


def import_legacy_users() -> None:
    if not LEGACY_USERS_PATH.exists():
        return

    try:
        raw_users = json.loads(LEGACY_USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current_app.logger.exception("Failed to read legacy users from %s", LEGACY_USERS_PATH)
        return

    if not isinstance(raw_users, list):
        return

    imported = 0
    with get_db() as connection:
        with connection.cursor() as cursor:
            for item in raw_users:
                if not isinstance(item, dict):
                    continue
                login_value = str(item.get("login") or "").strip()
                password_hash = str(item.get("password_hash") or "").strip()
                if not login_value or not password_hash or not LOGIN_RE.fullmatch(login_value):
                    continue
                cursor.execute(
                    """
                    INSERT INTO users (login, password_hash)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE password_hash = users.password_hash
                    """,
                    (login_value, password_hash),
                )
                cursor.execute("SELECT id FROM users WHERE login = %s LIMIT 1", (login_value,))
                user = cursor.fetchone()
                if user is None:
                    continue
                seed_balances(cursor, int(user["id"]))
                imported += 1
        connection.commit()

    if imported:
        current_app.logger.info("Imported %s legacy users into MySQL", imported)


def init_storage() -> None:
    ensure_schema()
    import_legacy_users()


def get_balances(user_id: int) -> dict[str, Decimal]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT currency_code, amount
                FROM balances
                WHERE user_id = %s
                """,
                (user_id,),
            )
            rows = cursor.fetchall()

    balances = {row["currency_code"]: Decimal(str(row["amount"])) for row in rows}
    for code, default_amount in INITIAL_BALANCES.items():
        balances.setdefault(code, default_amount)
    return balances


def update_balances(user_id: int, balances: dict[str, Decimal]) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO balances (user_id, currency_code, amount)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE amount = VALUES(amount)
                """,
                [
                    (user_id, code, decimal_to_str(amount))
                    for code, amount in balances.items()
                ],
            )
        connection.commit()
