"""Create the database named in DATABASE_URL (if missing) and apply schema.sql.

    python scripts/setup_db.py
"""
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import ROOT, get_settings  # noqa: E402


def main() -> None:
    dsn = get_settings().database_url
    dbname = conninfo_to_dict(dsn)["dbname"]
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            print(f"created database {dbname}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute((ROOT / "scripts" / "schema.sql").read_text(encoding="utf-8"))
        version = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        print(f"schema applied; pgvector {version[0]}")


if __name__ == "__main__":
    main()
