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
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from infrastructure.persistence.models import LabelORM, MessageLabelORM, MessageORM

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
class LabelSizeStats:
    """Aggregated size statistics for a single label, used in storage charts.

    Attributes:
        label_name:        Display name of the user label.
        total_size_bytes:  Sum of size_estimate for all messages carrying this label.
        message_count:     Number of messages carrying this label.
    """

    label_name: str
    total_size_bytes: int
    message_count: int


@dataclass(frozen=True)
class AgeBuckets:
    """Message count distribution across three age categories.

    Attributes:
        current_year:       Count of messages dated in the current calendar year.
        current_year_label: Display string for the current year (e.g. "2026").
        past_year:          Count of messages dated in the previous calendar year.
        past_year_label:    Display string for the past year (e.g. "2025").
        older:              Count of messages dated before the previous calendar year.
        older_label:        Display string for the older bucket (e.g. "2024 & older").
    """

    current_year: int
    current_year_label: str
    past_year: int
    past_year_label: str
    older: int
    older_label: str


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
    sort_by: str | None = None
    sort_dir: str | None = None
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

    def _order_clauses(
        self,
        *,
        sort_by: str,
        sort_dir: str,
        default_sort_by: str,
        default_sort_dir: str,
    ) -> list[Any]:
        column_map = {
            "internal_date": MessageORM.internal_date,
            "sender": MessageORM.sender,
            "sender_domain": MessageORM.sender_domain,
            "recipient": MessageORM.recipient,
            "subject": MessageORM.subject,
            "size_estimate": MessageORM.size_estimate,
        }
        key = sort_by if sort_by in column_map else default_sort_by
        direction = sort_dir if sort_dir in {"asc", "desc"} else default_sort_dir
        column = column_map[key]
        primary = column.asc() if direction == "asc" else column.desc()

        if key == "internal_date":
            return [primary]

        secondary = (
            MessageORM.internal_date.asc()
            if direction == "asc"
            else MessageORM.internal_date.desc()
        )
        return [primary, secondary]

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
        sort_by: str | None = None,
        sort_dir: str | None = None,
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
        default_sort_dir = "asc" if oldest_first else "desc"
        clauses = self._order_clauses(
            sort_by=sort_by or "internal_date",
            sort_dir=sort_dir or default_sort_dir,
            default_sort_by="internal_date",
            default_sort_dir=default_sort_dir,
        )
        stmt = (
            select(MessageORM)
            .where(MessageORM.is_inbox.is_(True))
            .order_by(*clauses)
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
        sort_by: str | None = None,
        sort_dir: str | None = None,
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
                *self._order_clauses(
                    sort_by=sort_by or "sender_domain",
                    sort_dir=sort_dir or "asc",
                    default_sort_by="sender_domain",
                    default_sort_dir="asc",
                )
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

    # ── Label membership via denormalised raw_label_ids ───────────────────────
    #
    # These helpers resolve label membership from the ``raw_label_ids`` JSON
    # column rather than the ``message_labels`` junction.  They are immune to a
    # stale or unpopulated junction, which matters for views that must be
    # correct immediately after a sync.  Matching is exact: each label ID is
    # stored quoted (e.g. ``"Label_123"``) and Gmail IDs never contain a double
    # quote, so a quoted-substring LIKE cannot produce false positives.

    @staticmethod
    def _raw_label_pattern(label_id: str) -> str:
        """Return a SQL LIKE pattern matching ``label_id`` inside raw_label_ids."""
        return f'%"{label_id}"%'

    def list_by_raw_label(
        self,
        label_id: str,
        *,
        is_archived: bool | None = None,
        limit: int = 200,
    ) -> list[Message]:
        """Return messages whose ``raw_label_ids`` contains ``label_id``.

        Args:
            label_id:    Gmail label ID to match.
            is_archived: If set, additionally constrain on archive state.
            limit:       Maximum number of messages to return.

        Returns:
            List of matching domain Message entities.
        """
        stmt = select(MessageORM).where(
            MessageORM.raw_label_ids.like(self._raw_label_pattern(label_id))
        )
        if is_archived is not None:
            stmt = stmt.where(MessageORM.is_archived.is_(is_archived))
        stmt = stmt.limit(limit)
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def count_by_raw_label(
        self,
        label_id: str,
        *,
        is_archived: bool | None = None,
    ) -> int:
        """Return the count of messages whose ``raw_label_ids`` has ``label_id``.

        Args:
            label_id:    Gmail label ID to match.
            is_archived: If set, additionally constrain on archive state.

        Returns:
            Integer count of matching messages.
        """
        stmt = select(func.count()).where(
            MessageORM.raw_label_ids.like(self._raw_label_pattern(label_id))
        )
        if is_archived is not None:
            stmt = stmt.where(MessageORM.is_archived.is_(is_archived))
        return self._session.execute(stmt).scalar_one()

    # ── Sent / outbox workflow ────────────────────────────────────────────────

    def list_sent(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        oldest_first: bool = True,
        sort_by: str | None = None,
        sort_dir: str | None = None,
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
        default_sort_dir = "asc" if oldest_first else "desc"
        clauses = self._order_clauses(
            sort_by=sort_by or "internal_date",
            sort_dir=sort_dir or default_sort_dir,
            default_sort_by="internal_date",
            default_sort_dir=default_sort_dir,
        )
        stmt = (
            select(MessageORM)
            .where(MessageORM.is_sent.is_(True))
            .order_by(*clauses)
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
        sort_by: str | None = None,
        sort_dir: str | None = None,
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

        clauses = self._order_clauses(
            sort_by=sort_by or "size_estimate",
            sort_dir=sort_dir or "desc",
            default_sort_by="size_estimate",
            default_sort_dir="desc",
        )
        stmt = (
            select(MessageORM)
            .where(*conditions)
            .order_by(*clauses)
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

    def size_by_label(self, *, limit: int = 10) -> list[LabelSizeStats]:
        """Return top user labels ranked by total attached message size.

        Each message is counted once per label it carries.  A message with
        two user labels contributes its size to both slices — useful for
        answering "which labels hold the most data?"

        Args:
            limit: Maximum number of labels to return.

        Returns:
            List of LabelSizeStats, largest total bytes first.
        """
        stmt = (
            select(
                LabelORM.name,
                func.coalesce(func.sum(MessageORM.size_estimate), 0).label(
                    "total_size_bytes"
                ),
                func.count().label("message_count"),
            )
            .select_from(LabelORM)
            .join(MessageLabelORM, LabelORM.id == MessageLabelORM.label_id)
            .join(MessageORM, MessageORM.id == MessageLabelORM.message_id)
            .where(LabelORM.type == "user")
            .group_by(LabelORM.id, LabelORM.name)
            .order_by(func.sum(MessageORM.size_estimate).desc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).all()
        return [
            LabelSizeStats(
                label_name=r.name,
                total_size_bytes=r.total_size_bytes,
                message_count=r.message_count,
            )
            for r in rows
        ]

    def messages_by_age_bucket(self) -> AgeBuckets:
        """Return message counts split across current year, past year, and older.

        Buckets are determined by calendar year of the message's internal_date
        (UTC).  No messages are double-counted — the three buckets are mutually
        exclusive and exhaustive.

        Returns:
            AgeBuckets with per-bucket counts and display labels.
        """
        now = datetime.now(tz=UTC)
        jan_current = datetime(now.year, 1, 1, tzinfo=UTC)
        jan_prev = datetime(now.year - 1, 1, 1, tzinfo=UTC)

        current: int = self._session.execute(
            select(func.count()).where(MessageORM.internal_date >= jan_current)
        ).scalar_one()
        past: int = self._session.execute(
            select(func.count()).where(
                MessageORM.internal_date >= jan_prev,
                MessageORM.internal_date < jan_current,
            )
        ).scalar_one()
        older: int = self._session.execute(
            select(func.count()).where(MessageORM.internal_date < jan_prev)
        ).scalar_one()

        return AgeBuckets(
            current_year=current,
            current_year_label=str(now.year),
            past_year=past,
            past_year_label=str(now.year - 1),
            older=older,
            older_label=f"{now.year - 2} & older",
        )

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

        sort_by = getattr(filters, "sort_by", None) if hasattr(filters, "sort_by") else None
        sort_dir = getattr(filters, "sort_dir", None) if hasattr(filters, "sort_dir") else None
        clauses = self._order_clauses(
            sort_by=sort_by or "internal_date",
            sort_dir=sort_dir or "desc",
            default_sort_by="internal_date",
            default_sort_dir="desc",
        )
        stmt = stmt.order_by(*clauses).limit(filters.limit).offset(filters.offset)
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

    def list_search(self, filters: MessageFilter) -> list[Message]:
        """Return messages matching the given filters (without pagination).

        Applies the same filtering logic as count_search but returns the
        full message entities instead of just counting them.
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

        if filters.limit is not None:
            stmt = stmt.limit(filters.limit)
        if filters.offset is not None:
            stmt = stmt.offset(filters.offset)

        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]
