from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from secrets import randbelow
from typing import Any

import pymysql
from flask import current_app
from werkzeug.security import generate_password_hash

from .config import (
    DEFAULT_AUTOSAVE_PERCENT,
    DEFAULT_TRANSFER_PIN,
    INITIAL_BALANCES,
    LEGACY_USERS_PATH,
    LOGIN_RE,
    SCHEMA_STATEMENTS,
    db_config,
)
from .utils import decimal_to_str


def get_db():
    return pymysql.connect(**db_config())


def ensure_schema() -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
            _ensure_user_columns(cursor)
            _ensure_user_profile_columns(cursor)
            _ensure_virtual_card_columns(cursor)
        connection.commit()


def _default_transfer_pin_hash() -> str:
    return generate_password_hash(DEFAULT_TRANSFER_PIN)


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


def _ensure_user_columns(cursor) -> None:
    if not _column_exists(cursor, "users", "transfer_pin_hash"):
        cursor.execute("ALTER TABLE users ADD COLUMN transfer_pin_hash VARCHAR(255) NULL AFTER password_hash")
    if not _column_exists(cursor, "users", "blocked_until"):
        cursor.execute("ALTER TABLE users ADD COLUMN blocked_until DATETIME NULL AFTER transfer_pin_hash")

    cursor.execute(
        """
        UPDATE users
        SET transfer_pin_hash = %s
        WHERE transfer_pin_hash IS NULL OR transfer_pin_hash = ''
        """,
        (_default_transfer_pin_hash(),),
    )


def _ensure_user_profile_columns(cursor) -> None:
    if not _column_exists(cursor, "user_profiles", "email"):
        cursor.execute("ALTER TABLE user_profiles ADD COLUMN email VARCHAR(255) NOT NULL DEFAULT '' AFTER last_name")


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
                    INSERT INTO users (login, password_hash, transfer_pin_hash)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        password_hash = users.password_hash,
                        transfer_pin_hash = COALESCE(users.transfer_pin_hash, VALUES(transfer_pin_hash))
                    """,
                    (login_value, password_hash, _default_transfer_pin_hash()),
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


def create_savings_goal(
    user_id: int,
    title: str,
    description: str,
    theme_key: str,
    target_amount: Decimal,
    target_date: date | None = None,
    currency_code: str = "UAH",
) -> int:
    normalized_target = Decimal(str(target_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    normalized_currency = str(currency_code or "UAH").strip().upper()[:3] or "UAH"

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO savings_goals (
                    user_id,
                    title,
                    description,
                    theme_key,
                    currency_code,
                    target_amount,
                    saved_amount,
                    target_date
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    str(title or "").strip()[:120],
                    str(description or "").strip()[:255],
                    str(theme_key or "aurora").strip()[:24] or "aurora",
                    normalized_currency,
                    decimal_to_str(normalized_target),
                    decimal_to_str(Decimal("0.00")),
                    target_date,
                ),
            )
            goal_id = int(cursor.lastrowid)
        connection.commit()
    return goal_id


def get_savings_goals(user_id: int) -> list[dict[str, Any]]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    g.id,
                    g.user_id,
                    g.title,
                    g.description,
                    g.theme_key,
                    g.currency_code,
                    g.target_amount,
                    g.saved_amount,
                    g.target_date,
                    g.created_at,
                    g.updated_at,
                    COUNT(e.id) AS topup_count,
                    MAX(e.created_at) AS last_topup_at
                FROM savings_goals AS g
                LEFT JOIN savings_goal_events AS e
                    ON e.goal_id = g.id
                   AND e.event_type IN ('topup', 'auto_topup')
                WHERE g.user_id = %s
                GROUP BY
                    g.id,
                    g.user_id,
                    g.title,
                    g.description,
                    g.theme_key,
                    g.currency_code,
                    g.target_amount,
                    g.saved_amount,
                    g.target_date,
                    g.created_at,
                    g.updated_at
                ORDER BY
                    (g.saved_amount >= g.target_amount) ASC,
                    COALESCE(g.target_date, DATE('9999-12-31')) ASC,
                    g.created_at DESC,
                    g.id DESC
                """,
                (user_id,),
            )
            return cursor.fetchall()


def get_savings_settings(user_id: int) -> dict[str, Any]:
    default_percent = DEFAULT_AUTOSAVE_PERCENT.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    s.user_id,
                    s.enabled,
                    s.auto_percent,
                    s.target_goal_id,
                    s.created_at,
                    s.updated_at,
                    g.title AS goal_title,
                    g.currency_code AS goal_currency_code,
                    g.target_amount AS goal_target_amount,
                    g.saved_amount AS goal_saved_amount
                FROM savings_settings AS s
                LEFT JOIN savings_goals AS g
                    ON g.id = s.target_goal_id
                   AND g.user_id = s.user_id
                WHERE s.user_id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()

    if row is None:
        return {
            "user_id": user_id,
            "enabled": False,
            "auto_percent": default_percent,
            "target_goal_id": None,
            "goal_title": "",
            "goal_currency_code": "UAH",
            "goal_target_amount": Decimal("0.00"),
            "goal_saved_amount": Decimal("0.00"),
        }

    row["enabled"] = bool(row.get("enabled"))
    row["auto_percent"] = Decimal(str(row.get("auto_percent") or default_percent)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    row["target_goal_id"] = int(row["target_goal_id"]) if row.get("target_goal_id") is not None else None
    row["goal_target_amount"] = Decimal(str(row.get("goal_target_amount") or "0")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    row["goal_saved_amount"] = Decimal(str(row.get("goal_saved_amount") or "0")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return row


def save_savings_settings(
    user_id: int,
    *,
    enabled: bool,
    auto_percent: Decimal,
    target_goal_id: int | None,
) -> dict[str, Any]:
    normalized_percent = Decimal(str(auto_percent)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if normalized_percent <= 0 or normalized_percent > Decimal("100.00"):
        raise ValueError("invalid auto percent")
    if enabled and target_goal_id is None:
        raise ValueError("target goal required")

    resolved_goal_id: int | None = None
    with get_db() as connection:
        with connection.cursor() as cursor:
            if target_goal_id is not None:
                cursor.execute(
                    """
                    SELECT id, currency_code, target_amount, saved_amount
                    FROM savings_goals
                    WHERE id = %s AND user_id = %s
                    LIMIT 1
                    """,
                    (target_goal_id, user_id),
                )
                goal_row = cursor.fetchone()
                if goal_row is None:
                    raise ValueError("goal not found")
                if str(goal_row.get("currency_code") or "UAH").strip().upper() != "UAH":
                    raise ValueError("unsupported goal currency")

                target_amount = Decimal(str(goal_row.get("target_amount") or "0")).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
                saved_amount = Decimal(str(goal_row.get("saved_amount") or "0")).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
                if enabled and saved_amount >= target_amount and target_amount > 0:
                    raise ValueError("goal already completed")
                resolved_goal_id = int(goal_row["id"])

            cursor.execute(
                """
                INSERT INTO savings_settings (user_id, enabled, auto_percent, target_goal_id)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    enabled = VALUES(enabled),
                    auto_percent = VALUES(auto_percent),
                    target_goal_id = VALUES(target_goal_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    1 if enabled else 0,
                    decimal_to_str(normalized_percent),
                    resolved_goal_id,
                ),
            )
        connection.commit()

    return get_savings_settings(user_id)


def get_savings_goal_activity(user_id: int, limit: int = 12) -> list[dict[str, Any]]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    e.id,
                    e.goal_id,
                    e.event_type,
                    e.amount,
                    e.created_at,
                    g.title,
                    g.theme_key,
                    g.currency_code,
                    g.target_amount,
                    g.saved_amount
                FROM savings_goal_events AS e
                JOIN savings_goals AS g ON g.id = e.goal_id
                WHERE e.user_id = %s
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT %s
                """,
                (user_id, max(1, int(limit))),
            )
            return cursor.fetchall()


def top_up_savings_goal(user_id: int, goal_id: int, amount: Decimal) -> dict[str, Any]:
    normalized_amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if normalized_amount <= 0:
        raise ValueError("amount must be positive")

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, currency_code, target_amount, saved_amount
                FROM savings_goals
                WHERE id = %s AND user_id = %s
                LIMIT 1
                FOR UPDATE
                """,
                (goal_id, user_id),
            )
            goal_row = cursor.fetchone()
            if goal_row is None:
                raise ValueError("goal not found")

            if str(goal_row.get("currency_code") or "UAH").strip().upper() != "UAH":
                raise ValueError("unsupported goal currency")

            target_amount = Decimal(str(goal_row.get("target_amount") or "0")).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            saved_amount = Decimal(str(goal_row.get("saved_amount") or "0")).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            remaining_amount = (target_amount - saved_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if remaining_amount <= 0:
                raise ValueError("goal already completed")
            if normalized_amount > remaining_amount:
                raise ValueError("goal top up exceeds remaining amount")

            cursor.execute(
                """
                SELECT amount
                FROM balances
                WHERE user_id = %s AND currency_code = 'UAH'
                LIMIT 1
                FOR UPDATE
                """,
                (user_id,),
            )
            balance_row = cursor.fetchone()
            available_amount = Decimal(str((balance_row or {}).get("amount") or "0")).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            if available_amount < normalized_amount:
                raise ValueError("insufficient funds")

            cursor.execute(
                """
                UPDATE balances
                SET amount = amount - %s
                WHERE user_id = %s AND currency_code = 'UAH'
                """,
                (decimal_to_str(normalized_amount), user_id),
            )
            cursor.execute(
                """
                UPDATE savings_goals
                SET saved_amount = saved_amount + %s
                WHERE id = %s
                """,
                (decimal_to_str(normalized_amount), goal_id),
            )
            cursor.execute(
                """
                INSERT INTO savings_goal_events (goal_id, user_id, event_type, amount)
                VALUES (%s, %s, 'topup', %s)
                """,
                (goal_id, user_id, decimal_to_str(normalized_amount)),
            )
        connection.commit()

    new_saved_amount = (saved_amount + normalized_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    new_remaining_amount = (target_amount - new_saved_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "goal_id": int(goal_row["id"]),
        "title": str(goal_row.get("title") or ""),
        "amount": normalized_amount,
        "saved_amount": new_saved_amount,
        "target_amount": target_amount,
        "remaining_amount": max(Decimal("0.00"), new_remaining_amount),
        "completed": new_saved_amount >= target_amount,
    }


def transfer_balance(
    sender_id: int,
    recipient_id: int,
    amount: Decimal,
    currency_code: str = "UAH",
) -> dict[str, Any]:
    transfer_amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if transfer_amount <= 0:
        raise ValueError("amount must be positive")

    normalized_currency = str(currency_code or "UAH").strip().upper()[:3] or "UAH"
    auto_saved_amount = Decimal("0.00")
    net_recipient_amount = transfer_amount
    auto_saved_goal_id: int | None = None
    auto_saved_goal_title = ""
    auto_saved_completed = False
    transfer_id = 0

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT amount
                FROM balances
                WHERE user_id = %s AND currency_code = %s
                FOR UPDATE
                """,
                (sender_id, normalized_currency),
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
                (decimal_to_str(transfer_amount), sender_id, normalized_currency),
            )
            cursor.execute(
                """
                INSERT INTO balances (user_id, currency_code, amount)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE amount = amount + VALUES(amount)
                """,
                (recipient_id, normalized_currency, decimal_to_str(transfer_amount)),
            )
            cursor.execute(
                """
                INSERT INTO transfers (sender_id, recipient_id, currency_code, amount)
                VALUES (%s, %s, %s, %s)
                """,
                (sender_id, recipient_id, normalized_currency, decimal_to_str(transfer_amount)),
            )
            transfer_id = int(cursor.lastrowid or 0)

            if normalized_currency == "UAH":
                cursor.execute(
                    """
                    SELECT enabled, auto_percent, target_goal_id
                    FROM savings_settings
                    WHERE user_id = %s
                    LIMIT 1
                    """,
                    (recipient_id,),
                )
                settings_row = cursor.fetchone()
                if settings_row and bool(settings_row.get("enabled")) and settings_row.get("target_goal_id") is not None:
                    auto_percent = Decimal(
                        str(settings_row.get("auto_percent") or DEFAULT_AUTOSAVE_PERCENT)
                    ).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP,
                    )
                    if auto_percent > 0:
                        cursor.execute(
                            """
                            SELECT id, title, currency_code, target_amount, saved_amount
                            FROM savings_goals
                            WHERE id = %s AND user_id = %s
                            LIMIT 1
                            FOR UPDATE
                            """,
                            (int(settings_row["target_goal_id"]), recipient_id),
                        )
                        goal_row = cursor.fetchone()
                        if goal_row is not None and str(goal_row.get("currency_code") or "UAH").strip().upper() == "UAH":
                            target_amount = Decimal(str(goal_row.get("target_amount") or "0")).quantize(
                                Decimal("0.01"),
                                rounding=ROUND_HALF_UP,
                            )
                            saved_amount = Decimal(str(goal_row.get("saved_amount") or "0")).quantize(
                                Decimal("0.01"),
                                rounding=ROUND_HALF_UP,
                            )
                            remaining_amount = max(
                                Decimal("0.00"),
                                (target_amount - saved_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                            )
                            proposed_amount = (transfer_amount * auto_percent / Decimal("100")).quantize(
                                Decimal("0.01"),
                                rounding=ROUND_HALF_UP,
                            )
                            auto_saved_amount = min(transfer_amount, remaining_amount, proposed_amount)

                            if auto_saved_amount > 0:
                                cursor.execute(
                                    """
                                    UPDATE balances
                                    SET amount = amount - %s
                                    WHERE user_id = %s AND currency_code = 'UAH'
                                    """,
                                    (decimal_to_str(auto_saved_amount), recipient_id),
                                )
                                cursor.execute(
                                    """
                                    UPDATE savings_goals
                                    SET saved_amount = saved_amount + %s
                                    WHERE id = %s
                                    """,
                                    (decimal_to_str(auto_saved_amount), int(goal_row["id"])),
                                )
                                cursor.execute(
                                    """
                                    INSERT INTO savings_goal_events (goal_id, user_id, event_type, amount)
                                    VALUES (%s, %s, 'auto_topup', %s)
                                    """,
                                    (
                                        int(goal_row["id"]),
                                        recipient_id,
                                        decimal_to_str(auto_saved_amount),
                                    ),
                                )
                                net_recipient_amount = (transfer_amount - auto_saved_amount).quantize(
                                    Decimal("0.01"),
                                    rounding=ROUND_HALF_UP,
                                )
                                auto_saved_goal_id = int(goal_row["id"])
                                auto_saved_goal_title = str(goal_row.get("title") or "")
                                auto_saved_completed = saved_amount + auto_saved_amount >= target_amount
        connection.commit()

    return {
        "transfer_id": transfer_id,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "currency_code": normalized_currency,
        "transfer_amount": transfer_amount,
        "net_recipient_amount": net_recipient_amount,
        "auto_saved_amount": auto_saved_amount,
        "auto_saved_goal_id": auto_saved_goal_id,
        "auto_saved_goal_title": auto_saved_goal_title,
        "auto_saved_applied": auto_saved_amount > 0,
        "auto_saved_completed": auto_saved_completed,
    }


def get_transfer_history_for_user(
    user_id: int,
    limit: int | None = 20,
    direction: str = "all",
    currency_code: str | None = None,
    search_query: str = "",
) -> list[dict[str, Any]]:
    normalized_direction = str(direction or "all").strip().lower()
    normalized_currency = str(currency_code or "").strip().upper()[:3]
    normalized_search = str(search_query or "").strip()
    conditions = ["(t.sender_id = %s OR t.recipient_id = %s)"]
    params: list[Any] = [user_id, user_id]

    if normalized_direction == "incoming":
        conditions.append("t.recipient_id = %s")
        params.append(user_id)
    elif normalized_direction == "outgoing":
        conditions.append("t.sender_id = %s")
        params.append(user_id)

    if normalized_currency:
        conditions.append("t.currency_code = %s")
        params.append(normalized_currency)

    if normalized_search:
        like_value = f"%{normalized_search}%"
        conditions.append("(sender.login LIKE %s OR recipient.login LIKE %s)")
        params.extend([like_value, like_value])

    with get_db() as connection:
        with connection.cursor() as cursor:
            query = """
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
                WHERE
            """
            query += " AND ".join(conditions)
            query += """
                ORDER BY t.created_at DESC, t.id DESC
            """

            if limit is not None:
                query += " LIMIT %s"
                params.append(max(1, int(limit)))

            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

    return rows


def get_financial_history_for_user(
    user_id: int,
    limit: int | None = 20,
    direction: str = "all",
    currency_code: str | None = None,
    search_query: str = "",
) -> list[dict[str, Any]]:
    normalized_direction = str(direction or "all").strip().lower()
    normalized_currency = str(currency_code or "").strip().upper()[:3]
    normalized_search = str(search_query or "").strip()

    transfer_rows = get_transfer_history_for_user(
        user_id=user_id,
        limit=None,
        direction=normalized_direction,
        currency_code=normalized_currency or None,
        search_query=normalized_search,
    )

    goal_rows: list[dict[str, Any]] = []
    if normalized_direction != "incoming" and (not normalized_currency or normalized_currency == "UAH"):
        with get_db() as connection:
            with connection.cursor() as cursor:
                params: list[Any] = [user_id]
                query = """
                    SELECT
                        e.id,
                        e.goal_id,
                        e.event_type,
                        e.amount,
                        e.created_at,
                        g.title,
                        g.currency_code
                    FROM savings_goal_events AS e
                    JOIN savings_goals AS g ON g.id = e.goal_id
                    WHERE e.user_id = %s
                """
                if normalized_search:
                    query += " AND g.title LIKE %s"
                    params.append(f"%{normalized_search}%")
                query += " ORDER BY e.created_at DESC, e.id DESC"
                cursor.execute(query, tuple(params))
                goal_rows = cursor.fetchall()

    history: list[dict[str, Any]] = []
    for row in transfer_rows:
        sender_id = int(row["sender_id"])
        recipient_id = int(row["recipient_id"])
        is_outgoing = sender_id == user_id
        history.append(
            {
                "history_key": f"transfer-{int(row['id'])}",
                "sort_id": int(row["id"]),
                "kind": "transfer",
                "created_at": row.get("created_at"),
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "sender_login": str(row.get("sender_login") or ""),
                "recipient_login": str(row.get("recipient_login") or ""),
                "amount": Decimal(str(row.get("amount") or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                "currency_code": str(row.get("currency_code") or "UAH"),
                "status_label": "Исходящий" if is_outgoing else "Входящий",
                "status_class": "status-pill--out" if is_outgoing else "status-pill--in",
                "operation_label": "Перевод",
            }
        )

    for row in goal_rows:
        event_type = str(row.get("event_type") or "topup")
        history.append(
            {
                "history_key": f"goal-topup-{int(row['id'])}",
                "sort_id": int(row["id"]),
                "kind": "goal_topup",
                "event_type": event_type,
                "created_at": row.get("created_at"),
                "sender_id": user_id,
                "recipient_id": None,
                "sender_login": "Автокопилка" if event_type == "auto_topup" else "Ваш баланс",
                "recipient_login": str(row.get("title") or "Цель накопления"),
                "amount": Decimal(str(row.get("amount") or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                "currency_code": str(row.get("currency_code") or "UAH"),
                "status_label": "Накопление",
                "status_class": "status-pill--goal",
                "operation_label": "Автокопилка" if event_type == "auto_topup" else "Пополнение цели",
            }
        )

    history.sort(
        key=lambda item: (
            item.get("created_at") or datetime.min,
            int(item.get("sort_id") or 0),
        ),
        reverse=True,
    )

    if limit is not None:
        history = history[: max(1, int(limit))]
    return history


def get_sender_daily_transfer_total(sender_id: int, currency_code: str = "UAH") -> Decimal:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount), 0.00) AS total_amount
                FROM transfers
                WHERE sender_id = %s
                  AND currency_code = %s
                  AND created_at >= CURRENT_DATE()
                  AND created_at < DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY)
                """,
                (sender_id, currency_code),
            )
            row = cursor.fetchone()

    return Decimal(str((row or {}).get("total_amount") or "0"))


def get_recent_sender_transfers(sender_id: int, since_seconds: int = 60) -> list[dict[str, Any]]:
    since_dt = (datetime.now(tz=timezone.utc) - timedelta(seconds=max(0, int(since_seconds)))).replace(tzinfo=None)

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, recipient_id, currency_code, amount, created_at
                FROM transfers
                WHERE sender_id = %s
                  AND created_at >= %s
                ORDER BY created_at ASC, id ASC
                """,
                (sender_id, since_dt),
            )
            rows = cursor.fetchall()

    return rows


def create_audit_log(user_id: int, action: str, details: str) -> None:
    normalized_action = str(action or "").strip()[:64]
    normalized_details = str(details or "").strip()
    if not normalized_action or not normalized_details:
        return

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_logs (user_id, action, details)
                VALUES (%s, %s, %s)
                """,
                (user_id, normalized_action, normalized_details),
            )
        connection.commit()


def set_user_blocked_until(user_id: int, blocked_until: datetime | None) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET blocked_until = %s
                WHERE id = %s
                """,
                (blocked_until, user_id),
            )
        connection.commit()


def get_user_blocked_until(user_id: int) -> datetime | None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT blocked_until
                FROM users
                WHERE id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()

    return (row or {}).get("blocked_until")


def create_notification(user_id: int, title: str, kind: str = "info") -> None:
    normalized_title = str(title or "").strip()
    normalized_kind = str(kind or "info").strip().lower()[:16] or "info"
    if not normalized_title:
        return

    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO notifications (user_id, title, kind)
                VALUES (%s, %s, %s)
                """,
                (user_id, normalized_title[:255], normalized_kind),
            )
        connection.commit()


def get_recent_notifications(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, kind, created_at
                FROM notifications
                WHERE user_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (user_id, max(1, int(limit))),
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
                SELECT user_id, first_name, last_name, email, age, avatar_filename
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
            "email": "",
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
    email: str,
    age: int | None,
    avatar_filename: str | None = None,
) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            if avatar_filename is None:
                cursor.execute(
                    """
                    INSERT INTO user_profiles (user_id, first_name, last_name, email, age)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        first_name = VALUES(first_name),
                        last_name = VALUES(last_name),
                        email = VALUES(email),
                        age = VALUES(age)
                    """,
                    (user_id, first_name, last_name, email, age),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO user_profiles (user_id, first_name, last_name, email, age, avatar_filename)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        first_name = VALUES(first_name),
                        last_name = VALUES(last_name),
                        email = VALUES(email),
                        age = VALUES(age),
                        avatar_filename = VALUES(avatar_filename)
                    """,
                    (user_id, first_name, last_name, email, age, avatar_filename),
                )
        connection.commit()


def create_login_2fa_code(
    user_id: int,
    code_hash: str,
    channel: str,
    target: str,
    expires_at: datetime,
) -> int:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_2fa_codes
                SET used_at = %s
                WHERE user_id = %s AND used_at IS NULL
                """,
                (datetime.now(tz=timezone.utc).replace(tzinfo=None), user_id),
            )
            cursor.execute(
                """
                INSERT INTO login_2fa_codes (user_id, code_hash, channel, target, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, code_hash, channel[:16], target[:255], expires_at),
            )
            record_id = int(cursor.lastrowid)
        connection.commit()
    return record_id


def get_active_login_2fa_code(user_id: int) -> dict[str, Any] | None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, user_id, code_hash, channel, target, expires_at, used_at, created_at
                FROM login_2fa_codes
                WHERE user_id = %s
                  AND used_at IS NULL
                  AND expires_at >= %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, datetime.now(tz=timezone.utc).replace(tzinfo=None)),
            )
            return cursor.fetchone()


def mark_login_2fa_code_used(code_id: int) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_2fa_codes
                SET used_at = %s
                WHERE id = %s
                """,
                (datetime.now(tz=timezone.utc).replace(tzinfo=None), code_id),
            )
        connection.commit()


def clear_login_2fa_codes(user_id: int) -> None:
    with get_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_2fa_codes
                SET used_at = %s
                WHERE user_id = %s AND used_at IS NULL
                """,
                (datetime.now(tz=timezone.utc).replace(tzinfo=None), user_id),
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
