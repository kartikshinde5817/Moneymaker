"""Engine and session handling. SQLite by default, Postgres by URL swap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

connect_args = {}
if settings.is_sqlite:
    # The scheduler thread and request handlers share the engine.
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver glue
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL lets the scheduler write while the dashboard reads.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add any model columns an existing SQLite file predates.

    `create_all` only creates tables that don't exist yet; it never alters
    one that's already there. Without this, adding a column to a model (as
    happened for the login feature) crashes every request against a database
    created before that change, rather than migrating it forward. This is a
    plain additive ALTER TABLE, safe to run on every startup and a no-op once
    the columns exist. Postgres gets a real migration tool later; this covers
    the SQLite default this app ships with.
    """
    if not settings.is_sqlite:
        return
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateColumn

    inspector = sa_inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}')


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
