"""Apply versioned SQL migrations exactly once."""

from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dotenv
import mysql.connector
from core.runtime_config import get_database_name


MIGRATIONS = ROOT / "migrations"


def main() -> None:
    dotenv.load_dotenv(ROOT / ".env")
    connection = mysql.connector.connect(
        host=os.environ["BOT_DATABASE_HOST"],
        user=os.environ["BOT_DATABASE_USER"],
        password=os.environ["BOT_DATABASE_PASSWORD"],
        database=get_database_name(),
        charset="utf8mb4",
    )
    cursor = connection.cursor()
    try:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(255) PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP) "
            "CHARACTER SET utf8mb4"
        )
        cursor.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cursor.fetchall()}

        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            statements = [statement.strip() for statement in path.read_text(encoding="utf-8").split(";")]
            for statement in filter(None, statements):
                cursor.execute(statement)
            cursor.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            connection.commit()
            print(f"Applied {path.name}")
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
