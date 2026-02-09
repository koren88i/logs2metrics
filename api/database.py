"""SQLite database setup via SQLModel."""

import sqlite3

from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = "sqlite:///data/rules.db"

engine = create_engine(DATABASE_URL, echo=False)


def _migrate():
    """Add columns that don't exist yet (SQLModel create_all won't alter tables)."""
    conn = sqlite3.connect("data/rules.db")
    cursor = conn.execute("PRAGMA table_info(log_metric_rules)")
    existing = {row[1] for row in cursor.fetchall()}
    if existing and "origin" not in existing:
        conn.execute("ALTER TABLE log_metric_rules ADD COLUMN origin JSON DEFAULT '{}'")
        conn.commit()
    conn.close()


def create_db():
    SQLModel.metadata.create_all(engine)
    _migrate()


def get_session():
    with Session(engine) as session:
        yield session
