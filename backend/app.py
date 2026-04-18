from __future__ import annotations

import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import pymysql
from flask import Flask, flash, redirect, render_template, request, session, url_for
from dotenv import load_dotenv
from pymysql.cursors import DictCursor
from werkzeug.security import check_password_hash, generate_password_hash


LOGIN_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
TWOPLACES = Decimal("0.01")
INITIAL_BALANCES = {
    "UAH": Decimal("15000.00"),
    "USD": Decimal("120.00"),
}
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
LEGACY_USERS_PATH = BACKEND_DIR / "data" / "users.json"
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
)

load_dotenv(PROJECT_DIR / ".env")

app = Flask(
    __name__,
    static_folder=str(FRONTEND_DIR),
    template_folder=str(FRONTEND_DIR / "templates"),
)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
app.logger.setLevel(logging.INFO)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


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
        app.logger.exception("Failed to read legacy users from %s", LEGACY_USERS_PATH)
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
        app.logger.info("Imported %s legacy users into MySQL", imported)


def init_storage() -> None:
    ensure_schema()
    import_legacy_users()


def decimal_to_str(value: Decimal | float | int) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value.quantize(TWOPLACES, rounding=ROUND_HALF_UP), "f")


def decimal_input(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None
    if not value.is_finite():
        return None
    return value


@app.template_filter("money")
def money_filter(value: Any) -> str:
    try:
        return f"{Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP):,.2f}".replace(",", " ")
    except (InvalidOperation, ValueError):
        return "0.00"


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
                INSERT INTO users (login, password_hash)
                VALUES (%s, %s)
                """,
                (login, generate_password_hash(password)),
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
            app.logger.exception("bcrypt is required to verify legacy password hashes")
            return False

        bcrypt_hash = normalized_hash.replace("$2y$", "$2b$", 1).encode("utf-8")
        return bcrypt.checkpw(password.encode("utf-8"), bcrypt_hash)

    return check_password_hash(normalized_hash, password)


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


def fetch_cbr_rates() -> dict[str, Any]:
    request_obj = Request(
        "https://www.cbr-xml-daily.ru/daily_json.js",
        headers={"User-Agent": "Mozilla/5.0 (CurrencyRatesDemo)"},
    )
    try:
        with urlopen(request_obj, timeout=3) as response:
            raw = response.read().decode("utf-8")
    except (TimeoutError, URLError, OSError):
        return {"ok": False, "error": "Не удалось получить курсы (нет соединения)."}

    try:
        import json

        payload = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "Ответ сервера с курсами повреждён."}

    valute = payload.get("Valute")
    if not isinstance(valute, dict):
        return {"ok": False, "error": "Ответ сервера с курсами повреждён."}

    rub_per_1 = {"RUB": Decimal("1")}
    name_map = {"RUB": "Российский рубль"}

    for code, item in valute.items():
        if not isinstance(item, dict):
            continue
        char_code = str(item.get("CharCode") or code)
        name = str(item.get("Name") or char_code)
        try:
            value = Decimal(str(item.get("Value") or "0"))
            nominal = Decimal(str(item.get("Nominal") or "1"))
        except InvalidOperation:
            continue
        if nominal <= 0 or value <= 0:
            continue
        rub_per_1[char_code] = value / nominal
        name_map[char_code] = name

    if "UAH" not in rub_per_1 or rub_per_1["UAH"] <= 0:
        return {"ok": False, "error": "В источнике нет UAH для пересчёта."}

    uah_rate = rub_per_1["UAH"]
    rates = []
    for code, rub in rub_per_1.items():
        if code == "RUB":
            continue
        rates.append(
            {
                "code": code,
                "name": name_map.get(code, code),
                "uah_per_1": float(rub / uah_rate),
            }
        )

    rates.sort(key=lambda item: item["code"])
    return {
        "ok": True,
        "date": str(payload.get("Date") or ""),
        "rates": rates,
        "base": "UAH",
    }


def rates_payload() -> dict[str, Any]:
    cache = session.get("rates_cache")
    if isinstance(cache, dict):
        ts = int(cache.get("ts", 0))
        payload = cache.get("payload")
        if isinstance(payload, dict) and time.time() - ts < 120:
            return payload

    payload = fetch_cbr_rates()
    session["rates_cache"] = {"ts": int(time.time()), "payload": payload}
    return payload


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
            except pymysql.MySQLError:
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
        elif get_user_by_login(login_value) is not None:
            error = "Пользователь с таким логином уже существует."
        else:
            try:
                user_id = create_user(login_value, password)
            except pymysql.MySQLError:
                error = "Не удалось сохранить пользователя в базе."
            else:
                session.clear()
                session["user_id"] = user_id
                session["login"] = login_value
                return redirect(url_for("profile"))

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
                TWOPLACES, rounding=ROUND_HALF_UP
            )
            balances[from_code] = (balances.get(from_code, Decimal("0")) - amount).quantize(
                TWOPLACES, rounding=ROUND_HALF_UP
            )
            balances[to_code] = (balances.get(to_code, Decimal("0")) + to_amount).quantize(
                TWOPLACES, rounding=ROUND_HALF_UP
            )
            update_balances(user["id"], balances)
            flash(
                f"Обмен выполнен: {decimal_to_str(amount)} {from_code} -> {decimal_to_str(to_amount)} {to_code}",
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


if __name__ == "__main__":
    with app.app_context():
        init_storage()
    app.run(host="0.0.0.0", port=int(env("APP_PORT", "8000")), debug=False)
