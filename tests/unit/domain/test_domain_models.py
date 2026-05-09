"""
Unit tests for domain model entities.

Covers: Message, Label, Thread, SyncState, DailySnapshot, and the
make_message factory.  All tests are pure — no I/O, no mocking.

Organised one test class per model, with sub-sections for construction,
properties, and edge cases.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from domain.models import (
    DailySnapshot,
    Label,
    LabelListVisibility,
    LabelType,
    Message,
    MessageListVisibility,
    SyncState,
    SyncType,
    Thread,
    make_message,
)

# ## Shared fixtures ##


@pytest.fixture
def now() -> datetime:
    return datetime.now(tz=UTC)


@pytest.fixture
def inbox_message(now: datetime) -> Message:
    """A typical unread inbox message with no custom labels."""
    return make_message(
        id="msg_inbox_001",
        thread_id="thread_001",
        history_id="hist_001",
        internal_date=now - timedelta(days=3),
        sender="sender@example.com",
        sender_domain="example.com",
        recipient="me@gmail.com",
        subject="Test inbox message",
        snippet="This is a test...",
        size_estimate=12_000,
        label_ids=frozenset({"INBOX", "UNREAD"}),
    )


@pytest.fixture
def archived_message(now: datetime) -> Message:
    """An archived message with a custom user label."""
    return make_message(
        id="msg_archive_001",
        thread_id="thread_002",
        history_id="hist_002",
        internal_date=now - timedelta(days=60),
        sender="newsletter@acme.com",
        sender_domain="acme.com",
        subject="Monthly digest",
        snippet="Here is this month's...",
        size_estimate=1_500_000,
        label_ids=frozenset({"Label_userNewsletter"}),
    )


@pytest.fixture
def sent_message(now: datetime) -> Message:
    """A sent message."""
    return make_message(
        id="msg_sent_001",
        thread_id="thread_003",
        history_id="hist_003",
        internal_date=now - timedelta(days=7),
        sender="me@gmail.com",
        sender_domain="gmail.com",
        recipient="boss@corp.com",
        subject="Q3 Report",
        snippet="Please find attached...",
        size_estimate=8_500_000,
        label_ids=frozenset({"SENT"}),
    )


# ── Message tests ─────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestMessage:
    """Tests for the Message domain entity and make_message factory."""

    # ## Construction and field mapping ##

    def test_inbox_message_flags(self, inbox_message: Message) -> None:
        assert inbox_message.is_inbox is True
        assert inbox_message.is_unread is True
        assert inbox_message.is_sent is False
        assert inbox_message.is_archived is False
        assert inbox_message.is_starred is False
        assert inbox_message.is_important is False

    def test_archived_message_flags(self, archived_message: Message) -> None:
        assert archived_message.is_inbox is False
        assert archived_message.is_archived is True
        assert archived_message.is_sent is False

    def test_sent_message_flags(self, sent_message: Message) -> None:
        assert sent_message.is_sent is True
        assert sent_message.is_inbox is False
        # Sent messages are archived (not in inbox/trash/spam)
        assert sent_message.is_archived is True

    def test_message_is_frozen(self, inbox_message: Message) -> None:
        with pytest.raises(AttributeError):
            inbox_message.subject = "mutated"  # type: ignore[misc]

    def test_make_message_defaults(self, now: datetime) -> None:
        """Defaults: empty labels, zero size, all booleans False except is_archived."""
        msg = make_message(
            id="x",
            thread_id="t",
            history_id="h",
            internal_date=now,
            sender="a@b.com",
            sender_domain="b.com",
        )
        assert msg.label_ids == frozenset()
        assert msg.size_estimate == 0
        assert msg.is_unread is False
        assert msg.is_inbox is False
        assert msg.is_sent is False
        # No inbox, trash, spam → is_archived == True
        assert msg.is_archived is True
        assert msg.has_custom_label is False
        assert msg.recipient is None
        assert msg.subject is None
        assert msg.snippet is None

    def test_make_message_timestamps_default_to_now(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h",
            internal_date=now, sender="a@b.com", sender_domain="b.com",
        )
        # Both timestamps should be close to now (within 1 second)
        assert abs((msg.first_seen_at - now).total_seconds()) < 1
        assert abs((msg.last_synced_at - now).total_seconds()) < 1

    def test_make_message_explicit_timestamps(self, now: datetime) -> None:
        first = now - timedelta(days=10)
        last = now - timedelta(hours=1)
        msg = make_message(
            id="x", thread_id="t", history_id="h",
            internal_date=now, sender="a@b.com", sender_domain="b.com",
            first_seen_at=first, last_synced_at=last,
        )
        assert msg.first_seen_at == first
        assert msg.last_synced_at == last

    # ## has_custom_label derivation ##

    def test_has_custom_label_with_user_label(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset({"INBOX", "Label_userDefined123"}),
        )
        assert msg.has_custom_label is True

    def test_no_custom_label_with_only_system_labels(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset({"INBOX", "UNREAD", "IMPORTANT", "CATEGORY_PERSONAL"}),
        )
        assert msg.has_custom_label is False

    def test_category_labels_not_counted_as_custom(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset({"INBOX", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}),
        )
        assert msg.has_custom_label is False

    # ## is_archived derivation ##

    def test_not_archived_when_in_inbox(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset({"INBOX"}),
        )
        assert msg.is_archived is False

    def test_not_archived_when_in_trash(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset({"TRASH"}),
        )
        assert msg.is_archived is False

    def test_not_archived_when_in_spam(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset({"SPAM"}),
        )
        assert msg.is_archived is False

    def test_archived_when_not_in_inbox_trash_spam(self, archived_message: Message) -> None:
        assert archived_message.is_archived is True

    # ## Derived properties ##

    def test_size_in_mb(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            size_estimate=5 * 1024 * 1024,
        )
        assert msg.size_in_mb == 5.0

    def test_size_in_mb_zero(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
        )
        assert msg.size_in_mb == 0.0

    def test_age_days_recent(self, inbox_message: Message) -> None:
        # inbox_message internal_date is now - 3 days
        assert inbox_message.age_days == 3

    def test_age_days_old(self, archived_message: Message) -> None:
        # archived_message internal_date is now - 60 days
        assert archived_message.age_days == 60

    def test_age_days_never_negative(self, now: datetime) -> None:
        # Future-dated message (edge case from clock skew)
        msg = make_message(
            id="x", thread_id="t", history_id="h",
            internal_date=now + timedelta(hours=1),
            sender="a@b.com", sender_domain="b.com",
        )
        assert msg.age_days == 0

    def test_is_large_above_threshold(self, sent_message: Message) -> None:
        # sent_message is 8.5 MB > 5 MB threshold
        assert sent_message.is_large is True

    def test_is_large_below_threshold(self, inbox_message: Message) -> None:
        # inbox_message is 12 KB
        assert inbox_message.is_large is False

    def test_needs_label_archived_unlabelled(self, now: datetime) -> None:
        msg = make_message(
            id="x", thread_id="t", history_id="h", internal_date=now,
            sender="a@b.com", sender_domain="b.com",
            label_ids=frozenset(),  # archived, no custom label
        )
        assert msg.needs_label is True

    def test_needs_label_false_when_in_inbox(self, inbox_message: Message) -> None:
        assert inbox_message.needs_label is False

    def test_needs_label_false_when_labelled(self, archived_message: Message) -> None:
        # archived_message has Label_userNewsletter
        assert archived_message.needs_label is False

    # ## with_labels mutation ##

    def test_with_labels_produces_new_instance(self, inbox_message: Message) -> None:
        updated = inbox_message.with_labels(frozenset({"INBOX", "Label_complete"}))
        assert updated is not inbox_message

    def test_with_labels_updates_has_custom_label(self, inbox_message: Message) -> None:
        assert inbox_message.has_custom_label is False
        updated = inbox_message.with_labels(frozenset({"INBOX", "Label_complete"}))
        assert updated.has_custom_label is True

    def test_with_labels_updates_is_unread(self, inbox_message: Message) -> None:
        assert inbox_message.is_unread is True
        updated = inbox_message.with_labels(frozenset({"INBOX"}))
        assert updated.is_unread is False

    def test_with_labels_updates_is_inbox(self, inbox_message: Message) -> None:
        # Removing INBOX (simulating external archive in Gmail — we track it, not do it)
        updated = inbox_message.with_labels(frozenset({"Label_complete"}))
        assert updated.is_inbox is False
        assert updated.is_archived is True

    def test_with_labels_preserves_immutable_fields(self, inbox_message: Message) -> None:
        """Fields that don't derive from labels must be preserved unchanged."""
        updated = inbox_message.with_labels(frozenset({"INBOX"}))
        assert updated.id == inbox_message.id
        assert updated.thread_id == inbox_message.thread_id
        assert updated.sender == inbox_message.sender
        assert updated.subject == inbox_message.subject
        assert updated.size_estimate == inbox_message.size_estimate
        assert updated.internal_date == inbox_message.internal_date
        assert updated.first_seen_at == inbox_message.first_seen_at

    def test_with_labels_empty_set(self, inbox_message: Message) -> None:
        updated = inbox_message.with_labels(frozenset())
        assert updated.label_ids == frozenset()
        assert updated.is_inbox is False
        assert updated.is_unread is False
        assert updated.has_custom_label is False
        assert updated.is_archived is True

    # ## __str__ ##

    def test_str_inbox_message(self, inbox_message: Message) -> None:
        s = str(inbox_message)
        assert "msg_inbox_001" in s
        assert "inbox" in s

    def test_str_sent_message(self, sent_message: Message) -> None:
        s = str(sent_message)
        assert "sent" in s

    def test_str_archived_message(self, archived_message: Message) -> None:
        s = str(archived_message)
        assert "archived" in s


# ── Label tests ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestLabel:
    """Tests for the Label domain entity."""

    @pytest.fixture
    def system_label(self, now: datetime) -> Label:
        return Label(
            id="INBOX",
            name="INBOX",
            label_type=LabelType.SYSTEM,
            message_list_visibility=MessageListVisibility.SHOW,
            label_list_visibility=LabelListVisibility.LABEL_SHOW,
            synced_at=now,
        )

    @pytest.fixture
    def user_label(self, now: datetime) -> Label:
        return Label(
            id="Label_1234567890",
            name="ZeroApp/Needs-Action",
            label_type=LabelType.USER,
            message_list_visibility=MessageListVisibility.SHOW,
            label_list_visibility=LabelListVisibility.LABEL_SHOW,
            synced_at=now,
        )

    @pytest.fixture
    def plain_user_label(self, now: datetime) -> Label:
        return Label(
            id="Label_9876543210",
            name="Work",
            label_type=LabelType.USER,
            message_list_visibility=None,
            label_list_visibility=None,
            synced_at=now,
        )

    def test_system_label_is_system(self, system_label: Label) -> None:
        assert system_label.is_system is True
        assert system_label.is_user is False

    def test_user_label_is_user(self, user_label: Label) -> None:
        assert user_label.is_system is False
        assert user_label.is_user is True

    def test_zeroapp_label_is_app_managed(self, user_label: Label) -> None:
        assert user_label.is_app_managed is True

    def test_plain_user_label_not_app_managed(self, plain_user_label: Label) -> None:
        assert plain_user_label.is_app_managed is False

    def test_system_label_not_app_managed(self, system_label: Label) -> None:
        assert system_label.is_app_managed is False

    def test_label_is_frozen(self, system_label: Label) -> None:
        with pytest.raises(AttributeError):
            system_label.name = "mutated"  # type: ignore[misc]

    def test_none_visibility_fields_accepted(self, plain_user_label: Label) -> None:
        assert plain_user_label.message_list_visibility is None
        assert plain_user_label.label_list_visibility is None

    def test_label_type_str_enum(self) -> None:
        assert LabelType.SYSTEM == "system"
        assert LabelType.USER == "user"

    def test_str_representation(self, system_label: Label) -> None:
        s = str(system_label)
        assert "INBOX" in s
        assert "system" in s


# ── Thread tests ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestThread:
    """Tests for the Thread domain entity."""

    @pytest.fixture
    def recent_inbox_thread(self, now: datetime) -> Thread:
        return Thread(
            id="thread_001",
            subject="Recent discussion",
            message_count=3,
            snippet="Latest reply...",
            last_message_at=now - timedelta(days=2),
            is_inbox=True,
            has_custom_label=False,
            last_synced_at=now,
        )

    @pytest.fixture
    def old_inbox_thread(self, now: datetime) -> Thread:
        return Thread(
            id="thread_002",
            subject="Old unresolved",
            message_count=1,
            snippet="Can you help...",
            last_message_at=now - timedelta(days=95),
            is_inbox=True,
            has_custom_label=False,
            last_synced_at=now,
        )

    @pytest.fixture
    def no_date_thread(self, now: datetime) -> Thread:
        return Thread(
            id="thread_003",
            subject=None,
            message_count=0,
            snippet=None,
            last_message_at=None,
            is_inbox=False,
            has_custom_label=False,
            last_synced_at=now,
        )

    def test_recent_thread_age(self, recent_inbox_thread: Thread) -> None:
        assert recent_inbox_thread.age_days == 2

    def test_old_thread_age(self, old_inbox_thread: Thread) -> None:
        assert old_inbox_thread.age_days == 95

    def test_no_date_thread_age_is_zero(self, no_date_thread: Thread) -> None:
        assert no_date_thread.age_days == 0

    def test_is_old_above_30_days(self, old_inbox_thread: Thread) -> None:
        assert old_inbox_thread.is_old is True

    def test_is_old_false_for_recent(self, recent_inbox_thread: Thread) -> None:
        assert recent_inbox_thread.is_old is False

    def test_is_very_old_above_90_days(self, old_inbox_thread: Thread) -> None:
        assert old_inbox_thread.is_very_old is True

    def test_is_very_old_false_for_30_day_thread(self, now: datetime) -> None:
        thread = Thread(
            id="t", subject=None, message_count=1, snippet=None,
            last_message_at=now - timedelta(days=45),
            is_inbox=True, has_custom_label=False, last_synced_at=now,
        )
        assert thread.is_very_old is False

    def test_is_single_message(self, old_inbox_thread: Thread) -> None:
        assert old_inbox_thread.is_single_message is True

    def test_is_not_single_message(self, recent_inbox_thread: Thread) -> None:
        assert recent_inbox_thread.is_single_message is False

    def test_thread_is_frozen(self, recent_inbox_thread: Thread) -> None:
        with pytest.raises(AttributeError):
            recent_inbox_thread.message_count = 99  # type: ignore[misc]

    def test_str_representation(self, recent_inbox_thread: Thread) -> None:
        s = str(recent_inbox_thread)
        assert "thread_001" in s
        assert "inbox=True" in s

    def test_age_days_never_negative(self, now: datetime) -> None:
        future_thread = Thread(
            id="t", subject=None, message_count=1, snippet=None,
            last_message_at=now + timedelta(hours=2),
            is_inbox=True, has_custom_label=False, last_synced_at=now,
        )
        assert future_thread.age_days == 0


# ## SyncState tests ##


@pytest.mark.unit
class TestSyncState:
    """Tests for the SyncState domain entity."""

    @pytest.fixture
    def full_sync_state(self, now: datetime) -> SyncState:
        return SyncState(
            id=1,
            history_id="hist_abc123",
            last_synced_at=now,
            sync_type=SyncType.FULL,
            messages_synced=12_289,
            created_at=now,
        )

    @pytest.fixture
    def incremental_sync_state(self, now: datetime) -> SyncState:
        return SyncState(
            id=2,
            history_id="hist_xyz789",
            last_synced_at=now,
            sync_type=SyncType.INCREMENTAL,
            messages_synced=23,
            created_at=now,
        )

    @pytest.fixture
    def unpersisted_sync_state(self, now: datetime) -> SyncState:
        """SyncState before it has been written to the DB (id is None)."""
        return SyncState(
            id=None,
            history_id="hist_new",
            last_synced_at=now,
            sync_type=SyncType.INCREMENTAL,
            messages_synced=5,
            created_at=now,
        )

    def test_full_sync_is_full(self, full_sync_state: SyncState) -> None:
        assert full_sync_state.is_full_sync is True
        assert full_sync_state.is_incremental_sync is False

    def test_incremental_sync_is_incremental(
        self, incremental_sync_state: SyncState
    ) -> None:
        assert incremental_sync_state.is_incremental_sync is True
        assert incremental_sync_state.is_full_sync is False

    def test_id_can_be_none_before_persistence(
        self, unpersisted_sync_state: SyncState
    ) -> None:
        assert unpersisted_sync_state.id is None

    def test_sync_state_is_frozen(self, full_sync_state: SyncState) -> None:
        with pytest.raises(AttributeError):
            full_sync_state.messages_synced = 0  # type: ignore[misc]

    def test_sync_type_str_enum(self) -> None:
        assert SyncType.FULL == "full"
        assert SyncType.INCREMENTAL == "incremental"

    def test_str_representation(self, full_sync_state: SyncState) -> None:
        s = str(full_sync_state)
        assert "hist_abc123" in s
        assert "full" in s
        assert "12289" in s


# ## DailySnapshot tests ##


@pytest.mark.unit
class TestDailySnapshot:
    """Tests for the DailySnapshot domain entity."""

    @pytest.fixture
    def healthy_snapshot(self) -> DailySnapshot:
        return DailySnapshot(
            snapshot_date=date(2024, 3, 12),
            inbox_count=50,
            inbox_size_bytes=52_428_800,    # 50 MB
            archive_unlabelled_count=200,
            sent_unresolved_count=10,
            total_size_bytes=4_294_967_296,  # 4 GB
            custom_label_coverage_pct=67.5,
        )

    @pytest.fixture
    def zero_snapshot(self) -> DailySnapshot:
        """Snapshot representing all four zero goals achieved."""
        return DailySnapshot(
            snapshot_date=date(2024, 6, 1),
            inbox_count=0,
            inbox_size_bytes=0,
            archive_unlabelled_count=0,
            sent_unresolved_count=0,
            total_size_bytes=1_073_741_824,  # 1 GB
            custom_label_coverage_pct=100.0,
        )

    def test_total_size_mb(self, healthy_snapshot: DailySnapshot) -> None:
        assert healthy_snapshot.total_size_mb == pytest.approx(4096.0)

    def test_total_size_gb(self, healthy_snapshot: DailySnapshot) -> None:
        assert healthy_snapshot.total_size_gb == pytest.approx(4.0)

    def test_inbox_size_mb(self, healthy_snapshot: DailySnapshot) -> None:
        assert healthy_snapshot.inbox_size_mb == pytest.approx(50.0)

    def test_inbox_zero_reached_false(self, healthy_snapshot: DailySnapshot) -> None:
        assert healthy_snapshot.inbox_zero_reached is False

    def test_inbox_zero_reached_true(self, zero_snapshot: DailySnapshot) -> None:
        assert zero_snapshot.inbox_zero_reached is True

    def test_archive_zero_reached_false(self, healthy_snapshot: DailySnapshot) -> None:
        assert healthy_snapshot.archive_zero_reached is False

    def test_archive_zero_reached_true(self, zero_snapshot: DailySnapshot) -> None:
        assert zero_snapshot.archive_zero_reached is True

    def test_sent_zero_reached_false(self, healthy_snapshot: DailySnapshot) -> None:
        assert healthy_snapshot.sent_zero_reached is False

    def test_sent_zero_reached_true(self, zero_snapshot: DailySnapshot) -> None:
        assert zero_snapshot.sent_zero_reached is True

    def test_snapshot_is_frozen(self, healthy_snapshot: DailySnapshot) -> None:
        with pytest.raises(AttributeError):
            healthy_snapshot.inbox_count = 0  # type: ignore[misc]

    def test_zero_size_snapshot(self) -> None:
        snap = DailySnapshot(
            snapshot_date=date.today(),
            inbox_count=0,
            inbox_size_bytes=0,
            archive_unlabelled_count=0,
            sent_unresolved_count=0,
            total_size_bytes=0,
            custom_label_coverage_pct=0.0,
        )
        assert snap.total_size_mb == 0.0
        assert snap.total_size_gb == 0.0

    def test_str_representation(self, healthy_snapshot: DailySnapshot) -> None:
        s = str(healthy_snapshot)
        assert "2024-03-12" in s
        assert "67.5%" in s
