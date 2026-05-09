"""
SQLAlchemy ORM models for gmail_zero_app.

These are the infrastructure-layer database representations of domain entities.
They are intentionally separate from the domain dataclasses — the ORM models
are mutable, SQLAlchemy-aware objects, while domain entities are frozen
dataclasses with no infrastructure dependencies.

The mapper layer (infrastructure.gmail.mapper, and the repository ``to_domain``
methods here) handles translation between the two representations.

Table inventory:
    messages            — Gmail message metadata
    threads             — Gmail thread aggregates
    labels              — Gmail label registry
    message_labels      — normalised message-label junction
    sync_state          — incremental sync high-water marks
    daily_snapshots     — one row per sync day for progress graphs
    label_operations_log — append-only audit log of every write

Design decisions:
    - ``has_custom_label`` is denormalised onto both messages and threads
      for fast archive-hygiene queries without joins.
    - ``raw_label_ids`` is a denormalised JSON column for rendering speed;
      ``message_labels`` is the canonical normalised source.
    - ``daily_snapshots`` has no foreign keys — historical data must survive
      a full DB wipe and resync.
    - All datetime columns are stored as UTC and retrieved as UTC-aware objects
      via the ``timezone=True`` flag on DateTime columns.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from domain.models.daily_snapshot import DailySnapshot
from domain.models.label import Label, LabelListVisibility, LabelType, MessageListVisibility
from domain.models.message import Message, make_message
from domain.models.sync_state import SyncState, SyncType
from domain.models.thread import Thread


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


# ── Messages ──────────────────────────────────────────────────────────────────


class MessageORM(Base):
    """
    ORM model for the ``messages`` table.

    Stores Gmail message metadata fetched during sync.  All write operations
    use upsert (INSERT OR REPLACE) to ensure idempotency.
    """

    __tablename__ = "messages"

    # Gmail-assigned immutable message ID
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("threads.id"), nullable=False, index=True
    )
    history_id: Mapped[str] = mapped_column(String(255), nullable=False)
    internal_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Sender fields — domain stored separately for efficient analytics
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    sender_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    recipient: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Derived boolean flags — maintained by sync pipeline from label_ids
    is_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_inbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_important: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Denormalised: True if any user-defined label is present.
    # Maintained on every sync to avoid join on the archive-hygiene hot path.
    has_custom_label: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Denormalised JSON array of label IDs for fast rendering.
    # Canonical source is the message_labels junction table.
    raw_label_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Relationships
    thread: Mapped[ThreadORM] = relationship("ThreadORM", back_populates="messages")
    label_associations: Mapped[list[MessageLabelORM]] = relationship(
        "MessageLabelORM", back_populates="message", cascade="all, delete-orphan"
    )
    audit_log_entries: Mapped[list[LabelOperationLogORM]] = relationship(
        "LabelOperationLogORM", back_populates="message", cascade="all, delete-orphan"
    )

    # Indexes for common dashboard queries
    __table_args__ = (
        Index("idx_messages_is_inbox", "is_inbox"),
        Index("idx_messages_is_sent", "is_sent"),
        Index("idx_messages_is_archived", "is_archived"),
        Index("idx_messages_has_custom_label", "has_custom_label"),
        Index("idx_messages_sender_domain", "sender_domain"),
        Index("idx_messages_internal_date", "internal_date"),
        Index("idx_messages_size_estimate", "size_estimate"),
        Index("idx_messages_is_unread", "is_unread"),
        # Composite index for the archive-hygiene query hot path
        Index("idx_messages_archive_hygiene", "is_archived", "has_custom_label"),
    )

    def to_domain(self) -> Message:
        """Convert this ORM row to an immutable domain entity."""
        raw: list[str] = json.loads(self.raw_label_ids)
        return make_message(
            id=self.id,
            thread_id=self.thread_id,
            history_id=self.history_id,
            internal_date=_ensure_utc(self.internal_date),
            sender=self.sender,
            sender_domain=self.sender_domain,
            recipient=self.recipient,
            subject=self.subject,
            snippet=self.snippet,
            size_estimate=self.size_estimate,
            label_ids=frozenset(raw),
            first_seen_at=_ensure_utc(self.first_seen_at),
            last_synced_at=_ensure_utc(self.last_synced_at),
        )

    @classmethod
    def from_domain(cls, msg: Message) -> MessageORM:
        """Construct an ORM instance from a frozen domain entity."""
        now = datetime.now(tz=UTC)
        return cls(
            id=msg.id,
            thread_id=msg.thread_id,
            history_id=msg.history_id,
            internal_date=msg.internal_date,
            sender=msg.sender,
            sender_domain=msg.sender_domain,
            recipient=msg.recipient,
            subject=msg.subject,
            snippet=msg.snippet,
            size_estimate=msg.size_estimate,
            is_unread=msg.is_unread,
            is_inbox=msg.is_inbox,
            is_sent=msg.is_sent,
            is_archived=msg.is_archived,
            is_starred=msg.is_starred,
            is_important=msg.is_important,
            has_custom_label=msg.has_custom_label,
            raw_label_ids=json.dumps(sorted(msg.label_ids)),
            first_seen_at=msg.first_seen_at,
            last_synced_at=msg.last_synced_at or now,
        )

    def __repr__(self) -> str:
        return f"<MessageORM id={self.id!r} inbox={self.is_inbox}>"


# ── Threads ───────────────────────────────────────────────────────────────────


class ThreadORM(Base):
    """ORM model for the ``threads`` table."""

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_inbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_custom_label: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Relationships
    messages: Mapped[list[MessageORM]] = relationship(
        "MessageORM", back_populates="thread"
    )

    __table_args__ = (
        Index("idx_threads_is_inbox", "is_inbox"),
        Index("idx_threads_last_message_at", "last_message_at"),
    )

    def to_domain(self) -> Thread:
        return Thread(
            id=self.id,
            subject=self.subject,
            message_count=self.message_count,
            snippet=self.snippet,
            last_message_at=(
                _ensure_utc(self.last_message_at) if self.last_message_at else None
            ),
            is_inbox=self.is_inbox,
            has_custom_label=self.has_custom_label,
            last_synced_at=_ensure_utc(self.last_synced_at),
        )

    @classmethod
    def from_domain(cls, thread: Thread) -> ThreadORM:
        return cls(
            id=thread.id,
            subject=thread.subject,
            message_count=thread.message_count,
            snippet=thread.snippet,
            last_message_at=thread.last_message_at,
            is_inbox=thread.is_inbox,
            has_custom_label=thread.has_custom_label,
            last_synced_at=thread.last_synced_at,
        )

    def __repr__(self) -> str:
        return f"<ThreadORM id={self.id!r} messages={self.message_count}>"


# ── Labels ────────────────────────────────────────────────────────────────────


class LabelORM(Base):
    """ORM model for the ``labels`` table."""

    __tablename__ = "labels"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 'system' or 'user'
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    message_list_visibility: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    label_list_visibility: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relationships
    message_associations: Mapped[list[MessageLabelORM]] = relationship(
        "MessageLabelORM", back_populates="label", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("idx_labels_type", "type"),)

    def to_domain(self) -> Label:
        mlv = (
            MessageListVisibility(self.message_list_visibility)
            if self.message_list_visibility
            else None
        )
        llv = (
            LabelListVisibility(self.label_list_visibility)
            if self.label_list_visibility
            else None
        )
        return Label(
            id=self.id,
            name=self.name,
            label_type=LabelType(self.type),
            message_list_visibility=mlv,
            label_list_visibility=llv,
            synced_at=_ensure_utc(self.synced_at),
        )

    @classmethod
    def from_domain(cls, label: Label) -> LabelORM:
        return cls(
            id=label.id,
            name=label.name,
            type=label.label_type.value,
            message_list_visibility=(
                label.message_list_visibility.value
                if label.message_list_visibility
                else None
            ),
            label_list_visibility=(
                label.label_list_visibility.value
                if label.label_list_visibility
                else None
            ),
            synced_at=label.synced_at,
        )

    def __repr__(self) -> str:
        return f"<LabelORM id={self.id!r} name={self.name!r}>"


# ── Message-Label junction ────────────────────────────────────────────────────


class MessageLabelORM(Base):
    """
    ORM model for the ``message_labels`` junction table.

    This is the normalised, canonical source for which labels are applied
    to which messages.  ``MessageORM.raw_label_ids`` is a denormalised
    copy for rendering speed only.
    """

    __tablename__ = "message_labels"

    message_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("messages.id"), primary_key=True
    )
    label_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("labels.id"), primary_key=True
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Relationships
    message: Mapped[MessageORM] = relationship(
        "MessageORM", back_populates="label_associations"
    )
    label: Mapped[LabelORM] = relationship(
        "LabelORM", back_populates="message_associations"
    )

    __table_args__ = (Index("idx_message_labels_label_id", "label_id"),)

    def __repr__(self) -> str:
        return f"<MessageLabelORM msg={self.message_id!r} label={self.label_id!r}>"


# ── Sync state ────────────────────────────────────────────────────────────────


class SyncStateORM(Base):
    """
    ORM model for the ``sync_state`` table.

    One row per sync run.  The most recent row is the active sync watermark.
    Older rows are retained for diagnostic purposes.
    """

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    history_id: Mapped[str] = mapped_column(String(255), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 'full' or 'incremental'
    sync_type: Mapped[str] = mapped_column(String(32), nullable=False)
    messages_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def to_domain(self) -> SyncState:
        return SyncState(
            id=self.id,
            history_id=self.history_id,
            last_synced_at=_ensure_utc(self.last_synced_at),
            sync_type=SyncType(self.sync_type),
            messages_synced=self.messages_synced,
            created_at=_ensure_utc(self.created_at),
        )

    @classmethod
    def from_domain(cls, state: SyncState) -> SyncStateORM:
        return cls(
            history_id=state.history_id,
            last_synced_at=state.last_synced_at,
            sync_type=state.sync_type.value,
            messages_synced=state.messages_synced,
            created_at=state.created_at,
        )

    def __repr__(self) -> str:
        return (
            f"<SyncStateORM id={self.id} history_id={self.history_id!r} "
            f"type={self.sync_type}>"
        )


# ── Daily snapshots ───────────────────────────────────────────────────────────


class DailySnapshotORM(Base):
    """
    ORM model for the ``daily_snapshots`` table.

    One row per calendar date.  Upserted at the end of each sync run.
    No foreign keys — intentionally self-contained so historical data
    survives a full DB wipe and resync.
    """

    __tablename__ = "daily_snapshots"

    # Date is the primary key — one row per day, upserted on conflict
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    inbox_count: Mapped[int] = mapped_column(Integer, nullable=False)
    inbox_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    archive_unlabelled_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    custom_label_coverage_pct: Mapped[float] = mapped_column(Float, nullable=False)

    def to_domain(self) -> DailySnapshot:
        return DailySnapshot(
            snapshot_date=self.snapshot_date,
            inbox_count=self.inbox_count,
            inbox_size_bytes=self.inbox_size_bytes,
            archive_unlabelled_count=self.archive_unlabelled_count,
            sent_unresolved_count=self.sent_unresolved_count,
            total_size_bytes=self.total_size_bytes,
            custom_label_coverage_pct=self.custom_label_coverage_pct,
        )

    @classmethod
    def from_domain(cls, snap: DailySnapshot) -> DailySnapshotORM:
        return cls(
            snapshot_date=snap.snapshot_date,
            inbox_count=snap.inbox_count,
            inbox_size_bytes=snap.inbox_size_bytes,
            archive_unlabelled_count=snap.archive_unlabelled_count,
            sent_unresolved_count=snap.sent_unresolved_count,
            total_size_bytes=snap.total_size_bytes,
            custom_label_coverage_pct=snap.custom_label_coverage_pct,
        )

    def __repr__(self) -> str:
        return (
            f"<DailySnapshotORM date={self.snapshot_date} "
            f"inbox={self.inbox_count}>"
        )


# ── Label operations audit log ────────────────────────────────────────────────


class LabelOperationLogORM(Base):
    """
    ORM model for the ``label_operations_log`` table.

    Append-only audit trail of every label add/remove attempted by the app.
    Written by LabelService regardless of whether the API call succeeded.
    Never deleted or updated — only inserted.
    """

    __tablename__ = "label_operations_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("messages.id"), nullable=False
    )
    # 'add' or 'remove'
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    label_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Snapshot of label name at time of operation (label may be renamed later)
    label_name: Mapped[str] = mapped_column(Text, nullable=False)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationship
    message: Mapped[MessageORM] = relationship(
        "MessageORM", back_populates="audit_log_entries"
    )

    __table_args__ = (
        Index("idx_log_message_id", "message_id"),
        Index("idx_log_performed_at", "performed_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<LabelOperationLogORM id={self.id} op={self.operation!r} "
            f"label={self.label_id!r} success={self.success}>"
        )


# ── Utility ───────────────────────────────────────────────────────────────────


def _ensure_utc(dt: datetime) -> datetime:
    """
    Ensure a datetime is UTC-aware.

    SQLite stores datetimes without timezone info.  This helper attaches UTC
    to naive datetimes returned by SQLAlchemy so domain entities always receive
    timezone-aware values.

    Args:
        dt: A datetime that may or may not be timezone-aware.

    Returns:
        A UTC-aware datetime.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def get_all_orm_models() -> list[type[Base]]:
    """
    Return all ORM model classes in dependency order.

    Used by tests and the database initialiser to verify that all tables
    are represented.  Order matters for foreign-key constraints on non-SQLite
    backends.
    """
    return [
        ThreadORM,       # No FK deps
        LabelORM,        # No FK deps
        DailySnapshotORM,  # No FK deps
        SyncStateORM,    # No FK deps
        MessageORM,      # FK → threads
        MessageLabelORM, # FK → messages, labels
        LabelOperationLogORM,  # FK → messages
    ]
