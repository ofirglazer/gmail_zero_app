"""
Flask application factory for gmail_zero_app.

Creates a fully configured Flask application with all services wired via
dependency injection.  This module is also the ``__main__`` entry point for
running the app locally.

Architecture:
    Presentation (Flask+HTMX) → Application (services) → Domain + Infrastructure

    The factory follows the Flask application-factory pattern so that the app
    object can be created fresh for each test, avoiding shared state.

Usage:
    # Demo mode (default — uses MockGmailClient, no OAuth)
    python -m presentation.app

    # Production mode (real Gmail API, requires credentials)
    GMAIL_ZERO_ENV=production python -m presentation.app

Safety:
    Settings.host_must_be_localhost ensures Flask never binds to a non-local
    interface.  This is validated at Settings construction time.
"""

from __future__ import annotations

import atexit
import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from flask import Flask, g, render_template, url_for
from sqlalchemy import Engine

from application.services.analytics_service import AnalyticsService
from application.services.label_config_service import LabelConfigService
from application.services.label_service import LabelService
from application.services.search_service import SearchService
from application.services.sync_service import SyncService
from config.settings import Settings, get_settings
from domain.exceptions import ForbiddenOperationError, SafetyViolationError
from domain.safety.guard import SafetyGuard
from infrastructure.gmail.mapper import GmailMapper
from infrastructure.persistence.database import build_engine, get_session, initialise_db
from infrastructure.persistence.repositories.label_repository import LabelRepository
from infrastructure.persistence.repositories.message_repository import MessageRepository
from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository
from infrastructure.scheduler.sync_scheduler import SyncScheduler

logging.basicConfig(level=logging.WARNING)
# logging.basicConfig(level=logging.INFO)
# Configure Werkzeug specifically
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('flask').setLevel(logging.WARNING)


if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from typing import Any

    from infrastructure.gmail.client import AbstractGmailClient

logger = logging.getLogger(__name__)


def _seed_demo_snapshots(engine: Engine) -> None:
    """
    Populate demo database with 90 days of historical snapshot data.

    Creates synthetic daily snapshots showing realistic trends:
    inbox count decreasing, archive unlabelled count decreasing, and custom
    label coverage increasing. This allows the dashboard to display meaningful
    progress graphs on first load in demo mode, with sufficient data for both
    30-day and 90-day views.

    Args:
        engine: SQLAlchemy Engine for the demo database.
    """
    from datetime import date, timedelta
    from sqlalchemy import delete
    from infrastructure.persistence.models import DailySnapshotORM
    from infrastructure.persistence.repositories.snapshot_repository import (
        SnapshotRepository,
    )
    from domain.models.daily_snapshot import DailySnapshot

    with get_session(engine) as session:
        repo = SnapshotRepository(session)

        # Only seed if we don't have 90 days of data (allow re-seed from old 30-day data)
        count = repo.count()
        if count >= 90:
            logger.info("Demo snapshots already cover 90+ days; skipping seed.")
            return

        if count > 0:
            logger.info("Clearing old snapshots to re-seed with 90 days of data.")
            session.execute(delete(DailySnapshotORM))
            session.commit()

        today = date.today()
        for i in range(90, 0, -1):
            snapshot_date = today - timedelta(days=i)
            repo.upsert(
                DailySnapshot(
                    snapshot_date=snapshot_date,
                    inbox_count=200 - (i // 3),  # Decreasing: 200 → 170
                    inbox_size_bytes=500_000_000 - (i // 3) * 2_000_000,  # 500MB → 440MB
                    archive_unlabelled_count=80 - (i // 3),  # 80 → 50
                    sent_unresolved_count=40 - (i // 6),  # 40 → 25
                    total_size_bytes=2_000_000_000 - (i // 3) * 10_000_000,  # 2GB → 1.7GB
                    custom_label_coverage_pct=10.0 + (i // 3) * 0.15,  # 10% → 25%
                )
            )

        logger.info("Seeded demo database with 90 days of historical snapshots.")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> Flask:
    """
    Flask application factory.

    Creates and configures a Flask application with all routes, services,
    and error handlers registered.  All dependencies are constructed here
    and stored in ``app.config`` for access via ``g`` in request context.

    Args:
        settings: Settings instance.  If None, calls ``get_settings()``
                  which reads from environment variables / .env file.
        engine:   Pre-built SQLAlchemy Engine.  When provided, the factory
                  skips ``build_engine(settings.db_url)`` entirely — no
                  filesystem access occurs.  Pass a ``sqlite:///:memory:``
                  engine from test fixtures to avoid creating a DB file.
                  ``initialise_db`` is still called on the provided engine
                  (idempotent via ``checkfirst=True``).

    Returns:
        A fully configured Flask application.
    """
    if settings is None:
        settings = get_settings()

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.secret_key = settings.secret_key

    # ── Database and engine ───────────────────────────────────────────────────
    # Accept a pre-built engine (e.g. in-memory SQLite for tests) so the
    # factory never touches the filesystem during test fixture setup.

    if engine is None:
        engine = build_engine(settings.db_url)
    initialise_db(engine)

    # ── Demo mode: seed historical snapshot data ──────────────────────────────
    # In demo mode, populate the database with 30 days of progress data so the
    # dashboard shows meaningful trends on first load.

    if settings.is_demo:
        _seed_demo_snapshots(engine)

    # ── Gmail client ──────────────────────────────────────────────────────────

    client: AbstractGmailClient
    if settings.is_demo:
        from infrastructure.gmail.mock_client import MockGmailClient
        client = MockGmailClient()
    else:
        from infrastructure.gmail.client import GmailClient
        from infrastructure.gmail.oauth import OAuthHandler
        oauth = OAuthHandler(
            credentials_path=settings.credentials_path,
            token_path=settings.token_path,
        )
        credentials = oauth.get_credentials()
        client = GmailClient(credentials)

    mapper = GmailMapper(user_email=getattr(client, "user_email", "user@gmail.com"))
    guard = SafetyGuard()

    # ── Ensure managed labels exist at startup ────────────────────────────────
    # Call LabelConfigService.ensure_labels_exist() before scheduler starts
    # so all ZeroApp/* labels are created in Gmail (or mock).

    logger.info("Ensuring managed labels exist in Gmail…")
    with get_session(engine) as _boot_session:
        _label_repo = LabelRepository(_boot_session)
        LabelConfigService(
            client=client,
            label_repo=_label_repo,
            labels_config_path=settings.labels_config_path,
        ).ensure_labels_exist()
    logger.info("Label bootstrap complete.")

    # ── Store dependencies on app for access in before_request ───────────────

    app.config["GMAIL_ZERO_SETTINGS"] = settings
    app.config["GMAIL_ZERO_ENGINE"] = engine
    app.config["GMAIL_ZERO_CLIENT"] = client
    app.config["GMAIL_ZERO_MAPPER"] = mapper
    app.config["GMAIL_ZERO_GUARD"] = guard

    # ── Background sync scheduler ───────────────────────────────────────────
    # Scheduled jobs run outside Flask's request context, so they must build
    # their own session/repository/service graph instead of using ``g``.

    @contextmanager
    def _sync_service_factory() -> Iterator[SyncService]:
        with get_session(engine) as session:
            msg_repo = MessageRepository(session)
            label_repo = LabelRepository(session)
            sync_repo = SyncStateRepository(session)
            snap_repo = SnapshotRepository(session)

            yield SyncService(
                client=client,
                mapper=mapper,
                msg_repo=msg_repo,
                label_repo=label_repo,
                sync_repo=sync_repo,
                snap_repo=snap_repo,
                settings=settings,
                session=session,
            )

    scheduler: SyncScheduler | None = None
    should_start_scheduler = (
        not settings.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    )
    if should_start_scheduler:
        scheduler = SyncScheduler(_sync_service_factory, settings)
        scheduler.start()

        def _shutdown_scheduler() -> None:
            scheduler.shutdown()

        atexit.register(_shutdown_scheduler)

    app.extensions["sync_scheduler"] = scheduler

    # ── Request lifecycle: build per-request repos and services ───────────────

    @app.before_request
    def _open_session() -> None:
        """
        Open a SQLAlchemy session and wire all repositories and services for
        the duration of this request.

        Everything is attached to Flask's ``g`` object so routes never
        import services directly — they receive them through ``g``.
        """
        g.settings = app.config["GMAIL_ZERO_SETTINGS"]
        g.client = app.config["GMAIL_ZERO_CLIENT"]
        _engine = app.config["GMAIL_ZERO_ENGINE"]
        _mapper = app.config["GMAIL_ZERO_MAPPER"]
        _guard = app.config["GMAIL_ZERO_GUARD"]

        # SQLAlchemy session — committed/rolled-back in teardown
        g._db_session_ctx = get_session(_engine)
        g.session = g._db_session_ctx.__enter__()

        # Repositories
        g.msg_repo = MessageRepository(g.session)
        g.label_repo = LabelRepository(g.session)
        g.sync_repo = SyncStateRepository(g.session)
        g.snap_repo = SnapshotRepository(g.session)

        # Read-only services (no session ownership — routes never commit)
        g.analytics_svc = AnalyticsService(
            msg_repo=g.msg_repo,
            sync_repo=g.sync_repo,
            snap_repo=g.snap_repo,
            label_repo=g.label_repo,
            settings=g.settings,
        )
        g.search_svc = SearchService(msg_repo=g.msg_repo)
        g.label_svc = LabelService(
            client=g.client,
            guard=_guard,
            msg_repo=g.msg_repo,
            label_repo=g.label_repo,
            session=g.session,
        )

        # SyncService (kept on g for potential manual-trigger route in later steps)
        g.sync_svc = SyncService(
            client=g.client,
            mapper=_mapper,
            msg_repo=g.msg_repo,
            label_repo=g.label_repo,
            sync_repo=g.sync_repo,
            snap_repo=g.snap_repo,
            settings=g.settings,
            session=g.session,
        )

    @app.teardown_request
    def _close_session(exc: BaseException | None) -> None:
        """Exit the session context manager, committing or rolling back."""
        ctx = g.pop("_db_session_ctx", None)
        if ctx is not None:
            # __exit__ protocol: pass (None, None, None) when no exception,
            # or (type, value, traceback) when one occurred.
            # Passing (type(None), None, None) is invalid and raises TypeError.
            if exc is None:
                ctx.__exit__(None, None, None)
            else:
                ctx.__exit__(type(exc), exc, exc.__traceback__)

    # ── Context processors ────────────────────────────────────────────────────

    @app.context_processor
    def _inject_globals() -> dict[str, Any]:
        """
        Inject template variables available in every template.

        ``is_demo``: bool — True when running with MockGmailClient.
        ``request_endpoint``: str — current Flask endpoint name, used by
            the nav to highlight the active link.
        ``labels_by_id``: dict[str, str] — mapping of Gmail label ID to
            display name, used by message-row label chips to show names
            instead of raw IDs.
        """
        from flask import request as flask_request
        labels_by_id: dict[str, str] = {
            lbl.id: lbl.name for lbl in g.label_repo.list_all()
        }
        return {
            "is_demo": g.settings.is_demo,
            "request_endpoint": flask_request.endpoint,
            "labels_by_id": labels_by_id,
        }

    @app.context_processor
    def _inject_query_helpers() -> dict[str, Any]:
        """Inject small helpers for query-string-preserving links."""
        from flask import request as flask_request

        def _query_url(endpoint: str, **updates: Any) -> str:
            params: dict[str, Any] = flask_request.args.to_dict(flat=True)
            for key, value in updates.items():
                if value is None or value == "":
                    params.pop(key, None)
                else:
                    params[key] = str(value)
            return url_for(endpoint, **params)

        def _sort_url(
            endpoint: str,
            *,
            sort_by: str,
            default_dir: str = "asc",
            page: int = 1,
            **updates: Any,
        ) -> str:
            params: dict[str, Any] = flask_request.args.to_dict(flat=True)
            current_sort = params.get("sort")
            current_dir = params.get("dir", default_dir)
            next_dir = "desc" if current_sort == sort_by and current_dir == "asc" else "asc"

            params.update({k: str(v) for k, v in updates.items() if v not in (None, "")})
            params["sort"] = sort_by
            params["dir"] = next_dir if current_sort == sort_by else default_dir
            if page is not None:
                params["page"] = str(page)
            return url_for(endpoint, **params)

        return {"query_url": _query_url, "sort_url": _sort_url}

    # ── Jinja2 filters ────────────────────────────────────────────────────────

    @app.template_filter("format_size")
    def _format_size(value: int) -> str:
        """Format a byte count as a human-readable string (1.2 MB, 3.4 GB, …)."""
        if value < 1_024:
            return f"{value} B"
        if value < 1_024 ** 2:
            return f"{value / 1_024:.1f} KB"
        if value < 1_024 ** 3:
            return f"{value / 1_024 ** 2:.1f} MB"
        return f"{value / 1_024 ** 3:.2f} GB"

    @app.template_filter("format_datetime")
    def _format_datetime(dt: datetime | None) -> str:
        """Format a UTC datetime as 'YYYY-MM-DD HH:MM' for display."""
        if dt is None:
            return "—"
        return dt.strftime("%Y-%m-%d %H:%M")

    @app.template_filter("age_label")
    def _age_label(days: int) -> str:
        """Convert an age in days to a compact human-readable label."""
        if days < 1:
            return "today"
        if days == 1:
            return "1 day"
        if days < 30:
            return f"{days} days"
        if days < 365:
            months = days // 30
            return f"{months} month{'s' if months > 1 else ''}"
        years = days // 365
        return f"{years} year{'s' if years > 1 else ''}"

    # ── Blueprints ────────────────────────────────────────────────────────────

    from presentation.routes.api import api_bp
    from presentation.routes.main import main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)

    # ── Error handlers ────────────────────────────────────────────────────────

    @app.errorhandler(SafetyViolationError)
    def _handle_safety_violation(error: SafetyViolationError) -> tuple[str, int]:
        """
        Return 400 when a label operation violates a safety rule.

        SafetyViolationError is a user/input error — the route attempted an
        operation that the SafetyGuard rejected.  Return the reason to the user.
        """
        return render_template("error.html", error=str(error), code=400), 400

    @app.errorhandler(ForbiddenOperationError)
    def _handle_forbidden_operation(error: ForbiddenOperationError) -> tuple[str, int]:
        """
        Return 500 when code attempts a forbidden Gmail API operation.

        ForbiddenOperationError is a programming error — it should never
        arise from user input.  Log at ERROR level and return a generic 500.
        """
        logger.error("ForbiddenOperationError: %s", error)
        return render_template("error.html", error="Internal server error", code=500), 500

    @app.errorhandler(404)
    def _not_found(error: Exception) -> tuple[str, int]:
        return render_template("error.html", error="Page not found", code=404), 404

    @app.errorhandler(500)
    def _server_error(error: Exception) -> tuple[str, int]:
        logger.exception("Unhandled server error: %s", error)
        return render_template("error.html", error="Internal server error", code=500), 500

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _settings = get_settings()
    _app = create_app(_settings)
    _app.run(
        host=_settings.host,
        port=_settings.port,
        debug=_settings.debug,
    )
