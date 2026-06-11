"""通用 SQL 執行器（直連 Supabase Postgres）。
用法：python supabase/run_sql.py <sql檔路徑> <db密碼>
"""
import sys
from pathlib import Path

import psycopg2

HOST = "db.djcebqribkmtrhkoytaq.supabase.co"

sql_path, password = sys.argv[1], sys.argv[2]
sql = Path(sql_path).read_text(encoding="utf-8")

conn = psycopg2.connect(host=HOST, port=5432, dbname="postgres",
                        user="postgres", password=password, connect_timeout=15)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(sql)
    cur.execute("NOTIFY pgrst, 'reload schema'")
conn.close()
print(f"OK: {sql_path} applied, schema cache reloaded")
