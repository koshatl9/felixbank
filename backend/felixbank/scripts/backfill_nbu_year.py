from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pymysql

from ..config import db_config


DEFAULT_CODES = ("USD", "EUR", "JPY", "KRW", "CNY")
NBU_RANGE_URL = "https://bank.gov.ua/NBU_Exchange/exchange_site"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load up to one year of official NBU currency history into MySQL.",
    )
    parser.add_argument(
        "--codes",
        nargs="+",
        default=list(DEFAULT_CODES),
        help="Currency codes to import, for example: USD EUR JPY KRW CNY",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="How many past days to import. Default: 365",
    )
    parser.add_argument(
        "--end-date",
        default=date.today().isoformat(),
        help="Inclusive end date in YYYY-MM-DD format. Default: today",
    )
    return parser.parse_args()


def build_url(currency_code: str, start_date: date, end_date: date) -> str:
    query = urlencode(
        {
            "start": start_date.strftime("%Y%m%d"),
            "end": end_date.strftime("%Y%m%d"),
            "valcode": currency_code,
            "json": "",
            "order": "asc",
            "sort": "exchangedate",
        }
    )
    return f"{NBU_RANGE_URL}?{query}"


def fetch_currency_range(currency_code: str, start_date: date, end_date: date) -> list[dict[str, object]]:
    request = Request(
        build_url(currency_code, start_date, end_date),
        headers={"User-Agent": "FelixBank/1.0"},
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, list):
        raise ValueError(f"Unexpected NBU payload for {currency_code}")

    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        exchangedate = str(item.get("exchangedate") or "").strip()
        rate = item.get("rate")
        code = str(item.get("cc") or currency_code).strip().upper()
        if not exchangedate or rate in (None, ""):
            continue
        try:
            day_value = datetime.strptime(exchangedate, "%d.%m.%Y").date()
            rate_value = float(rate)
        except ValueError:
            continue
        rows.append(
            {
                "currency_code": code,
                "created_at": datetime.combine(day_value, time(hour=12, minute=0)),
                "uah_per_1": rate_value,
            }
        )
    return rows


def upsert_rows(connection: pymysql.Connection, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0

    with connection.cursor() as cursor:
        for row in rows:
            cursor.execute(
                """
                DELETE FROM currency_rates_history
                WHERE currency_code = %s AND created_at = %s
                """,
                (row["currency_code"], row["created_at"]),
            )
        cursor.executemany(
            """
            INSERT INTO currency_rates_history (currency_code, uah_per_1, created_at)
            VALUES (%s, %s, %s)
            """,
            [
                (
                    str(row["currency_code"]),
                    f"{float(row['uah_per_1']):.6f}",
                    row["created_at"],
                )
                for row in rows
            ],
        )
    connection.commit()
    return len(rows)


def main() -> None:
    args = parse_args()
    end_date = datetime.strptime(str(args.end_date), "%Y-%m-%d").date()
    start_date = end_date - timedelta(days=max(1, int(args.days)) - 1)
    codes = [str(code).strip().upper() for code in args.codes if str(code).strip()]

    imported_total = 0
    with pymysql.connect(**db_config()) as connection:
        for code in codes:
            rows = fetch_currency_range(code, start_date, end_date)
            inserted = upsert_rows(connection, rows)
            imported_total += inserted
            print(f"{code}: inserted {inserted} rows")

    print(
        "Completed NBU backfill: "
        f"{imported_total} rows for {', '.join(codes)} "
        f"from {start_date.isoformat()} to {end_date.isoformat()}"
    )


if __name__ == "__main__":
    main()
