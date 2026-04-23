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


def transfer_balance(sender_id: int, recipient_id: int, amount: Decimal, currency_code: str = "UAH") -> None:
    transfer_amount = amount.quantize(Decimal("0.01"))
    if transfer_amount <= 0:
        raise ValueError("amount must be positive")

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT amount
                FROM balances
                WHERE user_id = %s AND currency_code = %s
                FOR UPDATE
                """,
                (sender_id, currency_code),
            )
            sender_row = cursor.fetchone()
            sender_balance = Decimal(str(sender_row["amount"])) if sender_row is not None else Decimal("0")
            if sender_balance < transfer_amount:
                raise ValueError("insufficient funds")

            cursor.execute(
                """
                UPDATE balances
                SET amount = amount - %s
                WHERE user_id = %s AND currency_code = %s
                """,
                (decimal_to_str(transfer_amount), sender_id, currency_code),
            )
            cursor.execute(
                """
                INSERT INTO balances (user_id, currency_code, amount)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE amount = amount + VALUES(amount)
                """,
                (recipient_id, currency_code, decimal_to_str(transfer_amount)),
            )
            cursor.execute(
                """
                INSERT INTO transfers (sender_id, recipient_id, currency_code, amount)
                VALUES (%s, %s, %s, %s)
                """,
                (sender_id, recipient_id, currency_code, decimal_to_str(transfer_amount)),
            )
        connection.commit()


def get_transfer_history_for_user(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    t.id,
                    t.sender_id,
                    t.recipient_id,
                    t.currency_code,
                    t.amount,
                    t.created_at,
                    sender.login AS sender_login,
                    recipient.login AS recipient_login
                FROM transfers AS t
                JOIN users AS sender ON sender.id = t.sender_id
                JOIN users AS recipient ON recipient.id = t.recipient_id
                WHERE t.sender_id = %s OR t.recipient_id = %s
                ORDER BY t.created_at DESC, t.id DESC
                LIMIT %s
                """,
                (user_id, user_id, limit),
            )
            rows = cursor.fetchall()

    return rows


def get_all_user_logins(exclude_user_id: int | None = None) -> list[str]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            if exclude_user_id is None:
                cursor.execute(
                    """
                    SELECT login
                    FROM users
                    ORDER BY login ASC
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT login
                    FROM users
                    WHERE id <> %s
                    ORDER BY login ASC
                    """,
                    (exclude_user_id,),
                )
            rows = cursor.fetchall()

    return [str(row["login"]) for row in rows]


def get_user_profile(user_id: int) -> dict[str, Any]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, first_name, last_name, age, avatar_filename
                FROM user_profiles
                WHERE user_id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()

    if row is None:
        return {
            "user_id": user_id,
            "first_name": "",
            "last_name": "",
            "age": None,
            "avatar_filename": "",
        }
    return row


def save_user_profile(
    user_id: int,
    first_name: str,
    last_name: str,
    age: int | None,
    avatar_filename: str | None = None,
) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            if avatar_filename is None:
                cursor.execute(
                    """
                    INSERT INTO user_profiles (user_id, first_name, last_name, age)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        first_name = VALUES(first_name),
                        last_name = VALUES(last_name),
                        age = VALUES(age)
                    """,
                    (user_id, first_name, last_name, age),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO user_profiles (user_id, first_name, last_name, age, avatar_filename)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        first_name = VALUES(first_name),
                        last_name = VALUES(last_name),
                        age = VALUES(age),
                        avatar_filename = VALUES(avatar_filename)
                    """,
                    (user_id, first_name, last_name, age, avatar_filename),
                )
        connection.commit()
