"""SQLAlchemy engine, session, and Base."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import get_settings

settings = get_settings()

connect_args = {}
pool_kwargs = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    # Free/small Postgres plans still allow well over this many connections.
    # Default SQLAlchemy pool (5 + 10 overflow = 15) is too small once the
    # HO dashboard fires ~16 API calls at once — bump it so a normal page
    # load doesn't exhaust the pool and start timing out other requests
    # (including login, which needs its own connection too).
    pool_kwargs = {
        "pool_size": 15,
        "max_overflow": 25,
        "pool_timeout": 30,
        "pool_recycle": 1800,  # avoid stale connections dropped by the DB host
    }

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    **pool_kwargs,
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
            "original_price": "FLOAT DEFAULT 0",
        },
        "promotions": {
            "start_time": "VARCHAR(8) DEFAULT ''",
            "end_time": "VARCHAR(8) DEFAULT ''",
        },
        "store_grn": {
            "received_by": "VARCHAR(128) DEFAULT ''",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            try:
                existing = {c["name"] for c in inspector.get_columns(table)}
            except Exception:
                continue
            newly_added = set()
            for col, ddl in cols.items():
                if col not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                        newly_added.add(col)
                    except Exception:
                        pass
            # One-time backfill: existing products won't have an Original
            # Price yet (it defaults to 0) — seed it from their current
            # retail price so the column isn't blank for everything
            # already in the system. New rows created after this get
            # their own original_price at creation time and this never
            # touches them again.
            if table == "products" and "original_price" in newly_added:
                try:
                    conn.execute(text("UPDATE products SET original_price = retail WHERE original_price = 0 OR original_price IS NULL"))
                except Exception:
                    pass
