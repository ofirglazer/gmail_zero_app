"""
MessageRepository — persistence operations for Gmail messages.

All queries return domain entities (frozen dataclasses), never ORM instances.
The repository is the only place that knows about the ORM model structure —
callers above this layer work exclusively with domain types.

Query methods are named after the dashboard views they serve, making the
relationship between UI and data access explicit and easy to trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from infrastructure.persistence.models import MessageLabelORM, MessageORM

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from domain.models.message import Message


@dataclass(frozen=True)
class SenderStats:
    """Aggregated statistics for a single sender, used in analytics views."""

    sender: str
    sender_domain: str
    message_count: int
    total_size_bytes: int


@dataclass(frozen=True)
class MessageFilter:
    """
    Parameters for filtered message queries (Search view and workflow views).

    All fields are optional — omitted fields are not applied as filters.
    Combining multiple fields produces an AND query.
    """

    sender: str | None = None
    sender_domain: str | None = None
    subject_contains: str | None = None
    label_id: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None
    is_unread: bool | None = None
    is_inbox: bool | None = None
    is_sent: bool | None = None
    is_archived: bool | None = None
    has_custom_label: bool | None = None
    limit: int = 200
    offset: int = 0


class MessageRepository:
    """
    Repository for message persistence operations.

    Accepts a SQLAlchemy ``Session`` via constructor injection.
    All methods operate within the caller's transaction — the repository
    never commits or rolls back; that is the responsibility of the caller
    (typically via the ``get_session`` context manager).

    Args:
        session: An open SQLAlchemy session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Write operations ──────────────────────────────────────────────────────

    def upsert(self, message: Message) -> None:
        """
        Insert or replace a single message record.

        On conflict (same ``id``), all fields except ``first_seen_at`` are
        updated.  ``first_seen_at`` is preserved — it records when the app
        first encountered this message, not when it was last synced.

        Args:
            message: The domain entity to persist.
        """
        import json

        now = datetime.now(tz=UTC)
        stmt = sqlite_insert(MessageORM).values(
            id=message.id,
            thread_id=message.thread_id,
            history_id=message.history_id,
            internal_date=message.internal_date,
            sender=message.sender,
            sender_domain=message.sender_domain,
            recipient=message.recipient,
            subject=message.subject,
            snippet=message.snippet,
            size_estimate=message.size_estimate,
            is_unread=message.is_unread,
            is_inbox=message.is_inbox,
            is_sent=message.is_sent,
            is_archived=message.is_archived,
            is_starred=message.is_starred,
            is_important=message.is_important,
            has_custom_label=message.has_custom_label,
            raw_label_ids=json.dumps(sorted(message.label_ids)),
            first_seen_at=message.first_seen_at or now,
            last_synced_at=message.last_synced_at or now,
        )
        # On conflict: update everything except first_seen_at
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "thread_id": stmt.excluded.thread_id,
                "history_id": stmt.excluded.history_id,
                "internal_date": stmt.excluded.internal_date,
                "sender": stmt.excluded.sender,
                "sender_domain": stmt.excluded.sender_domain,
                "recipient": stmt.excluded.recipient,
                "subject": stmt.excluded.subject,
                "snippet": stmt.excluded.snippet,
                "size_estimate": stmt.excluded.size_estimate,
                "is_unread": stmt.excluded.is_unread,
                "is_inbox": stmt.excluded.is_inbox,
                "is_sent": stmt.excluded.is_sent,
                "is_archived": stmt.excluded.is_archived,
                "is_starred": stmt.excluded.is_starred,
                "is_important": stmt.excluded.is_important,
                "has_custom_label": stmt.excluded.has_custom_label,
                "raw_label_ids": stmt.excluded.raw_label_ids,
                "last_synced_at": stmt.excluded.last_synced_at,
            },
        )
        self._session.execute(stmt)

    def upsert_many(self, messages: list[Message]) -> None:
        """
        Upsert a batch of messages efficiently.

        Calls ``upsert`` individually for each message.  SQLite's lack of
        true batch upsert syntax means this is the cleanest approach without
        raw SQL.  For large initial syncs, the caller should commit in
        batches (e.g. every 100 messages) rather than accumulating all
        changes in a single transaction.

        Args:
            messages: List of domain entities to upsert.
        """
        for message in messages:
            self.upsert(message)

    def update_labels(self, message_id: str, label_ids: frozenset[str]) -> None:
        """
        Update the label-derived fields for a message after a label change.

        Called by LabelService after a successful Gmail API label operation.
        Recalculates all boolean flags from the new label set and updates
        both ``raw_label_ids`` and all derived boolean columns atomically.

        Args:
            message_id: Gmail message ID to update.
            label_ids:  The complete new set of label IDs.
        """
        import json

        from domain.models.message import _SYSTEM_LABEL_IDS

        has_custom = any(
            lid not in _SYSTEM_LABEL_IDS and not lid.startswith("CATEGORY_")
            for lid in label_ids
        )
        stmt = (
            update(MessageORM)
            .where(MessageORM.id == message_id)
            .values(
                is_unread="UNREAD" in label_ids,
                is_inbox="INBOX" in label_ids,
                is_sent="SENT" in label_ids,
                is_archived=(
                    "INBOX" not in label_ids
                    and "TRASH" not in label_ids
                    and "SPAM" not in label_ids
                ),
                is_starred="STARRED" in label_ids,
                is_important="IMPORTANT" in label_ids,
                has_custom_label=has_custom,
                raw_label_ids=json.dumps(sorted(label_ids)),
                last_synced_at=datetime.now(tz=UTC),
            )
        )
        self._session.execute(stmt)

    # ── Single-record fetch ───────────────────────────────────────────────────

    def get_by_id(self, message_id: str) -> Message | None:
        """
        Fetch a single message by its Gmail message ID.

        Args:
            message_id: Gmail message ID.

        Returns:
            The domain entity, or None if not found.
        """
        row = self._session.get(MessageORM, message_id)
        return row.to_domain() if row else None

    def exists(self, message_id: str) -> bool:
        """Return True if a message with this ID is in the local database."""
        stmt = select(MessageORM.id).where(MessageORM.id == message_id)
        return self._session.execute(stmt).scalar() is not None

    # ── Inbox zero workflow ───────────────────────────────────────────────────

    def list_inbox(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        oldest_first: bool = True,
    ) -> list[Message]:
        """
        Return all messages currently in the inbox.

        Sorted oldest-first by default (oldest messages need processing first).

        Args:
            limit:       Maximum number of messages to return.
            offset:      Pagination offset.
            oldest_first: If True, sort by internal_date ASC; else DESC.

        Returns:
            List of domain Message entities.
        """
        order = (
            MessageORM.internal_date.asc()
            if oldest_first
            else MessageORM.internal_date.desc()
        )
        stmt = (
            select(MessageORM)
            .where(MessageORM.is_inbox.is_(True))
            .order_by(order)
            .limit(limit)
            .offset(offset)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def count_inbox(self) -> int:
        """Return the total count of messages in the inbox."""
        stmt = select(func.count()).where(MessageORM.is_inbox.is_(True))
        return self._session.execute(stmt).scalar_one()

    def inbox_size_bytes(self) -> int:
        """Return total estimated size of all inbox messages in bytes."""
        stmt = select(func.coalesce(func.sum(MessageORM.size_estimate), 0)).where(
            MessageORM.is_inbox.is_(True)
        )
        return self._session.execute(stmt).scalar_one()

    # ── Archive hygiene workflow ──────────────────────────────────────────────

    def list_archive_unlabelled(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Message]:
        """
        Return archived messages that have no custom user label.

        These are the targets of the Archive Hygiene workflow.  Sorted by
        sender_domain then internal_date so the by-sender grouping in the UI
        reflects the query order.

        Args:
            limit:  Maximum number of messages to return.
            offset: Pagination offset.

        Returns:
            List of domain Message entities.
        """
        stmt = (
            select(MessageORM)
            .where(
                MessageORM.is_archived.is_(True),
                MessageORM.has_custom_label.is_(False),
            )
            .order_by(
                MessageORM.sender_domain.asc(),
                MessageORM.internal_date.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def count_archive_unlabelled(self) -> int:
        """Return total count of archived messages with no custom label."""
        stmt = select(func.count()).where(
            MessageORM.is_archived.is_(True),
            MessageORM.has_custom_label.is_(False),
        )
        return self._session.execute(stmt).scalar_one()

    # ── Sent / outbox workflow ────────────────────────────────────────────────

    def list_sent(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        oldest_first: bool = True,
    ) -> list[Message]:
        """
        Return all sent messages.

        Args:
            limit:       Maximum number of messages to return.
            offset:      Pagination offset.
            oldest_first: Sort order by internal_date.

        Returns:
            List of domain Message entities.
        """
        order = (
            MessageORM.internal_date.asc()
            if oldest_first
            else MessageORM.internal_date.desc()
        )
        stmt = (
            select(MessageORM)
            .where(MessageORM.is_sent.is_(True))
            .order_by(order)
            .limit(limit)
            .offset(offset)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def count_sent_unresolved(self) -> int:
        """
        Return the count of sent messages not marked complete or labelled.

        A sent message is "unresolved" if it has no custom label — i.e. the
        user has not applied any workflow label (Complete, Follow-Up, etc.).
        Thread-level "no reply" analysis is done in AnalyticsService.
        """
        stmt = select(func.count()).where(
            MessageORM.is_sent.is_(True),
            MessageORM.has_custom_label.is_(False),
        )
        return self._session.execute(stmt).scalar_one()

    # ── Size reduction workflow ───────────────────────────────────────────────

    def list_largest(
        self,
        *,
        limit: int = 50,
        is_inbox: bool | None = None,
        is_sent: bool | None = None,
    ) -> list[Message]:
        """
        Return messages sorted by size descending.

        Used by the Size Reduction view.  Optionally filtered to inbox or
        sent messages only.

        Args:
            limit:    Maximum number of messages to return.
            is_inbox: If True, restrict to inbox messages only.
            is_sent:  If True, restrict to sent messages only.

        Returns:
            List of domain Message entities, largest first.
        """
        conditions = []
        if is_inbox is True:
            conditions.append(MessageORM.is_inbox.is_(True))
        if is_sent is True:
            conditions.append(MessageORM.is_sent.is_(True))

        stmt = (
            select(MessageORM)
            .where(*conditions)
            .order_by(MessageORM.size_estimate.desc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def total_size_bytes(self) -> int:
        """Return total estimated size of all messages in bytes."""
        stmt = select(func.coalesce(func.sum(MessageORM.size_estimate), 0))
        return self._session.execute(stmt).scalar_one()

    # ── Analytics queries ─────────────────────────────────────────────────────

    def top_senders_by_count(self, *, limit: int = 10) -> list[SenderStats]:
        """
        Return the top senders ranked by message count.

        Args:
            limit: Number of senders to return.

        Returns:
            List of SenderStats, highest count first.
        """
        stmt = (
            select(
                MessageORM.sender,
                MessageORM.sender_domain,
                func.count().label("message_count"),
                func.coalesce(func.sum(MessageORM.size_estimate), 0).label(
                    "total_size_bytes"
                ),
            )
            .group_by(MessageORM.sender, MessageORM.sender_domain)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).all()
        return [
            SenderStats(
                sender=r.sender,
                sender_domain=r.sender_domain,
                message_count=r.message_count,
                total_size_bytes=r.total_size_bytes,
            )
            for r in rows
        ]

    def top_senders_by_size(self, *, limit: int = 10) -> list[SenderStats]:
        """
        Return the top senders ranked by total message size.

        Args:
            limit: Number of senders to return.

        Returns:
            List of SenderStats, largest total size first.
        """
        stmt = (
            select(
                MessageORM.sender,
                MessageORM.sender_domain,
                func.count().label("message_count"),
                func.coalesce(func.sum(MessageORM.size_estimate), 0).label(
                    "total_size_bytes"
                ),
            )
            .group_by(MessageORM.sender, MessageORM.sender_domain)
            .order_by(func.sum(MessageORM.size_estimate).desc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).all()
        return [
            SenderStats(
                sender=r.sender,
                sender_domain=r.sender_domain,
                message_count=r.message_count,
                total_size_bytes=r.total_size_bytes,
            )
            for r in rows
        ]

    def custom_label_coverage_pct(self) -> float:
        """
        Return the percentage of all messages that have at least one custom label.

        Returns:
            Float in range 0.0-100.0.  Returns 0.0 if there are no messages.
        """
        total_stmt = select(func.count()).select_from(MessageORM)
        total: int = self._session.execute(total_stmt).scalar_one()
        if total == 0:
            return 0.0

        labelled_stmt = select(func.count()).where(
            MessageORM.has_custom_label.is_(True)
        )
        labelled: int = self._session.execute(labelled_stmt).scalar_one()
        return round((labelled / total) * 100, 2)

    # ── Filtered search ───────────────────────────────────────────────────────

    def search(self, filters: MessageFilter) -> list[Message]:
        """
        Return messages matching the given filter parameters.

        All filter fields are combined with AND.  If a filter field is None,
        that filter is not applied.  Label filtering (``label_id``) joins
        the ``message_labels`` table.

        Args:
            filters: A MessageFilter dataclass with the desired constraints.

        Returns:
            List of domain Message entities matching all active filters.
        """
        stmt = select(MessageORM)

        if filters.sender is not None:
            stmt = stmt.where(MessageORM.sender.ilike(f"%{filters.sender}%"))
        if filters.sender_domain is not None:
            stmt = stmt.where(
                MessageORM.sender_domain.ilike(f"%{filters.sender_domain}%")
            )
        if filters.subject_contains is not None:
            stmt = stmt.where(
                MessageORM.subject.ilike(f"%{filters.subject_contains}%")
            )
        if filters.date_from is not None:
            stmt = stmt.where(MessageORM.internal_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(MessageORM.internal_date <= filters.date_to)
        if filters.min_size_bytes is not None:
            stmt = stmt.where(MessageORM.size_estimate >= filters.min_size_bytes)
        if filters.max_size_bytes is not None:
            stmt = stmt.where(MessageORM.size_estimate <= filters.max_size_bytes)
        if filters.is_unread is not None:
            stmt = stmt.where(MessageORM.is_unread.is_(filters.is_unread))
        if filters.is_inbox is not None:
            stmt = stmt.where(MessageORM.is_inbox.is_(filters.is_inbox))
        if filters.is_sent is not None:
            stmt = stmt.where(MessageORM.is_sent.is_(filters.is_sent))
        if filters.is_archived is not None:
            stmt = stmt.where(MessageORM.is_archived.is_(filters.is_archived))
        if filters.has_custom_label is not None:
            stmt = stmt.where(
                MessageORM.has_custom_label.is_(filters.has_custom_label)
            )
        if filters.label_id is not None:
            stmt = stmt.join(
                MessageLabelORM,
                MessageORM.id == MessageLabelORM.message_id,
            ).where(MessageLabelORM.label_id == filters.label_id)

        stmt = (
            stmt.order_by(MessageORM.internal_date.desc())
            .limit(filters.limit)
            .offset(filters.offset)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def count_search(self, filters: MessageFilter) -> int:
        """Return the total count matching the given filters (without pagination)."""
        stmt = select(func.count()).select_from(MessageORM)

        if filters.sender is not None:
            stmt = stmt.where(MessageORM.sender.ilike(f"%{filters.sender}%"))
        if filters.sender_domain is not None:
            stmt = stmt.where(
                MessageORM.sender_domain.ilike(f"%{filters.sender_domain}%")
            )
        if filters.subject_contains is not None:
            stmt = stmt.where(
                MessageORM.subject.ilike(f"%{filters.subject_contains}%")
            )
        if filters.date_from is not None:
            stmt = stmt.where(MessageORM.internal_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(MessageORM.internal_date <= filters.date_to)
        if filters.min_size_bytes is not None:
            stmt = stmt.where(MessageORM.size_estimate >= filters.min_size_bytes)
        if filters.max_size_bytes is not None:
            stmt = stmt.where(MessageORM.size_estimate <= filters.max_size_bytes)
        if filters.is_unread is not None:
            stmt = stmt.where(MessageORM.is_unread.is_(filters.is_unread))
        if filters.is_inbox is not None:
            stmt = stmt.where(MessageORM.is_inbox.is_(filters.is_inbox))
        if filters.is_sent is not None:
            stmt = stmt.where(MessageORM.is_sent.is_(filters.is_sent))
        if filters.is_archived is not None:
            stmt = stmt.where(MessageORM.is_archived.is_(filters.is_archived))
        if filters.has_custom_label is not None:
            stmt = stmt.where(
                MessageORM.has_custom_label.is_(filters.has_custom_label)
            )
        if filters.label_id is not None:
            stmt = stmt.join(
                MessageLabelORM,
                MessageORM.id == MessageLabelORM.message_id,
            ).where(MessageLabelORM.label_id == filters.label_id)

        return self._session.execute(stmt).scalar_one()
