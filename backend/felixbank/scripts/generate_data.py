from __future__ import annotations

import mysql.connector
from dotenv import load_dotenv

from ..config import PROJECT_DIR, mysql_host, mysql_port


load_dotenv(PROJECT_DIR / ".env", override=True)

conn = mysql.connector.connect(
    host=mysql_host(),
    port=mysql_port(),
    user="felixbank",
    password="felixbank",
    database="felixbank",
)

cursor = conn.cursor()

query = """
INSERT INTO currency_rates_history (currency_code, uah_per_1, created_at)
SELECT 
    currency,
    base_rate + (RAND() - 0.5) * variation AS rate,
    NOW() - INTERVAL seq * 10 MINUTE AS created_at
FROM (
    SELECT 'EUR' AS currency, 43.0000 AS base_rate, 0.1200 AS variation
    UNION ALL
    SELECT 'USD', 39.5000, 0.1200
    UNION ALL
    SELECT 'JPY', 0.2600, 0.0100
) currencies
JOIN (
    SELECT @row := @row + 1 AS seq
    FROM 
        (SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) t1,
        (SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) t2,
        (SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) t3,
        (SELECT @row := 0) r
    LIMIT 300
) seq_data;
"""

cursor.execute(query)
conn.commit()

cursor.close()
conn.close()

print("Данные сгенерированы 🚀")