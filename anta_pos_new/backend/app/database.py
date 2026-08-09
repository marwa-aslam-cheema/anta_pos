"""SQLAlchemy engine, session, and Base."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import get_settings

settings = get_settings()

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, connection_record):
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables and seed defaults."""
    from . import models  # noqa: F401
    from . import models_accounting  # noqa: F401
    from .seed import seed_if_empty

    # Ensure parent dir exists for sqlite
    if settings.database_url.startswith("sqlite:///"):
        from pathlib import Path

        db_path = Path(settings.database_url.replace("sqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)
    _auto_migrate()
    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()


def _auto_migrate() -> None:
    """Add newly introduced columns to existing tables (safe, additive). Works for SQLite and Postgres."""
    from sqlalchemy import inspect, text

    wanted = {
        "products": {
            "color": "VARCHAR(64) DEFAULT ''",
            "department": "VARCHAR(64) DEFAULT ''",
            "season": "VARCHAR(64) DEFAULT ''",
            "gender": "VARCHAR(32) DEFAULT ''",
        },
        "promotions": {
            "start_time": "VARCHAR(8) DEFAULT ''",
            "end_time": "VARCHAR(8) DEFAULT ''",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            try:
                existing = {c["name"] for c in inspector.get_columns(table)}
            except Exception:
                continue
            for col, ddl in cols.items():
                if col not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    except Exception:
                        pass
