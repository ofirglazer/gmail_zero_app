"""
Integration tests for the sync pipeline (Step 5).

These tests exercise the full application stack against a real in-memory SQLite
database via the actual repository implementations — no mocking of the DB layer.
The Gmail API is represented by ``MockGmailClient`` (per the testing strategy in
README_HANDOFF.md).

Coverage targets from STEP5_CONTRACTS.md:
    ✓ Full sync populates DB from MockGmailClient
    ✓ Incremental sync adds new messages via client.advance_history()
    ✓ Label operation end-to-end: SafetyGuard → LabelService → MockGmailClient → DB
    ✓ Label operation blocked by SafetyGuard returns correct error
    ✓ Analytics after sync returns non-zero counts

Fixture strategy:
    ``synced_engine`` — runs a full sync once and returns the engine.  All
    read-only tests share this fixture via session_scope to avoid re-running
    the sync for each test.

    ``fresh_synced_db`` — re-syncs for tests that need to mutate state
    (label operations, incremental sync) without affecting sibling tests.

pytest markers:
    @pytest.mark.integration — all tests in this module carry this marker.
    Run with: pytest -m integration tests/integration/
"""

from __future__ import annotations

import pytest

from config.settings import Environment, Settings
from domain.exceptions import LabelOperationError, SafetyViolationError
from domain.safety.guard import SafetyGuard
from application.dto.label_operation import BulkLabelOperationRequest, LabelOperationRequest
from application.services.analytics_service import AnalyticsService
from application.services.label_service import LabelService
from application.services.search_service import SearchService
from application.services.sync_service import SyncService
from infrastructure.gmail.mapper import GmailMapper
from infrastructure.gmail.mock_client import MockGmailClient
from infrastructure.persistence.database import build_engine, get_session, initialise_db
from infrastructure.persistence.repositories.label_repository import LabelRepository
from infrastructure.persistence.repositories.message_repository import (
    MessageFilter,
    MessageRepository,
)
from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository

pytestmark = pytest.mark.integration

# ── Settings fixture ──────────────────────────────────────────────────────────

# Known label IDs from the MockGmailClient dataset
_LABEL_NEEDS_ACTION = "Label_NeedsAction001"
_LABEL_COMPLETE = "Label_Complete001"
_LABEL_NEWSLETTER = "Label_Newsletter001"


@pytest.fixture(scope="module")
def demo_settings() -> Settings:
    """Minimal Settings instance for integration tests."""
    return Settings(
        env=Environment.DEMO,
        sync_batch_size=50,
        sync_rate_limit_delay_ms=0,  # No sleeping in tests
    )


# ── Core fixtures ─────────────────────────────────────────────────────────────

def _build_synced_engine(settings: Settings):
    """Helper: build an engine, initialise the schema, run a full sync."""
    engine = build_engine("sqlite:///:memory:")
    initialise_db(engine)

    client = MockGmailClient()
    mapper = GmailMapper(user_email=client.user_email)

    with get_session(engine) as session:
        msg_repo = MessageRepository(session)
        label_repo = LabelRepository(session)
        sync_repo = SyncStateRepository(session)
        snap_repo = SnapshotRepository(session)

        svc = SyncService(
            client=client,
            mapper=mapper,
            msg_repo=msg_repo,
            label_repo=label_repo,
            sync_repo=sync_repo,
            snap_repo=snap_repo,
            settings=settings,
            session=session,
        )
        svc.run_full_sync()

    return engine, client


@pytest.fixture(scope="module")
def synced_engine(demo_settings: Settings):
    """
    Module-scoped: one full sync shared across all read-only tests.
    Do NOT use this fixture in tests that mutate message or label state.
    """
    engine, client = _build_synced_engine(demo_settings)
    return engine, client


@pytest.fixture()
def fresh_synced_db(demo_settings: Settings):
    """
    Function-scoped: fresh full sync per test.
    Use for tests that add/remove labels or advance history.
    """
    engine, client = _build_synced_engine(demo_settings)
    return engine, client, demo_settings


# ── Full sync tests ───────────────────────────────────────────────────────────

class TestFullSync:
    """Full sync pipeline correctness tests."""

    def test_full_sync_populates_inbox(self, synced_engine):
        """After a full sync, count_inbox() must equal the mock inbox count."""
        engine, client = synced_engine
        with get_session(engine) as session:
            repo = MessageRepository(session)
            db_count = repo.count_inbox()

        assert db_count > 0, "Expected at least one inbox message after full sync"
        assert db_count == client.inbox_count(), (
            f"DB inbox count ({db_count}) does not match mock client inbox count "
            f"({client.inbox_count()})"
        )

    def test_full_sync_total_message_count(self, synced_engine):
        """Every message in the mock dataset should be upserted to the DB."""
        engine, client = synced_engine
        with get_session(engine) as session:
            repo = MessageRepository(session)
            # count_search with no filters = total count
            total = repo.count_search(MessageFilter())

        assert total == client.message_count(), (
            f"DB total ({total}) does not match mock dataset size ({client.message_count()})"
        )

    def test_full_sync_writes_sync_state(self, synced_engine):
        """A SyncState(FULL) record must exist after a full sync."""
        from domain.models.sync_state import SyncType

        engine, _ = synced_engine
        with get_session(engine) as session:
            sync_repo = SyncStateRepository(session)
            state = sync_repo.latest()

        assert state is not None, "Expected a SyncState after full sync"
        assert state.sync_type == SyncType.FULL
        assert state.messages_synced > 0
        assert state.history_id  # non-empty string

    def test_full_sync_writes_daily_snapshot(self, synced_engine):
        """A DailySnapshot must be written at the end of a full sync."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            snap_repo = SnapshotRepository(session)
            count = snap_repo.count()

        assert count >= 1, "Expected at least one DailySnapshot after full sync"

    def test_full_sync_writes_labels(self, synced_engine):
        """The label registry should be populated with system + user labels."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            label_repo = LabelRepository(session)
            labels = label_repo.list_all()

        label_names = {lbl.name for lbl in labels}
        assert "INBOX" in label_names, "System label INBOX must be in registry"
        assert "ZeroApp/Needs-Action" in label_names, (
            "User label ZeroApp/Needs-Action must be in registry"
        )

    def test_full_sync_archive_unlabelled_count_positive(self, synced_engine):
        """There should be archived messages with no custom label (archive hygiene targets)."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            repo = MessageRepository(session)
            count = repo.count_archive_unlabelled()

        assert count > 0, "Expected unlabelled archived messages in the mock dataset"

    def test_full_sync_has_size_data(self, synced_engine):
        """Total size bytes must be non-zero after syncing the mock dataset."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            repo = MessageRepository(session)
            total = repo.total_size_bytes()

        assert total > 0, "Expected non-zero total size after full sync"


# ── Incremental sync tests ────────────────────────────────────────────────────

class TestIncrementalSync:
    """Incremental sync pipeline correctness tests."""

    def test_incremental_sync_adds_new_messages(
        self, fresh_synced_db: tuple, demo_settings: Settings
    ):
        """advance_history() then incremental sync should add the new messages."""
        engine, client, settings = fresh_synced_db

        # Record DB state after full sync
        with get_session(engine) as session:
            count_before = MessageRepository(session).count_search(MessageFilter())

        # Simulate 5 new inbox messages arriving
        client.advance_history(new_message_count=5)

        # Run incremental sync
        mapper = GmailMapper(user_email=client.user_email)
        with get_session(engine) as session:
            msg_repo = MessageRepository(session)
            label_repo = LabelRepository(session)
            sync_repo = SyncStateRepository(session)
            snap_repo = SnapshotRepository(session)

            svc = SyncService(
                client=client,
                mapper=mapper,
                msg_repo=msg_repo,
                label_repo=label_repo,
                sync_repo=sync_repo,
                snap_repo=snap_repo,
                settings=settings,
                session=session,
            )
            state = svc.run_incremental_sync()

        with get_session(engine) as session:
            count_after = MessageRepository(session).count_search(MessageFilter())

        assert count_after == count_before + 5, (
            f"Expected {count_before + 5} messages after incremental sync; "
            f"got {count_after}"
        )
        assert state.messages_synced == 5

    def test_incremental_sync_without_prior_sync_falls_back_to_full(
        self, demo_settings: Settings
    ):
        """When no SyncState exists, incremental sync must fall back to full sync."""
        from domain.models.sync_state import SyncType

        # Fresh DB — no sync has run yet
        engine = build_engine("sqlite:///:memory:")
        initialise_db(engine)
        client = MockGmailClient()
        mapper = GmailMapper(user_email=client.user_email)

        with get_session(engine) as session:
            msg_repo = MessageRepository(session)
            label_repo = LabelRepository(session)
            sync_repo = SyncStateRepository(session)
            snap_repo = SnapshotRepository(session)

            svc = SyncService(
                client=client,
                mapper=mapper,
                msg_repo=msg_repo,
                label_repo=label_repo,
                sync_repo=sync_repo,
                snap_repo=snap_repo,
                settings=demo_settings,
                session=session,
            )
            # Should silently fall back to full sync
            state = svc.run_incremental_sync()

        assert state.sync_type == SyncType.FULL
        assert state.messages_synced == client.message_count()

    def test_incremental_sync_no_changes_produces_zero_messages_synced(
        self, fresh_synced_db: tuple
    ):
        """An incremental sync with no changes should report 0 messages synced."""
        engine, client, settings = fresh_synced_db

        # No advance_history() call — nothing has changed
        mapper = GmailMapper(user_email=client.user_email)
        with get_session(engine) as session:
            msg_repo = MessageRepository(session)
            label_repo = LabelRepository(session)
            sync_repo = SyncStateRepository(session)
            snap_repo = SnapshotRepository(session)

            svc = SyncService(
                client=client,
                mapper=mapper,
                msg_repo=msg_repo,
                label_repo=label_repo,
                sync_repo=sync_repo,
                snap_repo=snap_repo,
                settings=settings,
                session=session,
            )
            state = svc.run_incremental_sync()

        assert state.messages_synced == 0


# ── Label service tests ───────────────────────────────────────────────────────

class TestLabelService:
    """End-to-end label operation tests."""

    def test_add_label_updates_db_and_mock(self, fresh_synced_db: tuple):
        """
        apply_label_operation should:
            1. Call the mock client (mutate its state).
            2. Update the MessageRepository.
            3. Write to the LabelOperationLog.
        """
        engine, client, _ = fresh_synced_db

        # inbox001 starts without _LABEL_COMPLETE
        assert _LABEL_COMPLETE not in client.get_label_ids_for_message("inbox001")

        guard = SafetyGuard()
        request = LabelOperationRequest(
            message_id="inbox001",
            add_label_ids=frozenset({_LABEL_COMPLETE}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            updated = label_svc.apply_label_operation(request)

        # Mock state should reflect the change
        assert _LABEL_COMPLETE in client.get_label_ids_for_message("inbox001")

        # Returned entity should have the new label
        assert _LABEL_COMPLETE in updated.label_ids

        # DB should also reflect the change
        with get_session(engine) as session:
            db_message = MessageRepository(session).get_by_id("inbox001")
        assert db_message is not None
        assert _LABEL_COMPLETE in db_message.label_ids

    def test_remove_label_updates_db_and_mock(self, fresh_synced_db: tuple):
        """apply_label_operation should remove labels from both mock and DB."""
        engine, client, _ = fresh_synced_db

        # inbox002 already has _LABEL_COMPLETE
        assert _LABEL_COMPLETE in client.get_label_ids_for_message("inbox002")

        guard = SafetyGuard()
        request = LabelOperationRequest(
            message_id="inbox002",
            remove_label_ids=frozenset({_LABEL_COMPLETE}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            updated = label_svc.apply_label_operation(request)

        assert _LABEL_COMPLETE not in client.get_label_ids_for_message("inbox002")
        assert _LABEL_COMPLETE not in updated.label_ids

    def test_label_operation_writes_audit_log(self, fresh_synced_db: tuple):
        """Every label operation must produce an audit log entry."""
        from sqlalchemy import select
        from infrastructure.persistence.models import LabelOperationLogORM

        engine, client, _ = fresh_synced_db

        guard = SafetyGuard()
        request = LabelOperationRequest(
            message_id="inbox001",
            add_label_ids=frozenset({_LABEL_NEWSLETTER}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            label_svc.apply_label_operation(request)

        with get_session(engine) as session:
            log_entries = session.execute(
                select(LabelOperationLogORM).where(
                    LabelOperationLogORM.message_id == "inbox001",
                    LabelOperationLogORM.label_id == _LABEL_NEWSLETTER,
                    LabelOperationLogORM.success.is_(True),
                )
            ).scalars().all()

        assert len(log_entries) >= 1, "Expected at least one audit log entry"

    def test_label_op_blocked_by_safety_guard_inbox_removal(
        self, fresh_synced_db: tuple
    ):
        """
        Attempting to remove INBOX must raise SafetyViolationError.
        The error must propagate — NOT be caught by LabelService.
        """
        engine, client, _ = fresh_synced_db

        guard = SafetyGuard()
        # Removing INBOX archives the message — this is the #1 safety violation
        request = LabelOperationRequest(
            message_id="inbox001",
            remove_label_ids=frozenset({"INBOX"}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            with pytest.raises(SafetyViolationError) as exc_info:
                label_svc.apply_label_operation(request)

        assert "INBOX" in str(exc_info.value)
        # Mock client must NOT have been called — the guard fired before the API call
        assert "INBOX" in client.get_label_ids_for_message("inbox001"), (
            "INBOX label must still be present — operation should have been blocked before API call"
        )

    def test_label_op_blocked_by_safety_guard_protected_add(
        self, fresh_synced_db: tuple
    ):
        """Adding TRASH must raise SafetyViolationError."""
        engine, client, _ = fresh_synced_db

        guard = SafetyGuard()
        request = LabelOperationRequest(
            message_id="inbox001",
            add_label_ids=frozenset({"TRASH"}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            with pytest.raises(SafetyViolationError):
                label_svc.apply_label_operation(request)

    def test_label_op_fails_for_unknown_message(self, fresh_synced_db: tuple):
        """Attempting to label a message not in the DB should raise LabelOperationError."""
        engine, client, _ = fresh_synced_db

        guard = SafetyGuard()
        request = LabelOperationRequest(
            message_id="NONEXISTENT_ID",
            add_label_ids=frozenset({_LABEL_COMPLETE}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            with pytest.raises(LabelOperationError):
                label_svc.apply_label_operation(request)

    def test_bulk_label_operation_applies_to_all_messages(
        self, fresh_synced_db: tuple
    ):
        """Bulk operation should update every message in the request."""
        engine, client, _ = fresh_synced_db

        target_ids = ("inbox001", "inbox003", "inbox005")
        guard = SafetyGuard()
        bulk_request = BulkLabelOperationRequest(
            message_ids=target_ids,
            add_label_ids=frozenset({_LABEL_NEEDS_ACTION}),
        )

        with get_session(engine) as session:
            label_svc = LabelService(
                client=client,
                guard=guard,
                msg_repo=MessageRepository(session),
                label_repo=LabelRepository(session),
                session=session,
            )
            results = label_svc.apply_bulk_label_operation(bulk_request)

        assert len(results) == 3
        for msg in results:
            assert _LABEL_NEEDS_ACTION in msg.label_ids, (
                f"Expected {_LABEL_NEEDS_ACTION} in {msg.id} label_ids"
            )


# ── Analytics service tests ───────────────────────────────────────────────────

class TestAnalyticsService:
    """Analytics service correctness tests after a full sync."""

    def test_dashboard_summary_non_zero_counts(
        self, synced_engine, demo_settings: Settings
    ):
        """All major counts must be positive after syncing the mock dataset."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = AnalyticsService(
                msg_repo=MessageRepository(session),
                sync_repo=SyncStateRepository(session),
                snap_repo=SnapshotRepository(session),
                label_repo=LabelRepository(session),
                settings=demo_settings,
            )
            summary = svc.dashboard_summary()

        assert summary.inbox_count > 0, "inbox_count must be > 0"
        assert summary.archive_unlabelled_count > 0, "archive_unlabelled_count must be > 0"
        assert summary.total_size_bytes > 0, "total_size_bytes must be > 0"
        assert summary.has_ever_synced, "has_ever_synced must be True after a sync"

    def test_dashboard_summary_zero_goals_reflect_correctly(
        self, synced_engine, demo_settings: Settings
    ):
        """Verify that zero-goal properties are derived correctly from counts."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = AnalyticsService(
                msg_repo=MessageRepository(session),
                sync_repo=SyncStateRepository(session),
                snap_repo=SnapshotRepository(session),
                label_repo=LabelRepository(session),
                settings=demo_settings,
            )
            summary = svc.dashboard_summary()

        # inbox_zero_reached is derived from inbox_count
        assert summary.inbox_zero_reached == (summary.inbox_count == 0)
        assert summary.archive_zero_reached == (summary.archive_unlabelled_count == 0)
        assert summary.sent_zero_reached == (summary.sent_unresolved_count == 0)

    def test_top_senders_by_count_returns_results(
        self, synced_engine, demo_settings: Settings
    ):
        """top_senders_by_count must return at least one sender."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = AnalyticsService(
                msg_repo=MessageRepository(session),
                sync_repo=SyncStateRepository(session),
                snap_repo=SnapshotRepository(session),
                label_repo=LabelRepository(session),
                settings=demo_settings,
            )
            senders = svc.top_senders_by_count(limit=5)

        assert len(senders) > 0
        # Sorted by count descending
        counts = [s.message_count for s in senders]
        assert counts == sorted(counts, reverse=True)

    def test_top_senders_by_size_returns_results(
        self, synced_engine, demo_settings: Settings
    ):
        """top_senders_by_size must return at least one sender."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = AnalyticsService(
                msg_repo=MessageRepository(session),
                sync_repo=SyncStateRepository(session),
                snap_repo=SnapshotRepository(session),
                label_repo=LabelRepository(session),
                settings=demo_settings,
            )
            senders = svc.top_senders_by_size(limit=5)

        assert len(senders) > 0
        sizes = [s.total_size_bytes for s in senders]
        assert sizes == sorted(sizes, reverse=True)

    def test_progress_snapshots_returns_at_least_one(
        self, synced_engine, demo_settings: Settings
    ):
        """After a full sync, at least one DailySnapshot must exist."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = AnalyticsService(
                msg_repo=MessageRepository(session),
                sync_repo=SyncStateRepository(session),
                snap_repo=SnapshotRepository(session),
                label_repo=LabelRepository(session),
                settings=demo_settings,
            )
            snapshots = svc.progress_snapshots(days=30)

        assert len(snapshots) >= 1


# ── Search service tests ──────────────────────────────────────────────────────

class TestSearchService:
    """SearchService correctness tests."""

    def test_search_no_filters_returns_all(self, synced_engine):
        """An empty MessageFilter should return all messages (up to the default limit)."""
        engine, client = synced_engine
        with get_session(engine) as session:
            svc = SearchService(msg_repo=MessageRepository(session))
            results, total = svc.search(MessageFilter())

        # total should equal the full dataset size
        assert total == client.message_count()
        # results are paginated by the default limit (200)
        assert len(results) <= 200

    def test_search_by_inbox_flag(self, synced_engine):
        """Filtering by is_inbox=True must return only inbox messages."""
        engine, client = synced_engine
        with get_session(engine) as session:
            svc = SearchService(msg_repo=MessageRepository(session))
            results, total = svc.search(MessageFilter(is_inbox=True))

        assert total == client.inbox_count()
        for msg in results:
            assert msg.is_inbox, f"Message {msg.id} is not in inbox"

    def test_search_by_sender_domain(self, synced_engine):
        """Filtering by sender_domain must restrict results to that domain."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = SearchService(msg_repo=MessageRepository(session))
            results, total = svc.search(MessageFilter(sender_domain="github.com"))

        assert total > 0, "Expected messages from github.com in the mock dataset"
        for msg in results:
            assert "github" in msg.sender_domain, (
                f"Message {msg.id} sender_domain {msg.sender_domain!r} "
                "does not contain 'github'"
            )

    def test_search_total_count_ignores_pagination(self, synced_engine):
        """total_count must reflect all matching rows regardless of limit/offset."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = SearchService(msg_repo=MessageRepository(session))
            _, total_p1 = svc.search(MessageFilter(is_inbox=True, limit=5, offset=0))
            _, total_p2 = svc.search(MessageFilter(is_inbox=True, limit=5, offset=5))

        # Both pages should report the same total_count
        assert total_p1 == total_p2, (
            "total_count should be the same regardless of pagination offset"
        )

    def test_search_returns_empty_for_impossible_filter(self, synced_engine):
        """A filter matching no messages should return ([], 0)."""
        engine, _ = synced_engine
        with get_session(engine) as session:
            svc = SearchService(msg_repo=MessageRepository(session))
            results, total = svc.search(
                MessageFilter(sender_domain="this-domain-does-not-exist-xyz.invalid")
            )

        assert results == []
        assert total == 0
