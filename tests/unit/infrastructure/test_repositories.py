"""
Repository tests for gmail_zero_app — Step 3.

All tests run against an in-memory SQLite database (sqlite:///:memory:).
No mocking of the database layer — tests exercise real SQL through
SQLAlchemy to catch schema issues, constraint violations, and query bugs.

Test structure:
    TestDatabaseInitialisation  — tables created, idempotent create_all
    TestMessageRepository       — CRUD, upsert, analytics, search, label update
    TestLabelRepository         — CRUD, junction table sync, audit log
    TestSyncStateRepository     — save, latest, thread upserts
    TestSnapshotRepository      — upsert, list_recent, latest

Fixtures are defined in this file (not conftest) because they require a
live engine and are specific to infrastructure tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Engine, inspect, text

from domain.models.daily_snapshot import DailySnapshot
from domain.models.label import Label, LabelType
from domain.models.message import make_message
from domain.models.sync_state import SyncState, SyncType
from domain.models.thread import Thread
from infrastructure.persistence.database import build_engine, get_session, initialise_db
from infrastructure.persistence.models import get_all_orm_models
from infrastructure.persistence.repositories import (
    LabelRepository,
    MessageFilter,
    MessageRepository,
    SnapshotRepository,
    SyncStateRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def engine() -> Engine:
    """Fresh in-memory SQLite engine per test function."""
    eng = build_engine("sqlite:///:memory:")
    initialise_db(eng)
    return eng


@pytest.fixture
def session(engine: Engine):
    """Open session that rolls back after each test (isolation without teardown)."""
    with get_session(engine) as s:
        yield s


@pytest.fixture
def msg_repo(session: Session) -> MessageRepository:
    return MessageRepository(session)


@pytest.fixture
def label_repo(session: Session) -> LabelRepository:
    return LabelRepository(session)


@pytest.fixture
def sync_repo(session: Session) -> SyncStateRepository:
    return SyncStateRepository(session)


@pytest.fixture
def snap_repo(session: Session) -> SnapshotRepository:
    return SnapshotRepository(session)


# ── Domain entity helpers ─────────────────────────────────────────────────────


def _make_label(
    label_id: str = "Label_001",
    name: str = "Test Label",
    label_type: LabelType = LabelType.USER,
) -> Label:
    return Label(
        id=label_id,
        name=name,
        label_type=label_type,
        message_list_visibility=None,
        label_list_visibility=None,
        synced_at=datetime.now(tz=UTC),
    )


def _make_thread(thread_id: str = "thread_001") -> Thread:
    return Thread(
        id=thread_id,
        subject="Test thread",
        message_count=1,
        snippet="snippet",
        last_message_at=datetime.now(tz=UTC) - timedelta(days=5),
        is_inbox=True,
        has_custom_label=False,
        last_synced_at=datetime.now(tz=UTC),
    )


def _make_inbox_message(
    msg_id: str = "msg_001",
    thread_id: str = "thread_001",
    sender: str = "sender@example.com",
    days_old: int = 3,
    size: int = 10_000,
) -> make_message.__annotations__:  # type: ignore[return]
    return make_message(
        id=msg_id,
        thread_id=thread_id,
        history_id="hist_001",
        internal_date=datetime.now(tz=UTC) - timedelta(days=days_old),
        sender=sender,
        sender_domain=sender.split("@")[-1],
        subject="Test subject",
        snippet="Test snippet",
        size_estimate=size,
        label_ids=frozenset({"INBOX", "UNREAD"}),
    )


def _make_archived_message(
    msg_id: str = "msg_arc_001",
    thread_id: str = "thread_001",
    sender: str = "news@example.com",
    has_label: bool = False,
) -> make_message.__annotations__:  # type: ignore[return]
    labels: frozenset[str] = (
        frozenset({"Label_custom"}) if has_label else frozenset()
    )
    return make_message(
        id=msg_id,
        thread_id=thread_id,
        history_id="hist_002",
        internal_date=datetime.now(tz=UTC) - timedelta(days=30),
        sender=sender,
        sender_domain=sender.split("@")[-1],
        subject="Newsletter",
        size_estimate=500_000,
        label_ids=labels,
    )


def _make_sent_message(
    msg_id: str = "msg_sent_001",
    thread_id: str = "thread_001",
) -> make_message.__annotations__:  # type: ignore[return]
    return make_message(
        id=msg_id,
        thread_id=thread_id,
        history_id="hist_003",
        internal_date=datetime.now(tz=UTC) - timedelta(days=7),
        sender="me@gmail.com",
        sender_domain="gmail.com",
        subject="Sent message",
        size_estimate=5_000,
        label_ids=frozenset({"SENT"}),
    )


def _insert_thread_and_message(session: Session, msg_id: str = "msg_001",
                               thread_id: str = "thread_001") -> None:
    """Helper: insert a thread row (needed for FK) then a message."""
    from datetime import UTC, datetime

    from infrastructure.persistence.models import ThreadORM
    thread = ThreadORM(
        id=thread_id,
        subject="Subject",
        message_count=1,
        snippet="snip",
        last_message_at=datetime.now(tz=UTC),
        is_inbox=True,
        has_custom_label=False,
        last_synced_at=datetime.now(tz=UTC),
    )
    session.add(thread)
    session.flush()


# ── DB initialisation tests ───────────────────────────────────────────────────


@pytest.mark.unit
class TestDatabaseInitialisation:
    """All expected tables are created; create_all is idempotent."""

    def test_all_tables_created(self, engine: Engine) -> None:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        expected = {
            "messages", "threads", "labels", "message_labels",
            "sync_state", "daily_snapshots", "label_operations_log",
        }
        assert expected <= set(tables), (
            f"Missing tables: {expected - set(tables)}"
        )

    def test_create_all_is_idempotent(self, engine: Engine) -> None:
        """Calling initialise_db twice must not raise or corrupt existing tables."""
        initialise_db(engine)  # Second call
        inspector = inspect(engine)
        assert "messages" in inspector.get_table_names()

    def test_all_orm_models_listed(self) -> None:
        """get_all_orm_models() must include all seven ORM classes."""
        models = get_all_orm_models()
        assert len(models) == 7

    def test_foreign_keys_enabled(self, engine: Engine) -> None:
        """SQLite FK enforcement must be on (PRAGMA foreign_keys=ON)."""
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert result == 1

    def test_wal_mode_enabled(self, tmp_path: pytest.fixture) -> None:  # type: ignore[valid-type]
        """SQLite WAL journal mode must be configured for file-based databases."""
        db_path = tmp_path / "test_wal.db"
        file_engine = build_engine(f"sqlite:///{db_path}")
        initialise_db(file_engine)
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert result == "wal"

    def test_messages_table_columns(self, engine: Engine) -> None:
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("messages")}
        required = {
            "id", "thread_id", "history_id", "internal_date",
            "sender", "sender_domain", "size_estimate",
            "is_inbox", "is_archived", "is_sent", "is_unread",
            "has_custom_label", "raw_label_ids",
        }
        assert required <= columns

    def test_message_labels_composite_pk(self, engine: Engine) -> None:
        inspector = inspect(engine)
        pk = inspector.get_pk_constraint("message_labels")
        assert "message_id" in pk["constrained_columns"]
        assert "label_id" in pk["constrained_columns"]


# ── MessageRepository tests ───────────────────────────────────────────────────


@pytest.mark.unit
class TestMessageRepositoryUpsert:
    """Insert, update, and idempotency of message upserts."""

    def test_upsert_new_message(self, msg_repo: MessageRepository,
                                session: Session) -> None:
        _insert_thread_and_message(session)
        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()
        assert msg_repo.exists(msg.id)

    def test_upsert_is_idempotent(self, msg_repo: MessageRepository,
                                  session: Session) -> None:
        _insert_thread_and_message(session)
        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        msg_repo.upsert(msg)  # second call must not raise
        session.flush()
        assert msg_repo.count_inbox() == 1

    def test_upsert_updates_fields(self, msg_repo: MessageRepository,
                                   session: Session) -> None:
        _insert_thread_and_message(session)
        msg = _make_inbox_message(size=10_000)
        msg_repo.upsert(msg)
        session.flush()

        # Re-upsert with updated size (simulating a sync refresh)
        updated = make_message(
            id=msg.id,
            thread_id=msg.thread_id,
            history_id="hist_updated",
            internal_date=msg.internal_date,
            sender=msg.sender,
            sender_domain=msg.sender_domain,
            size_estimate=99_999,
            label_ids=msg.label_ids,
        )
        msg_repo.upsert(updated)
        session.flush()

        fetched = msg_repo.get_by_id(msg.id)
        assert fetched is not None
        assert fetched.size_estimate == 99_999

    def test_upsert_preserves_first_seen_at(self, msg_repo: MessageRepository,
                                             session: Session) -> None:
        _insert_thread_and_message(session)
        original_first_seen = datetime.now(tz=UTC) - timedelta(days=10)
        msg = make_message(
            id="msg_fs",
            thread_id="thread_001",
            history_id="h",
            internal_date=datetime.now(tz=UTC),
            sender="a@b.com",
            sender_domain="b.com",
            first_seen_at=original_first_seen,
        )
        msg_repo.upsert(msg)
        session.flush()

        # Upsert again — first_seen_at must not change
        msg_repo.upsert(msg)
        session.flush()

        fetched = msg_repo.get_by_id("msg_fs")
        assert fetched is not None
        assert abs((fetched.first_seen_at - original_first_seen).total_seconds()) < 1

    def test_upsert_many(self, msg_repo: MessageRepository,
                         session: Session) -> None:
        _insert_thread_and_message(session)
        msgs = [_make_inbox_message(msg_id=f"msg_{i:03d}") for i in range(5)]
        msg_repo.upsert_many(msgs)
        session.flush()
        assert msg_repo.count_inbox() == 5

    def test_get_by_id_returns_none_for_unknown(
        self, msg_repo: MessageRepository
    ) -> None:
        assert msg_repo.get_by_id("nonexistent") is None

    def test_exists_false_for_unknown(self, msg_repo: MessageRepository) -> None:
        assert msg_repo.exists("nonexistent") is False


@pytest.mark.unit
class TestMessageRepositoryInboxQueries:
    """Inbox zero workflow queries."""

    def _setup(self, msg_repo: MessageRepository, session: Session,
               count: int = 3) -> None:
        _insert_thread_and_message(session)
        msgs = [
            _make_inbox_message(
                msg_id=f"msg_{i:03d}",
                days_old=10 - i,  # varying ages
                size=i * 1000 + 1000,
            )
            for i in range(count)
        ]
        msg_repo.upsert_many(msgs)
        session.flush()

    def test_count_inbox(self, msg_repo: MessageRepository,
                         session: Session) -> None:
        self._setup(msg_repo, session, 4)
        assert msg_repo.count_inbox() == 4

    def test_count_inbox_zero(self, msg_repo: MessageRepository) -> None:
        assert msg_repo.count_inbox() == 0

    def test_list_inbox_oldest_first(self, msg_repo: MessageRepository,
                                     session: Session) -> None:
        self._setup(msg_repo, session, 3)
        msgs = msg_repo.list_inbox(oldest_first=True)
        dates = [m.internal_date for m in msgs]
        assert dates == sorted(dates)

    def test_list_inbox_newest_first(self, msg_repo: MessageRepository,
                                     session: Session) -> None:
        self._setup(msg_repo, session, 3)
        msgs = msg_repo.list_inbox(oldest_first=False)
        dates = [m.internal_date for m in msgs]
        assert dates == sorted(dates, reverse=True)

    def test_list_inbox_limit(self, msg_repo: MessageRepository,
                               session: Session) -> None:
        self._setup(msg_repo, session, 5)
        msgs = msg_repo.list_inbox(limit=2)
        assert len(msgs) == 2

    def test_inbox_size_bytes(self, msg_repo: MessageRepository,
                               session: Session) -> None:
        _insert_thread_and_message(session)
        msgs = [
            _make_inbox_message(msg_id=f"m{i}", size=1_000_000)
            for i in range(3)
        ]
        msg_repo.upsert_many(msgs)
        session.flush()
        assert msg_repo.inbox_size_bytes() == 3_000_000

    def test_inbox_size_bytes_empty(self, msg_repo: MessageRepository) -> None:
        assert msg_repo.inbox_size_bytes() == 0


@pytest.mark.unit
class TestMessageRepositoryArchiveQueries:
    """Archive hygiene workflow queries."""

    def test_count_archive_unlabelled(self, msg_repo: MessageRepository,
                                       session: Session) -> None:
        _insert_thread_and_message(session)
        unlabelled = [
            _make_archived_message(msg_id=f"a{i}", has_label=False)
            for i in range(3)
        ]
        labelled = _make_archived_message(msg_id="a3", has_label=True)
        msg_repo.upsert_many([*unlabelled, labelled])
        session.flush()
        assert msg_repo.count_archive_unlabelled() == 3

    def test_list_archive_unlabelled(self, msg_repo: MessageRepository,
                                      session: Session) -> None:
        _insert_thread_and_message(session)
        unlabelled = [
            _make_archived_message(msg_id=f"a{i}", has_label=False)
            for i in range(2)
        ]
        msg_repo.upsert_many(unlabelled)
        session.flush()
        results = msg_repo.list_archive_unlabelled()
        assert len(results) == 2
        assert all(not m.has_custom_label for m in results)
        assert all(m.is_archived for m in results)


@pytest.mark.unit
class TestMessageRepositoryAnalytics:
    """Analytics queries: top senders, size, coverage."""

    def _setup_senders(self, msg_repo: MessageRepository,
                        session: Session) -> None:
        _insert_thread_and_message(session)
        senders_data = [
            ("a@alpha.com", 3, 1_000_000),
            ("b@beta.com", 5, 500_000),
            ("c@gamma.com", 1, 2_000_000),
        ]
        i = 0
        for sender, count, size in senders_data:
            for _j in range(count):
                msg = make_message(
                    id=f"msg_{i:04d}",
                    thread_id="thread_001",
                    history_id="h",
                    internal_date=datetime.now(tz=UTC),
                    sender=sender,
                    sender_domain=sender.split("@")[1],
                    size_estimate=size,
                    label_ids=frozenset({"INBOX"}),
                )
                msg_repo.upsert(msg)
                i += 1
        session.flush()

    def test_top_senders_by_count(self, msg_repo: MessageRepository,
                                   session: Session) -> None:
        self._setup_senders(msg_repo, session)
        top = msg_repo.top_senders_by_count(limit=1)
        assert len(top) == 1
        assert top[0].sender == "b@beta.com"
        assert top[0].message_count == 5

    def test_top_senders_by_size(self, msg_repo: MessageRepository,
                                  session: Session) -> None:
        self._setup_senders(msg_repo, session)
        # alpha: 3 msgs x 1_000_000 = 3_000_000 (largest total)
        # beta:  5 msgs x 500_000   = 2_500_000
        # gamma: 1 msg  x 2_000_000 = 2_000_000
        top = msg_repo.top_senders_by_size(limit=1)
        assert len(top) == 1
        assert top[0].sender == "a@alpha.com"
        assert top[0].total_size_bytes == 3_000_000

    def test_total_size_bytes(self, msg_repo: MessageRepository,
                               session: Session) -> None:
        _insert_thread_and_message(session)
        msgs = [
            _make_inbox_message(msg_id=f"m{i}", size=1_048_576)
            for i in range(4)
        ]
        msg_repo.upsert_many(msgs)
        session.flush()
        assert msg_repo.total_size_bytes() == 4 * 1_048_576

    def test_total_size_bytes_empty(self, msg_repo: MessageRepository) -> None:
        assert msg_repo.total_size_bytes() == 0

    def test_custom_label_coverage_empty(self, msg_repo: MessageRepository) -> None:
        assert msg_repo.custom_label_coverage_pct() == 0.0

    def test_custom_label_coverage(self, msg_repo: MessageRepository,
                                    session: Session) -> None:
        _insert_thread_and_message(session)
        labelled = _make_archived_message("m1", has_label=True)
        unlabelled = _make_archived_message("m2", has_label=False)
        msg_repo.upsert_many([labelled, unlabelled])
        session.flush()
        assert msg_repo.custom_label_coverage_pct() == 50.0

    def test_list_largest(self, msg_repo: MessageRepository,
                           session: Session) -> None:
        _insert_thread_and_message(session)
        sizes = [100_000, 50_000, 200_000, 10_000]
        msgs = [
            _make_inbox_message(msg_id=f"m{i}", size=s)
            for i, s in enumerate(sizes)
        ]
        msg_repo.upsert_many(msgs)
        session.flush()
        largest = msg_repo.list_largest(limit=2)
        assert largest[0].size_estimate == 200_000
        assert largest[1].size_estimate == 100_000


@pytest.mark.unit
class TestMessageRepositorySearch:
    """Filtered search queries."""

    def _setup(self, msg_repo: MessageRepository, session: Session) -> None:
        _insert_thread_and_message(session)
        msgs = [
            make_message(
                id="msg_inbox_unread",
                thread_id="thread_001",
                history_id="h",
                internal_date=datetime.now(tz=UTC) - timedelta(days=1),
                sender="alice@corp.com",
                sender_domain="corp.com",
                subject="Hello from Alice",
                size_estimate=5_000,
                label_ids=frozenset({"INBOX", "UNREAD"}),
            ),
            make_message(
                id="msg_inbox_read",
                thread_id="thread_001",
                history_id="h",
                internal_date=datetime.now(tz=UTC) - timedelta(days=5),
                sender="bob@other.com",
                sender_domain="other.com",
                subject="Hello from Bob",
                size_estimate=15_000,
                label_ids=frozenset({"INBOX"}),
            ),
            _make_archived_message("msg_archived", has_label=False),
        ]
        msg_repo.upsert_many(msgs)
        session.flush()

    def test_search_all(self, msg_repo: MessageRepository,
                         session: Session) -> None:
        self._setup(msg_repo, session)
        results = msg_repo.search(MessageFilter())
        assert len(results) == 3

    def test_search_by_sender(self, msg_repo: MessageRepository,
                               session: Session) -> None:
        self._setup(msg_repo, session)
        results = msg_repo.search(MessageFilter(sender="alice"))
        assert len(results) == 1
        assert results[0].sender == "alice@corp.com"

    def test_search_by_is_inbox(self, msg_repo: MessageRepository,
                                 session: Session) -> None:
        self._setup(msg_repo, session)
        results = msg_repo.search(MessageFilter(is_inbox=True))
        assert len(results) == 2
        assert all(m.is_inbox for m in results)

    def test_search_by_is_unread(self, msg_repo: MessageRepository,
                                  session: Session) -> None:
        self._setup(msg_repo, session)
        results = msg_repo.search(MessageFilter(is_unread=True))
        assert len(results) == 1
        assert results[0].id == "msg_inbox_unread"

    def test_search_by_min_size(self, msg_repo: MessageRepository,
                                 session: Session) -> None:
        self._setup(msg_repo, session)
        results = msg_repo.search(MessageFilter(min_size_bytes=10_000))
        assert all(m.size_estimate >= 10_000 for m in results)

    def test_search_limit(self, msg_repo: MessageRepository,
                           session: Session) -> None:
        self._setup(msg_repo, session)
        results = msg_repo.search(MessageFilter(limit=1))
        assert len(results) == 1

    def test_count_search(self, msg_repo: MessageRepository,
                           session: Session) -> None:
        self._setup(msg_repo, session)
        count = msg_repo.count_search(MessageFilter(is_inbox=True))
        assert count == 2


@pytest.mark.unit
class TestMessageRepositoryLabelUpdate:
    """update_labels() recalculates all derived boolean fields."""

    def test_update_labels_recalculates_is_inbox(
        self, msg_repo: MessageRepository, session: Session
    ) -> None:
        _insert_thread_and_message(session)
        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()
        assert msg_repo.count_inbox() == 1

        # Simulate Gmail updating labels externally (e.g. user archived in Gmail)
        msg_repo.update_labels(msg.id, frozenset({"Label_custom"}))
        session.flush()

        fetched = msg_repo.get_by_id(msg.id)
        assert fetched is not None
        assert fetched.is_inbox is False
        assert fetched.is_archived is True
        assert fetched.has_custom_label is True

    def test_update_labels_recalculates_is_unread(
        self, msg_repo: MessageRepository, session: Session
    ) -> None:
        _insert_thread_and_message(session)
        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()
        assert msg_repo.get_by_id(msg.id).is_unread is True  # type: ignore[union-attr]

        msg_repo.update_labels(msg.id, frozenset({"INBOX"}))  # UNREAD removed
        session.flush()
        assert msg_repo.get_by_id(msg.id).is_unread is False  # type: ignore[union-attr]


# ── LabelRepository tests ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestLabelRepository:
    """Label CRUD and junction table operations."""

    def test_upsert_and_get(self, label_repo: LabelRepository,
                             session: Session) -> None:
        label = _make_label("Label_001", "Test")
        label_repo.upsert(label)
        session.flush()
        fetched = label_repo.get_by_id("Label_001")
        assert fetched is not None
        assert fetched.name == "Test"

    def test_upsert_is_idempotent(self, label_repo: LabelRepository,
                                   session: Session) -> None:
        label = _make_label("Label_001", "Test")
        label_repo.upsert(label)
        label_repo.upsert(label)
        session.flush()
        assert label_repo.get_by_id("Label_001") is not None

    def test_upsert_updates_name(self, label_repo: LabelRepository,
                                  session: Session) -> None:
        label_repo.upsert(_make_label("Label_001", "Original"))
        session.flush()
        label_repo.upsert(_make_label("Label_001", "Renamed"))
        session.flush()
        assert label_repo.get_by_id("Label_001").name == "Renamed"  # type: ignore[union-attr]

    def test_get_by_name(self, label_repo: LabelRepository,
                          session: Session) -> None:
        label_repo.upsert(_make_label("Label_001", "MyLabel"))
        session.flush()
        fetched = label_repo.get_by_name("MyLabel")
        assert fetched is not None
        assert fetched.id == "Label_001"

    def test_get_by_name_not_found(self, label_repo: LabelRepository) -> None:
        assert label_repo.get_by_name("NoSuchLabel") is None

    def test_list_all(self, label_repo: LabelRepository,
                       session: Session) -> None:
        labels = [
            _make_label("Label_001", "B", LabelType.USER),
            _make_label("INBOX", "INBOX", LabelType.SYSTEM),
            _make_label("Label_002", "A", LabelType.USER),
        ]
        label_repo.upsert_many(labels)
        session.flush()
        all_labels = label_repo.list_all()
        assert len(all_labels) == 3
        # system before user, then alphabetical within type
        assert all_labels[0].label_type == LabelType.SYSTEM

    def test_list_user_labels_only(self, label_repo: LabelRepository,
                                    session: Session) -> None:
        label_repo.upsert(_make_label("INBOX", "INBOX", LabelType.SYSTEM))
        label_repo.upsert(_make_label("Label_001", "User Label", LabelType.USER))
        session.flush()
        user_labels = label_repo.list_user_labels()
        assert len(user_labels) == 1
        assert user_labels[0].label_type == LabelType.USER

    def test_exists(self, label_repo: LabelRepository,
                     session: Session) -> None:
        label_repo.upsert(_make_label("Label_001"))
        session.flush()
        assert label_repo.exists("Label_001") is True
        assert label_repo.exists("nonexistent") is False

    def test_count_messages_with_label(
        self, label_repo: LabelRepository,
        msg_repo: MessageRepository,
        session: Session,
    ) -> None:
        _insert_thread_and_message(session)
        label_repo.upsert(_make_label("Label_001", "Test"))
        session.flush()

        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()

        label_repo.sync_message_labels(msg.id, frozenset({"INBOX", "Label_001"}))
        session.flush()

        count = label_repo.count_messages_with_label("Label_001")
        assert count == 1

    def test_sync_message_labels_replaces_existing(
        self, label_repo: LabelRepository,
        msg_repo: MessageRepository,
        session: Session,
    ) -> None:
        _insert_thread_and_message(session)
        label_repo.upsert(_make_label("Label_A", "A"))
        label_repo.upsert(_make_label("Label_B", "B"))
        session.flush()

        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()

        label_repo.sync_message_labels(msg.id, frozenset({"INBOX", "Label_A"}))
        session.flush()
        assert label_repo.count_messages_with_label("Label_A") == 1

        label_repo.sync_message_labels(msg.id, frozenset({"Label_B"}))
        session.flush()
        assert label_repo.count_messages_with_label("Label_A") == 0
        assert label_repo.count_messages_with_label("Label_B") == 1

    def test_get_label_ids_for_message(
        self, label_repo: LabelRepository,
        msg_repo: MessageRepository,
        session: Session,
    ) -> None:
        _insert_thread_and_message(session)
        label_repo.upsert(_make_label("Label_001", "Test"))
        session.flush()
        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()
        label_repo.sync_message_labels(msg.id, frozenset({"Label_001"}))
        session.flush()
        ids = label_repo.get_label_ids_for_message(msg.id)
        assert "Label_001" in ids

    def test_log_label_operation(
        self, label_repo: LabelRepository,
        msg_repo: MessageRepository,
        session: Session,
    ) -> None:
        _insert_thread_and_message(session)
        msg = _make_inbox_message()
        msg_repo.upsert(msg)
        session.flush()

        label_repo.log_label_operation(
            message_id=msg.id,
            operation="add",
            label_id="Label_001",
            label_name="Test Label",
            success=True,
        )
        session.flush()
        # No assertion on count — just verify it doesn't raise
        # Audit log contents tested via SQL in integration tests


# ── SyncStateRepository tests ─────────────────────────────────────────────────


@pytest.mark.unit
class TestSyncStateRepository:
    """Sync state watermark and thread operations."""

    def _make_sync_state(self, sync_type: SyncType = SyncType.FULL,
                          history_id: str = "hist_abc") -> SyncState:
        now = datetime.now(tz=UTC)
        return SyncState(
            id=None,
            history_id=history_id,
            last_synced_at=now,
            sync_type=sync_type,
            messages_synced=42,
            created_at=now,
        )

    def test_save_returns_id(self, sync_repo: SyncStateRepository,
                              session: Session) -> None:
        state = self._make_sync_state()
        saved = sync_repo.save(state)
        assert saved.id is not None

    def test_latest_returns_most_recent(self, sync_repo: SyncStateRepository,
                                         session: Session) -> None:
        sync_repo.save(self._make_sync_state(history_id="first"))
        sync_repo.save(self._make_sync_state(history_id="second"))
        session.flush()
        latest = sync_repo.latest()
        assert latest is not None
        assert latest.history_id == "second"

    def test_latest_none_when_empty(self, sync_repo: SyncStateRepository) -> None:
        assert sync_repo.latest() is None

    def test_has_ever_synced_false(self, sync_repo: SyncStateRepository) -> None:
        assert sync_repo.has_ever_synced() is False

    def test_has_ever_synced_true(self, sync_repo: SyncStateRepository,
                                   session: Session) -> None:
        sync_repo.save(self._make_sync_state())
        session.flush()
        assert sync_repo.has_ever_synced() is True

    def test_list_recent(self, sync_repo: SyncStateRepository,
                          session: Session) -> None:
        for i in range(5):
            sync_repo.save(self._make_sync_state(history_id=f"hist_{i}"))
        session.flush()
        results = sync_repo.list_recent(limit=3)
        assert len(results) == 3
        # Most recent first
        assert results[0].history_id == "hist_4"

    def test_last_full_sync(self, sync_repo: SyncStateRepository,
                             session: Session) -> None:
        sync_repo.save(self._make_sync_state(SyncType.FULL, "full_1"))
        sync_repo.save(self._make_sync_state(SyncType.INCREMENTAL, "incr_1"))
        sync_repo.save(self._make_sync_state(SyncType.FULL, "full_2"))
        session.flush()
        last = sync_repo.last_full_sync()
        assert last is not None
        assert last.history_id == "full_2"

    def test_upsert_thread(self, sync_repo: SyncStateRepository,
                            session: Session) -> None:
        thread = _make_thread("thread_x")
        sync_repo.upsert_thread(thread)
        session.flush()
        fetched = sync_repo.get_thread_by_id("thread_x")
        assert fetched is not None
        assert fetched.subject == "Test thread"

    def test_upsert_thread_idempotent(self, sync_repo: SyncStateRepository,
                                       session: Session) -> None:
        thread = _make_thread("thread_x")
        sync_repo.upsert_thread(thread)
        sync_repo.upsert_thread(thread)
        session.flush()
        assert sync_repo.get_thread_by_id("thread_x") is not None

    def test_count_old_inbox_threads(self, sync_repo: SyncStateRepository,
                                      session: Session) -> None:
        old_thread = Thread(
            id="old_thread",
            subject="Old",
            message_count=1,
            snippet="x",
            last_message_at=datetime.now(tz=UTC) - timedelta(days=60),
            is_inbox=True,
            has_custom_label=False,
            last_synced_at=datetime.now(tz=UTC),
        )
        recent_thread = Thread(
            id="recent_thread",
            subject="Recent",
            message_count=1,
            snippet="y",
            last_message_at=datetime.now(tz=UTC) - timedelta(days=5),
            is_inbox=True,
            has_custom_label=False,
            last_synced_at=datetime.now(tz=UTC),
        )
        sync_repo.upsert_threads([old_thread, recent_thread])
        session.flush()
        count = sync_repo.count_old_inbox_threads(threshold_days=30)
        assert count == 1


# ── SnapshotRepository tests ──────────────────────────────────────────────────


@pytest.mark.unit
class TestSnapshotRepository:
    """Daily snapshot persistence and time-series queries."""

    def _make_snap(self, snapshot_date: date, inbox_count: int = 100) -> DailySnapshot:
        return DailySnapshot(
            snapshot_date=snapshot_date,
            inbox_count=inbox_count,
            inbox_size_bytes=inbox_count * 10_000,
            archive_unlabelled_count=inbox_count * 2,
            sent_unresolved_count=10,
            total_size_bytes=inbox_count * 100_000,
            custom_label_coverage_pct=50.0,
        )

    def test_upsert_and_get(self, snap_repo: SnapshotRepository,
                             session: Session) -> None:
        today = date.today()
        snap = self._make_snap(today, inbox_count=42)
        snap_repo.upsert(snap)
        session.flush()
        fetched = snap_repo.get_by_date(today)
        assert fetched is not None
        assert fetched.inbox_count == 42

    def test_upsert_is_idempotent(self, snap_repo: SnapshotRepository,
                                   session: Session) -> None:
        today = date.today()
        snap_repo.upsert(self._make_snap(today, inbox_count=100))
        snap_repo.upsert(self._make_snap(today, inbox_count=50))
        session.flush()
        fetched = snap_repo.get_by_date(today)
        assert fetched is not None
        assert fetched.inbox_count == 50  # second upsert wins

    def test_get_by_date_not_found(self, snap_repo: SnapshotRepository) -> None:
        assert snap_repo.get_by_date(date(2000, 1, 1)) is None

    def test_list_recent_sorted_oldest_first(
        self, snap_repo: SnapshotRepository, session: Session
    ) -> None:
        today = date.today()
        for i in range(5):
            snap_repo.upsert(self._make_snap(today - timedelta(days=i)))
        session.flush()
        results = snap_repo.list_recent(days=30)
        dates = [s.snapshot_date for s in results]
        assert dates == sorted(dates)

    def test_list_recent_respects_window(
        self, snap_repo: SnapshotRepository, session: Session
    ) -> None:
        today = date.today()
        snap_repo.upsert(self._make_snap(today - timedelta(days=5)))
        snap_repo.upsert(self._make_snap(today - timedelta(days=60)))
        session.flush()
        results = snap_repo.list_recent(days=30)
        assert len(results) == 1

    def test_latest_returns_most_recent(
        self, snap_repo: SnapshotRepository, session: Session
    ) -> None:
        today = date.today()
        snap_repo.upsert(self._make_snap(today - timedelta(days=2)))
        snap_repo.upsert(self._make_snap(today))
        session.flush()
        latest = snap_repo.latest()
        assert latest is not None
        assert latest.snapshot_date == today

    def test_latest_none_when_empty(self, snap_repo: SnapshotRepository) -> None:
        assert snap_repo.latest() is None

    def test_count(self, snap_repo: SnapshotRepository,
                    session: Session) -> None:
        today = date.today()
        snap_repo.upsert(self._make_snap(today - timedelta(days=1)))
        snap_repo.upsert(self._make_snap(today))
        session.flush()
        assert snap_repo.count() == 2
