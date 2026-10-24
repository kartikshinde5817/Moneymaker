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
    _migrate()


def _add_missing_columns() -> None:
    """Add any model columns an existing table predates.

    `create_all` only creates tables that don't exist yet; it never alters
    one that's already there. This is a plain additive ALTER TABLE, safe on
    every startup and a no-op once the columns exist. New columns are either
    nullable or carry a server default, so tables that already hold rows can
    take them.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateColumn

    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}')


# --- versioned data migrations ------------------------------------------------

SCHEMA_VERSION = 2

# Tables whose rows belong to one customer. Rows from before the app had
# customers belong to the admin.
OWNED_TABLES = ("campaigns", "campaign_runs", "leads", "email_messages",
                "whatsapp_messages", "mailboxes", "suppressions")

# Unique constraints that became per-customer: (table, old name, new name, columns)
UNIQUE_SWAPS = (
    ("leads", "uq_lead_company_service", "uq_lead_owner_company_service",
     ("owner_id", "company_id", "service_category")),
    ("suppressions", "uq_suppression", "uq_suppression_owner", ("owner_id", "scope", "value")),
)


def _migrate() -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        row = conn.execute(text("SELECT value FROM settings WHERE key = 'schema_version'")).first()
        version = int(row[0]) if row else 1
    if version >= SCHEMA_VERSION:
        return

    with engine.begin() as conn:
        admin_id = ensure_admin_row(conn)
        for table in OWNED_TABLES:
            conn.execute(text(f'UPDATE "{table}" SET owner_id = :a WHERE owner_id IS NULL'),
                         {"a": admin_id})
    for table, old, new, cols in UNIQUE_SWAPS:
        _swap_unique(table, old, new, cols)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM settings WHERE key = 'schema_version'"))
        conn.execute(text("INSERT INTO settings (key, value) VALUES ('schema_version', :v)"),
                     {"v": str(SCHEMA_VERSION)})


def ensure_admin_row(conn) -> int:
    """The platform owner's account, created from ADMIN_EMAIL / ADMIN_PASSWORD."""
    import secrets as _secrets

    from sqlalchemy import text

    row = conn.execute(text(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")).first()
    if row:
        return row[0]
    from app.models import utcnow
    from app.services.auth import hash_password

    conn.execute(text(
        "INSERT INTO users (email, password_hash, role, name, status, totp_confirmed, created_at) "
        "VALUES (:e, :p, 'admin', 'Admin', 'active', :f, :t)"),
        {"e": settings.admin_email.lower(),
         "p": hash_password(settings.admin_password or _secrets.token_urlsafe(24)),
         "f": False, "t": utcnow()})
    return conn.execute(text(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")).first()[0]


def _swap_unique(table: str, old: str, new: str, cols: tuple[str, ...]) -> None:
    from sqlalchemy import inspect as sa_inspect

    names = {u["name"] for u in sa_inspect(engine).get_unique_constraints(table)}
    if new in names and old not in names:
        return
    if not settings.is_sqlite:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{old}"')
            if new not in names:
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table}" ADD CONSTRAINT "{new}" UNIQUE ({", ".join(cols)})')
        return
    _rebuild_sqlite_table(table)


def _rebuild_sqlite_table(name: str) -> None:
    """SQLite cannot drop a constraint, so rebuild the table from the model.

    Foreign keys are switched off for the copy: with them on, dropping the old
    table would cascade-delete every row that points at it.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateIndex, CreateTable

    table = Base.metadata.tables[name]
    old_cols = {c["name"] for c in sa_inspect(engine).get_columns(name)}
    cols = ", ".join(f'"{c.name}"' for c in table.columns if c.name in old_cols)
    create = str(CreateTable(table).compile(dialect=engine.dialect)).strip()
    create = create.replace(f"CREATE TABLE {name} (", f"CREATE TABLE {name}__new (", 1)
    raw = engine.raw_connection()
    try:
        c = raw.driver_connection
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN")
        c.execute(create)
        c.execute(f'INSERT INTO "{name}__new" ({cols}) SELECT {cols} FROM "{name}"')
        c.execute(f'DROP TABLE "{name}"')
        c.execute(f'ALTER TABLE "{name}__new" RENAME TO "{name}"')
        for index in table.indexes:
            c.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=engine.dialect)))
        c.execute("COMMIT")
        c.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()


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
