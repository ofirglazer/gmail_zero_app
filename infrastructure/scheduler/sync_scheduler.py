"""
SyncScheduler — APScheduler wrapper for background Gmail sync.

Wraps a SyncService in a background scheduler that fires a daily full sync
at a configured time.  Incremental syncs can optionally be scheduled at
a higher frequency to keep the local database near-real-time.

This module is optional — the application works correctly without a running
scheduler.  Manual sync triggers (e.g. via a route or CLI) bypass this
entirely.

Usage::

    factory = SyncServiceFactory(engine, client, mapper, settings)
    scheduler = SyncScheduler(factory, settings)
    scheduler.start()          # non-blocking; runs in background thread
    ...
    scheduler.shutdown()       # graceful stop; flushes pending jobs

Design:
    APScheduler is listed as an optional dependency.  ImportError is deferred
    to ``start()`` so the app can boot without it when scheduling is disabled.

    SyncServiceFactory is a callable that constructs a fresh SyncService
    (with its own session) for each scheduler invocation.  This avoids sharing
    a single SQLAlchemy session across threads, which is not safe.

    The scheduler runs in a ``BackgroundScheduler`` (daemon thread) so it
    does not block the Flask dev server from shutting down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from domain.exceptions import IncrementalSyncError

if TYPE_CHECKING:
    from config.settings import Settings
    from application.services.sync_service import SyncService

logger = logging.getLogger(__name__)

# Callable that produces a fully-wired SyncService with a fresh session.
# The factory is responsible for creating the session and all repos.
SyncServiceFactory = Callable[[], "SyncService"]


class SyncScheduler:
    """
    Background scheduler that triggers Gmail sync on a recurring schedule.

    Runs an incremental sync at ``incremental_interval_minutes`` frequency.
    If the incremental sync raises ``IncrementalSyncError`` (stale historyId),
    it automatically falls back to a full sync.

    Args:
        factory:  Callable that returns a fresh SyncService.  Called at each
                  scheduled invocation (not at scheduler construction time).
        settings: Application settings for schedule configuration.
        incremental_interval_minutes: How often to run an incremental sync
                                       (default: 30 minutes).
        full_sync_hour: Hour (0-23 UTC) at which to run the daily full sync
                        (default: 3 AM UTC to avoid peak quota usage).
    """

    def __init__(
        self,
        factory: SyncServiceFactory,
        settings: Settings,
        incremental_interval_minutes: int = 30,
        full_sync_hour: int = 3,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._incremental_interval_minutes = incremental_interval_minutes
        self._full_sync_hour = full_sync_hour
        self._scheduler: object | None = None  # APScheduler instance, type-erased

    def start(self) -> None:
        """
        Start the background scheduler.

        Registers:
            - A daily full sync at ``full_sync_hour:00`` UTC.
            - An incremental sync every ``incremental_interval_minutes`` minutes.

        Raises:
            ImportError: If ``apscheduler`` is not installed.
            RuntimeError: If the scheduler is already running.
        """
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError as exc:
            raise ImportError(
                "APScheduler is required for background sync scheduling. "
                "Install it with: pip install apscheduler"
            ) from exc

        if self._scheduler is not None:
            raise RuntimeError("SyncScheduler is already running.")

        scheduler = BackgroundScheduler(timezone="UTC")

        # Daily full sync at the configured hour
        scheduler.add_job(
            func=self._run_full_sync,
            trigger=CronTrigger(hour=self._full_sync_hour, minute=0),
            id="full_sync_daily",
            name="Daily full Gmail sync",
            replace_existing=True,
            misfire_grace_time=3600,  # 1 hour: tolerate system sleep / restart
        )

        # Frequent incremental sync
        scheduler.add_job(
            func=self._run_incremental_sync_with_fallback,
            trigger=IntervalTrigger(minutes=self._incremental_interval_minutes),
            id="incremental_sync",
            name=f"Incremental Gmail sync every {self._incremental_interval_minutes}m",
            replace_existing=True,
            misfire_grace_time=300,  # 5 minutes
        )

        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "SyncScheduler started: full sync at %02d:00 UTC, "
            "incremental every %d minutes.",
            self._full_sync_hour,
            self._incremental_interval_minutes,
        )

    def shutdown(self, wait: bool = True) -> None:
        """
        Gracefully stop the scheduler.

        Args:
            wait: If True (default), wait for running jobs to finish before
                  returning.  Set to False for fast shutdown in tests.
        """
        if self._scheduler is not None:
            # APScheduler's shutdown() method — type-erased but present
            getattr(self._scheduler, "shutdown")(wait=wait)
            self._scheduler = None
            logger.info("SyncScheduler stopped.")

    # ── Job implementations ───────────────────────────────────────────────────

    def _run_full_sync(self) -> None:
        """
        Job handler for scheduled full syncs.

        Creates a fresh SyncService (and session) via the factory, runs the
        full sync, then disposes the session.  Exceptions are logged but not
        re-raised so APScheduler does not remove the job on failure.
        """
        logger.info("Scheduled full sync starting.")
        try:
            service = self._factory()
            state = service.run_full_sync()
            logger.info(
                "Scheduled full sync complete: %d messages synced, historyId=%s.",
                state.messages_synced,
                state.history_id,
            )
        except Exception:
            logger.exception("Scheduled full sync failed.")

    def _run_incremental_sync_with_fallback(self) -> None:
        """
        Job handler for scheduled incremental syncs.

        Attempts an incremental sync.  On ``IncrementalSyncError`` (stale
        historyId), falls back to a full sync automatically and logs a warning.
        """
        logger.info("Scheduled incremental sync starting.")
        try:
            service = self._factory()
            try:
                state = service.run_incremental_sync()
                logger.info(
                    "Incremental sync complete: %d messages synced, historyId=%s.",
                    state.messages_synced,
                    state.history_id,
                )
            except IncrementalSyncError as exc:
                logger.warning(
                    "Incremental sync failed (stale historyId %r); falling back to full sync.",
                    exc.history_id,
                )
                # Re-create the service to get a fresh session after the failed transaction
                service = self._factory()
                state = service.run_full_sync()
                logger.info(
                    "Fallback full sync complete: %d messages synced.",
                    state.messages_synced,
                )
        except Exception:
            logger.exception("Scheduled incremental sync (with fallback) failed.")
