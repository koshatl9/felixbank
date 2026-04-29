from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from secrets import randbelow
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
            _ensure_virtual_card_columns(cursor)
        connection.commit()


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS column_count
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND column_name = %s
        """,
        (table_name, column_name),
    )
    row = cursor.fetchone()
    return bool(row and int(row.get("column_count") or 0))


def _ensure_virtual_card_columns(cursor) -> None:
    column_definitions = (
        ("card_number", "CHAR(19) NULL AFTER user_id"),
        ("expiry_month", "TINYINT UNSIGNED NULL AFTER card_number"),
        ("expiry_year", "SMALLINT UNSIGNED NULL AFTER expiry_month"),
        ("cvv", "CHAR(3) NULL AFTER expiry_year"),
    )
    for column_name, definition in column_definitions:
        if _column_exists(cursor, "virtual_cards", column_name):
            continue
        cursor.execute(f"ALTER TABLE virtual_cards ADD COLUMN {column_name} {definition}")


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


def normalize_virtual_card_number(raw_value: str) -> str:
    digits = "".join(char for char in str(raw_value or "") if char.isdigit())
    if len(digits) != 16:
        return ""
    return f"{digits[0:4]} {digits[4:8]} {digits[8:12]} {digits[12:16]}"


def get_user_by_virtual_card_number(card_number: str) -> dict[str, Any] | None:
    normalized_number = normalize_virtual_card_number(card_number)
    if not normalized_number:
        return None

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    users.id,
                    users.login,
                    virtual_cards.card_number,
                    virtual_cards.is_blocked
                FROM virtual_cards
                JOIN users ON users.id = virtual_cards.user_id
                WHERE virtual_cards.card_number = %s
                LIMIT %s
                """,
                (normalized_number, 1),
            )
            return cursor.fetchone()


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


def get_virtual_card_blocked(user_id: int) -> bool:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT is_blocked
                FROM virtual_cards
                WHERE user_id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()

    if row is None:
        return False
    return bool(row.get("is_blocked"))


def _luhn_check_digit(number_without_check: str) -> str:
    digits = [int(char) for char in number_without_check]
    checksum = 0
    parity = (len(digits) + 1) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return str((10 - (checksum % 10)) % 10)


def _generate_unique_virtual_card_number(cursor) -> str:
    while True:
        prefix = "5412"
        body = "".join(str(randbelow(10)) for _ in range(11))
        raw_number = prefix + body
        check_digit = _luhn_check_digit(raw_number)
        digits = raw_number + check_digit
        formatted_number = f"{digits[0:4]} {digits[4:8]} {digits[8:12]} {digits[12:16]}"
        cursor.execute(
            """
            SELECT user_id
            FROM virtual_cards
            WHERE card_number = %s
            LIMIT 1
            """,
            (formatted_number,),
        )
        if cursor.fetchone() is None:
            return formatted_number


def get_or_create_virtual_card(user_id: int, login_value: str) -> dict[str, Any]:
    holder = login_value.upper()[:24]

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT card_number, expiry_month, expiry_year, cvv, is_blocked
                FROM virtual_cards
                WHERE user_id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()

            if (
                row is not None
                and row.get("card_number")
                and row.get("expiry_month") is not None
                and row.get("expiry_year") is not None
                and row.get("cvv")
            ):
                expiry_month = int(row["expiry_month"])
                expiry_year = int(row["expiry_year"])
                return {
                    "number": str(row["card_number"]),
                    "expiry": f"{expiry_month:02d}/{expiry_year % 100:02d}",
                    "cvv": str(row["cvv"]),
                    "holder": holder,
                    "blocked": bool(row.get("is_blocked")),
                }

            card_number = _generate_unique_virtual_card_number(cursor)
            expiry_month = randbelow(12) + 1
            expiry_year = datetime.now(tz=timezone.utc).year + 3 + randbelow(4)
            cvv = f"{randbelow(1000):03d}"

            if row is None:
                cursor.execute(
                    """
                    INSERT INTO virtual_cards (
                        user_id,
                        card_number,
                        expiry_month,
                        expiry_year,
                        cvv,
                        is_blocked
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (user_id, card_number, expiry_month, expiry_year, cvv, 0),
                )
                blocked = False
            else:
                blocked = bool(row.get("is_blocked"))
                cursor.execute(
                    """
                    UPDATE virtual_cards
                    SET card_number = %s,
                        expiry_month = %s,
                        expiry_year = %s,
                        cvv = %s
                    WHERE user_id = %s
                    """,
                    (card_number, expiry_month, expiry_year, cvv, user_id),
                )
        connection.commit()

    return {
        "number": card_number,
        "expiry": f"{expiry_month:02d}/{expiry_year % 100:02d}",
        "cvv": cvv,
        "holder": holder,
        "blocked": blocked,
    }


def set_virtual_card_blocked(user_id: int, is_blocked: bool) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO virtual_cards (user_id, is_blocked)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE is_blocked = VALUES(is_blocked)
                """,
                (user_id, 1 if is_blocked else 0),
            )
        connection.commit()


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


def insert_rates_history(points: list[dict[str, Any]]) -> None:
    if not points:
        return
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO currency_rates_history (currency_code, uah_per_1)
                VALUES (%s, %s)
                """,
                [(str(p["code"]), str(p["uah_per_1"])) for p in points if p.get("code") and p.get("uah_per_1")],
            )
        connection.commit()


def get_rates_history(
    currency_code: str,
    since_seconds: int,
    limit: int,
    aggregate_by_day: bool = False,
) -> list[dict[str, Any]]:
    code = (currency_code or "").strip().upper()[:3]
    if not code:
        return []
    since = datetime.now(tz=timezone.utc) - timedelta(seconds=max(0, int(since_seconds)))
    with get_db() as connection:
        with connection.cursor() as cursor:
            if aggregate_by_day:
                cursor.execute(
                    """
                    SELECT
                        UNIX_TIMESTAMP(created_at) * 1000 AS ts,
                        uah_per_1 AS value
                    FROM currency_rates_history
                    WHERE id IN (
                        SELECT max_ids.id
                        FROM (
                            SELECT MAX(id) AS id
                            FROM currency_rates_history
                            WHERE currency_code = %s AND created_at >= %s
                            GROUP BY DATE(created_at)
                            ORDER BY DATE(created_at) DESC
                            LIMIT %s
                        ) AS max_ids
                    )
                    ORDER BY created_at ASC, id ASC
                    """,
                    (code, since.replace(tzinfo=None), int(limit)),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        UNIX_TIMESTAMP(created_at) * 1000 AS ts,
                        uah_per_1 AS value
                    FROM currency_rates_history
                    WHERE currency_code = %s AND created_at >= %s
                    ORDER BY created_at ASC, id ASC
                    LIMIT %s
                    """,
                    (code, since.replace(tzinfo=None), int(limit)),
                )
            rows = cursor.fetchall()
            if len(rows) < 2 or (aggregate_by_day and len(rows) < 7):
                cursor.execute(
                    """
                    SELECT ts, value
                    FROM (
                        SELECT
                            UNIX_TIMESTAMP(created_at) * 1000 AS ts,
                            uah_per_1 AS value
                        FROM currency_rates_history
                        WHERE currency_code = %s
                        ORDER BY created_at DESC, id DESC
                        LIMIT %s
                    ) AS latest_points
                    ORDER BY ts ASC
                    """,
                    (code, min(int(limit), 180)),
                )
                rows = cursor.fetchall()
    return [{"ts": int(row["ts"]), "value": float(row["value"])} for row in rows]


def get_recent_rates_rows(currency_code: str, limit: int = 8) -> list[dict[str, Any]]:
    code = (currency_code or "").strip().upper()[:3]
    if not code:
        return []

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT currency_code, uah_per_1, created_at
                FROM currency_rates_history
                WHERE currency_code = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (code, max(1, int(limit))),
            )
            rows = cursor.fetchall()

    return rows


def get_rates_history_between_dates(
    currency_code: str,
    start_date: date,
    end_date: date,
    limit: int,
    aggregate_by_day: bool = False,
) -> list[dict[str, Any]]:
    code = (currency_code or "").strip().upper()[:3]
    if not code:
        return []

    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min)

    with get_db() as connection:
        with connection.cursor() as cursor:
            if aggregate_by_day:
                cursor.execute(
                    """
                    SELECT
                        UNIX_TIMESTAMP(created_at) * 1000 AS ts,
                        uah_per_1 AS value
                    FROM currency_rates_history
                    WHERE id IN (
                        SELECT max_ids.id
                        FROM (
                            SELECT MAX(id) AS id
                            FROM currency_rates_history
                            WHERE currency_code = %s
                              AND created_at >= %s
                              AND created_at < %s
                            GROUP BY DATE(created_at)
                            ORDER BY DATE(created_at) DESC
                            LIMIT %s
                        ) AS max_ids
                    )
                    ORDER BY created_at ASC, id ASC
                    """,
                    (code, start_dt, end_dt, int(limit)),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        UNIX_TIMESTAMP(created_at) * 1000 AS ts,
                        uah_per_1 AS value
                    FROM currency_rates_history
                    WHERE currency_code = %s
                      AND created_at >= %s
                      AND created_at < %s
                    ORDER BY created_at ASC, id ASC
                    LIMIT %s
                    """,
                    (code, start_dt, end_dt, int(limit)),
                )
            rows = cursor.fetchall()

            if len(rows) < 2 and aggregate_by_day:
                cursor.execute(
                    """
                    SELECT
                        UNIX_TIMESTAMP(created_at) * 1000 AS ts,
                        uah_per_1 AS value
                    FROM currency_rates_history
                    WHERE currency_code = %s
                      AND created_at >= %s
                      AND created_at < %s
                    ORDER BY created_at ASC, id ASC
                    LIMIT %s
                    """,
                    (code, start_dt, end_dt, min(int(limit), 1000)),
                )
                rows = cursor.fetchall()

    return [{"ts": int(row["ts"]), "value": float(row["value"])} for row in rows]


def get_recent_rates_rows_in_range(
    currency_code: str,
    start_date: date,
    end_date: date,
    limit: int = 8,
) -> list[dict[str, Any]]:
    code = (currency_code or "").strip().upper()[:3]
    if not code:
        return []

    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min)

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT currency_code, uah_per_1, created_at
                FROM currency_rates_history
                WHERE currency_code = %s
                  AND created_at >= %s
                  AND created_at < %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (code, start_dt, end_dt, max(1, int(limit))),
            )
            rows = cursor.fetchall()

    return rows
