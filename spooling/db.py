"""Database connection management."""

import psycopg
from psycopg.rows import dict_row

from spooling.config import DATABASE_URL


def get_connection():
    """Get a database connection."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def check_db():
    """Check if the database is reachable."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
