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

load_dotenv(PROJECT_DIR / ".env")

LOGIN_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
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
)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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
