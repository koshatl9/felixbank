from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymysql.cursors import DictCursor


BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
LEGACY_USERS_PATH = BACKEND_DIR / "data" / "users.json"

# Важно: override=True, чтобы локальные переменные окружения не "перебивали" .env.
# Иначе легко получить MYSQL_PORT=3306 из глобальной среды и "Can't connect".
load_dotenv(PROJECT_DIR / ".env", override=True)

LOGIN_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
TRANSFER_PIN_RE = re.compile(r"^\d{4}$")
TWOPLACES = Decimal("0.01")
INITIAL_BALANCES = {
    "UAH": Decimal("15000.00"),
    "USD": Decimal("120.00"),
}
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        login VARCHAR(32) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        transfer_pin_hash VARCHAR(255) NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balances (
        user_id INT NOT NULL,
        currency_code CHAR(3) NOT NULL,
        amount DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
        PRIMARY KEY (user_id, currency_code),
        CONSTRAINT fk_balances_user
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfers (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        sender_id INT NOT NULL,
        recipient_id INT NOT NULL,
        currency_code CHAR(3) NOT NULL DEFAULT 'UAH',
        amount DECIMAL(12, 2) NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_transfers_sender
            FOREIGN KEY (sender_id) REFERENCES users(id)
            ON DELETE CASCADE,
        CONSTRAINT fk_transfers_recipient
            FOREIGN KEY (recipient_id) REFERENCES users(id)
            ON DELETE CASCADE,
        INDEX idx_transfers_sender_created (sender_id, created_at),
        INDEX idx_transfers_recipient_created (recipient_id, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id INT NOT NULL PRIMARY KEY,
        first_name VARCHAR(64) NOT NULL DEFAULT '',
        last_name VARCHAR(64) NOT NULL DEFAULT '',
        age INT NULL,
        avatar_filename VARCHAR(255) NOT NULL DEFAULT '',
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_user_profiles_user
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS currency_rates_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        currency_code CHAR(3) NOT NULL,
        uah_per_1 DECIMAL(18, 6) NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_rates_history_code_time (currency_code, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS virtual_cards (
        user_id INT NOT NULL PRIMARY KEY,
        card_number CHAR(19) NULL,
        expiry_month TINYINT UNSIGNED NULL,
        expiry_year SMALLINT UNSIGNED NULL,
        cvv CHAR(3) NULL,
        is_blocked TINYINT(1) NOT NULL DEFAULT 0,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_virtual_cards_user
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        title VARCHAR(255) NOT NULL,
        kind VARCHAR(16) NOT NULL DEFAULT 'info',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_notifications_user
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE,
        INDEX idx_notifications_user_created (user_id, created_at)
    )
    """,
)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def env_decimal(name: str, default: str) -> Decimal:
    raw = env(name, default).strip() or default
    try:
        return Decimal(raw)
    except Exception:
        return Decimal(default)


DEFAULT_TRANSFER_PIN = env("DEFAULT_TRANSFER_PIN", "1234")
TRANSFER_MIN_AMOUNT_UAH = env_decimal("TRANSFER_MIN_AMOUNT_UAH", "10")
TRANSFER_MAX_AMOUNT_UAH = env_decimal("TRANSFER_MAX_AMOUNT_UAH", "50000")
TRANSFER_DAILY_LIMIT_UAH = env_decimal("TRANSFER_DAILY_LIMIT_UAH", "100000")


def running_in_docker() -> bool:
    return Path("/.dockerenv").exists()


def default_mysql_host() -> str:
    return "db" if running_in_docker() else "127.0.0.1"


def default_mysql_port() -> int:
    return 3306 if running_in_docker() else 3307


def mysql_host() -> str:
    variable_name = "MYSQL_HOST_DOCKER" if running_in_docker() else "MYSQL_HOST_LOCAL"
    return env("MYSQL_HOST", env(variable_name, default_mysql_host()))


def mysql_port() -> int:
    variable_name = "MYSQL_PORT_DOCKER" if running_in_docker() else "MYSQL_PORT_LOCAL"
    return int(env("MYSQL_PORT", env(variable_name, str(default_mysql_port()))))


def db_config() -> dict[str, Any]:
    return {
        "host": mysql_host(),
        "port": mysql_port(),
        "user": env("MYSQL_USER", "felixbank"),
        "password": env("MYSQL_PASSWORD", "felixbank"),
        "database": env("MYSQL_DATABASE", "felixbank"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
    }
