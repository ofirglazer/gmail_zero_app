"""
LabelRepository — persistence for labels and message-label associations.

Manages two tables:
  - ``labels``        — the canonical registry of Gmail labels
  - ``message_labels`` — normalised junction of which labels are on which messages

The junction table is always kept in sync with ``MessageORM.raw_label_ids``
(denormalised) via ``sync_message_labels``, which is called by the sync
pipeline after any label change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from infrastructure.persistence.models import LabelOperationLogORM, LabelORM, MessageLabelORM

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from domain.models.label import Label


class LabelRepository:
    """
    Repository for label and message-label persistence.

    Args:
        session: An open SQLAlchemy session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Label CRUD ────────────────────────────────────────────────────────────

    def upsert(self, label: Label) -> None:
        """
        Insert or update a label record.

        Safe to call repeatedly — updates all fields on conflict.

        Args:
            label: The domain Label entity to persist.
        """
        stmt = sqlite_insert(LabelORM).values(
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
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "name": stmt.excluded.name,
                "type": stmt.excluded.type,
                "message_list_visibility": stmt.excluded.message_list_visibility,
                "label_list_visibility": stmt.excluded.label_list_visibility,
                "synced_at": stmt.excluded.synced_at,
            },
        )
        self._session.execute(stmt)

    def upsert_many(self, labels: list[Label]) -> None:
        """Upsert a collection of labels."""
        for label in labels:
            self.upsert(label)

    def get_by_id(self, label_id: str) -> Label | None:
        """
        Fetch a single label by its Gmail label ID.

        Args:
            label_id: Gmail label ID (e.g. "INBOX", "Label_1234567890").

        Returns:
            Domain Label entity, or None if not found.
        """
        row = self._session.get(LabelORM, label_id)
        return row.to_domain() if row else None

    def get_by_name(self, name: str) -> Label | None:
        """
        Fetch a label by its exact display name.

        Args:
            name: Exact label name as shown in Gmail (case-sensitive).

        Returns:
            Domain Label entity, or None if not found.
        """
        stmt = select(LabelORM).where(LabelORM.name == name)
        row = self._session.execute(stmt).scalar_one_or_none()
        return row.to_domain() if row else None

    def list_all(self) -> list[Label]:
        """Return all labels, sorted by type (system first) then name."""
        stmt = select(LabelORM).order_by(LabelORM.type.asc(), LabelORM.name.asc())
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def list_user_labels(self) -> list[Label]:
        """Return only user-defined labels, sorted by name."""
        stmt = (
            select(LabelORM)
            .where(LabelORM.type == "user")
            .order_by(LabelORM.name.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def exists(self, label_id: str) -> bool:
        """Return True if a label with this ID exists in the local database."""
        stmt = select(LabelORM.id).where(LabelORM.id == label_id)
        return self._session.execute(stmt).scalar() is not None

    # ── Message-label junction management ─────────────────────────────────────

    def sync_message_labels(
        self, message_id: str, label_ids: frozenset[str]
    ) -> None:
        """
        Replace all label associations for a message with the given set.

        Deletes all existing rows for this message then bulk-inserts the new
        set.  This approach is used during sync when the full new label set
        is known.  It is more efficient than diffing individual changes.

        Note: Only label IDs that exist in the ``labels`` table are inserted.
        Unknown label IDs (e.g. from a partial label sync) are silently skipped
        to preserve referential integrity.

        Args:
            message_id: Gmail message ID.
            label_ids:  Complete new set of label IDs for this message.
        """
        # Delete all existing associations for this message
        del_stmt = delete(MessageLabelORM).where(
            MessageLabelORM.message_id == message_id
        )
        self._session.execute(del_stmt)

        if not label_ids:
            return

        # Filter to only label IDs that exist in the labels table
        existing_stmt = select(LabelORM.id).where(LabelORM.id.in_(label_ids))
        existing_ids: set[str] = set(self._session.execute(existing_stmt).scalars())

        now = datetime.now(tz=UTC)
        for label_id in sorted(existing_ids):  # sorted for determinism
            assoc = MessageLabelORM(
                message_id=message_id,
                label_id=label_id,
                applied_at=now,
            )
            self._session.add(assoc)

    def get_label_ids_for_message(self, message_id: str) -> frozenset[str]:
        """
        Return the set of label IDs currently associated with a message.

        Reads from the normalised junction table (not the denormalised
        raw_label_ids column on MessageORM).

        Args:
            message_id: Gmail message ID.

        Returns:
            Frozen set of label ID strings.
        """
        stmt = select(MessageLabelORM.label_id).where(
            MessageLabelORM.message_id == message_id
        )
        rows = self._session.execute(stmt).scalars().all()
        return frozenset(rows)

    def count_messages_with_label(self, label_id: str) -> int:
        """
        Return the number of messages currently carrying this label.

        Used by the Labels view to show per-label message counts.

        Args:
            label_id: Gmail label ID.

        Returns:
            Integer count.
        """
        from sqlalchemy import func

        stmt = select(func.count()).where(
            MessageLabelORM.label_id == label_id
        )
        return self._session.execute(stmt).scalar_one()

    # ── Audit log ─────────────────────────────────────────────────────────────

    def log_label_operation(
        self,
        *,
        message_id: str,
        operation: str,
        label_id: str,
        label_name: str,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        """
        Append a record to the label operations audit log.

        Called by LabelService after every label add/remove attempt,
        regardless of whether the operation succeeded.

        Args:
            message_id:    Gmail message ID.
            operation:     "add" or "remove".
            label_id:      Gmail label ID.
            label_name:    Human-readable label name (snapshot at operation time).
            success:       True if the Gmail API call succeeded.
            error_message: Error detail if success is False.
        """
        entry = LabelOperationLogORM(
            message_id=message_id,
            operation=operation,
            label_id=label_id,
            label_name=label_name,
            performed_at=datetime.now(tz=UTC),
            success=success,
            error_message=error_message,
        )
        self._session.add(entry)
