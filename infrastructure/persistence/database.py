"""
Database engine and session factory for gmail_zero_app.

Provides:
    - ``build_engine``   — creates a configured SQLAlchemy engine for a given URL
    - ``SessionFactory`` — a callable that produces scoped SQLAlchemy sessions
    - ``initialise_db``  — creates all tables (idempotent via create_all)
    - ``get_session``    — context manager for a single unit-of-work session

SQLite-specific configuration applied:
    - WAL journal mode for concurrent read performance
    - Foreign key enforcement (SQLite disables this by default)
    - Busy timeout to handle rare write contention

Usage (application code)::

    from infrastructure.persistence.database import build_engine, get_session

    engine = build_engine(settings.db_url)
    with get_session(engine) as session:
        repo = MessageRepository(session)
        messages = repo.list_inbox()

Usage (tests)::

    engine = build_engine("sqlite:///:memory:")
    initialise_db(engine)
    with get_session(engine) as session:
        ...
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session, sessionmaker

from infrastructure.persistence.models import Base

if TYPE_CHECKING:
    from collections.abc import Generator


def build_engine(db_url: str, *, echo: bool = False) -> Engine:
    """
    Create and configure a SQLAlchemy engine.

    SQLite-specific optimisations are applied via event listeners rather than
    connect_args so they work correctly across all connection pool checkouts,
    not just the first connection.

    Args:
        db_url: SQLAlchemy database URL, e.g. ``"sqlite:///data/app.db"``
                or ``"sqlite:///:memory:"`` for tests.
        echo:   If True, SQLAlchemy will log all SQL statements to stdout.
                Never enable in production.

    Returns:
        A fully configured ``Engine`` instance.
    """
    engine = sa_create_engine(
        db_url,
        echo=echo,
        # Use StaticPool for in-memory SQLite so all connections share the
        # same database.  Required for tests using "sqlite:///:memory:".
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        """
        Apply SQLite PRAGMAs on every new connection.

        These must be set per-connection, not per-engine, because SQLite
        PRAGMAs are connection-scoped.
        """
        cursor = dbapi_connection.cursor()  # type: ignore[union-attr]

        # WAL mode: readers don't block writers, writers don't block readers.
        # Significantly improves dashboard query performance during sync.
        cursor.execute("PRAGMA journal_mode=WAL")

        # Enforce foreign key constraints — SQLite disables these by default.
        # This catches referential integrity violations that would otherwise
        # silently corrupt the database.
        cursor.execute("PRAGMA foreign_keys=ON")

        # 5-second busy timeout before raising "database is locked".
        # Handles the rare case where a sync and a dashboard query collide.
        cursor.execute("PRAGMA busy_timeout=5000")

        cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """
    Create a session factory bound to the given engine.

    The returned factory produces sessions with ``autocommit=False`` and
    ``autoflush=False``.  Callers are responsible for explicit commit/rollback,
    which is handled by the ``get_session`` context manager.

    Args:
        engine: A configured SQLAlchemy engine.

    Returns:
        A ``sessionmaker`` callable.
    """
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,  # Prevent lazy-load after commit in closed session
    )


def initialise_db(engine: Engine) -> None:
    """
    Create all database tables if they do not already exist.

    Idempotent — safe to call on every application startup.  Uses SQLAlchemy's
    ``create_all`` with ``checkfirst=True`` (the default), so existing tables
    and their data are never modified.

    For schema migrations in future steps, Alembic should be used instead.
    This function covers the MVP case of initial creation only.

    Args:
        engine: A configured SQLAlchemy engine.
    """
    Base.metadata.create_all(engine, checkfirst=True)


@contextmanager
def get_session(engine: Engine) -> Generator[Session, None, None]:
    """
    Context manager providing a single unit-of-work database session.

    Commits on clean exit, rolls back on any exception, and always closes
    the session.  This is the standard pattern for all repository usage
    in the application.

    Args:
        engine: A configured SQLAlchemy engine.

    Yields:
        An open ``Session`` instance.

    Raises:
        Re-raises any exception after rolling back the transaction.

    Example::

        with get_session(engine) as session:
            repo = MessageRepository(session)
            repo.upsert(message)
            # commits automatically on context exit
    """
    factory = build_session_factory(engine)
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
