"""
Message domain entity for gmail_zero_app.

Represents Gmail message metadata as a pure Python dataclass.  This entity
contains no message body, no attachments, and no raw MIME — only the fields
the application needs for its workflow and analytics goals.

Note on is_archived:
    Gmail has no explicit "archived" state.  A message is considered archived
    when it is not in INBOX, not in TRASH, and not in SPAM.  This derived
    flag is computed by the mapper and stored for query efficiency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Message:
    """
    Immutable domain entity representing the metadata of a single Gmail message.

    Frozen dataclass — all mutations (e.g. label changes after sync) produce
    new instances.  The repository upserts the new instance on every sync.

    Attributes:
        id:                Gmail message ID (immutable, assigned by Gmail).
        thread_id:         ID of the thread this message belongs to.
        history_id:        Gmail history ID at the time of last sync.
                           Used for incremental sync watermarking.
        internal_date:     UTC datetime Gmail assigned to the message.
                           Corresponds to the send/receive time.
        sender:            Full sender address, e.g. "Name <email@example.com>".
        sender_domain:     Extracted domain portion of the sender address,
                           e.g. "example.com".  Stored for domain-level analytics.
        recipient:         Primary recipient address.  May be None for drafts.
        subject:           Message subject line.  None if absent.
        snippet:           Short plaintext preview from Gmail.  None if absent.
        size_estimate:     Estimated message size in bytes (Gmail's estimate).
        is_unread:         True if the UNREAD system label is present.
        is_inbox:          True if the INBOX system label is present.
        is_sent:           True if the SENT system label is present.
        is_archived:       True if not in inbox, trash, or spam.
                           Computed by the mapper from label_ids.
        is_starred:        True if the STARRED system label is present.
        is_important:      True if the IMPORTANT system label is present.
        has_custom_label:  True if at least one user-defined label is present.
                           Stored and maintained for efficient archive hygiene queries.
        label_ids:         Immutable set of all Gmail label IDs on this message.
        first_seen_at:     UTC datetime this message was first persisted locally.
        last_synced_at:    UTC datetime of the most recent sync of this message.
    """

    id: str
    thread_id: str
    history_id: str
    internal_date: datetime
    sender: str
    sender_domain: str
    recipient: str | None
    subject: str | None
    snippet: str | None
    size_estimate: int
    is_unread: bool
    is_inbox: bool
    is_sent: bool
    is_archived: bool
    is_starred: bool
    is_important: bool
    has_custom_label: bool
    label_ids: frozenset[str]
    first_seen_at: datetime
    last_synced_at: datetime

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def size_in_mb(self) -> float:
        """Message size in megabytes, rounded to two decimal places."""
        return round(self.size_estimate / (1024 * 1024), 2)

    @property
    def age_days(self) -> int:
        """
        Number of whole days elapsed since internal_date.

        Uses UTC now so the result is timezone-consistent regardless of
        where the application is running.
        """
        now = datetime.now(tz=UTC)
        # Ensure internal_date is timezone-aware for subtraction
        internal = (
            self.internal_date
            if self.internal_date.tzinfo is not None
            else self.internal_date.replace(tzinfo=UTC)
        )
        delta = now - internal
        return max(0, delta.days)

    @property
    def is_large(self) -> bool:
        """
        Rough size-based flag — True if message exceeds 5 MB.

        The authoritative threshold is the configured setting
        (GMAIL_ZERO_LARGE_MESSAGE_THRESHOLD_BYTES).  This property uses a
        hardcoded 5 MB default for convenience in domain logic that does not
        have access to settings.  The Size Reduction workflow uses the
        configured threshold from Settings.
        """
        return self.size_estimate >= 5 * 1024 * 1024

    @property
    def needs_label(self) -> bool:
        """
        True if this message is a candidate for the Archive Hygiene workflow.

        A message needs labelling if it is archived (not in inbox) and has no
        user-defined custom label.
        """
        return self.is_archived and not self.has_custom_label

    @property
    def is_sent_awaiting_reply(self) -> bool:
        """
        True if this is a sent message that is the most recent in its thread.

        NOTE: This property is a per-message approximation only.  The
        authoritative "sent awaiting reply" state requires thread-level analysis
        (is the last message in the thread from the user?).  That analysis lives
        in AnalyticsService.  This flag is included for convenience in
        single-message contexts.
        """
        return self.is_sent

    def with_labels(self, label_ids: frozenset[str]) -> Message:
        """
        Return a new Message with an updated label set.

        Used by the sync pipeline when label changes are received via the
        History API without a full message re-fetch.

        Args:
            label_ids: The complete new set of label IDs for this message.

        Returns:
            A new frozen Message instance with updated label-derived fields.
        """
        has_custom = any(
            lid not in _SYSTEM_LABEL_IDS and not lid.startswith("CATEGORY_") for lid in label_ids
        )
        return Message(
            id=self.id,
            thread_id=self.thread_id,
            history_id=self.history_id,
            internal_date=self.internal_date,
            sender=self.sender,
            sender_domain=self.sender_domain,
            recipient=self.recipient,
            subject=self.subject,
            snippet=self.snippet,
            size_estimate=self.size_estimate,
            is_unread="UNREAD" in label_ids,
            is_inbox="INBOX" in label_ids,
            is_sent="SENT" in label_ids,
            is_archived=(
                "INBOX" not in label_ids and "TRASH" not in label_ids and "SPAM" not in label_ids
            ),
            is_starred="STARRED" in label_ids,
            is_important="IMPORTANT" in label_ids,
            has_custom_label=has_custom,
            label_ids=label_ids,
            first_seen_at=self.first_seen_at,
            last_synced_at=self.last_synced_at,
        )

    def __str__(self) -> str:
        location = "inbox" if self.is_inbox else "sent" if self.is_sent else "archived"
        return (
            f"Message(id={self.id!r}, from={self.sender!r}, "
            f"subject={self.subject!r}, location={location}, "
            f"size={self.size_in_mb}MB)"
        )


# ── Module-level constants ────────────────────────────────────────────────────

# System label IDs used in with_labels() to determine has_custom_label.
# Kept module-private — external code should use domain.safety.constants.
_SYSTEM_LABEL_IDS: frozenset[str] = frozenset(
    {
        "INBOX", "SENT", "DRAFT", "TRASH", "SPAM",
        "STARRED", "IMPORTANT", "UNREAD",
        "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES", "CATEGORY_FORUMS",
    }
)


# ── Factory helpers ───────────────────────────────────────────────────────────

def make_message(
    *,
    id: str,
    thread_id: str,
    history_id: str,
    internal_date: datetime,
    sender: str,
    sender_domain: str,
    recipient: str | None = None,
    subject: str | None = None,
    snippet: str | None = None,
    size_estimate: int = 0,
    label_ids: frozenset[str] | None = None,
    first_seen_at: datetime | None = None,
    last_synced_at: datetime | None = None,
) -> Message:
    """
    Convenience factory for constructing a Message from raw fields.

    Derives is_unread, is_inbox, is_sent, is_archived, is_starred,
    is_important, and has_custom_label from the provided label_ids set.
    This ensures all derived booleans stay consistent with label_ids.

    Args:
        id:            Gmail message ID.
        thread_id:     Gmail thread ID.
        history_id:    Gmail history ID at time of sync.
        internal_date: UTC message date/time.
        sender:        Full sender address string.
        sender_domain: Domain portion of sender address.
        recipient:     Primary recipient address, or None.
        subject:       Subject line, or None.
        snippet:       Short text preview, or None.
        size_estimate: Estimated size in bytes.
        label_ids:     Complete set of Gmail label IDs.  Defaults to empty set.
        first_seen_at: When this message was first stored locally.
                       Defaults to UTC now.
        last_synced_at: When this message was last synced.
                        Defaults to UTC now.

    Returns:
        A fully constructed, frozen Message entity.
    """
    now = datetime.now(tz=UTC)
    ids: frozenset[str] = label_ids if label_ids is not None else frozenset()

    has_custom = any(
        lid not in _SYSTEM_LABEL_IDS and not lid.startswith("CATEGORY_") for lid in ids
    )

    return Message(
        id=id,
        thread_id=thread_id,
        history_id=history_id,
        internal_date=internal_date,
        sender=sender,
        sender_domain=sender_domain,
        recipient=recipient,
        subject=subject,
        snippet=snippet,
        size_estimate=size_estimate,
        is_unread="UNREAD" in ids,
        is_inbox="INBOX" in ids,
        is_sent="SENT" in ids,
        is_archived=("INBOX" not in ids and "TRASH" not in ids and "SPAM" not in ids),
        is_starred="STARRED" in ids,
        is_important="IMPORTANT" in ids,
        has_custom_label=has_custom,
        label_ids=ids,
        first_seen_at=first_seen_at or now,
        last_synced_at=last_synced_at or now,
    )
