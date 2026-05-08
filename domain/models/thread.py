"""
Thread domain entity for gmail_zero_app.

Represents a Gmail conversation thread as a pure Python dataclass.
Thread-level data is used by the Sent Review workflow (to detect threads
where the user's sent message is the last one — indicating no reply) and
by the "Old Unresolved Threads" dashboard panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Thread:
    """
    Immutable domain entity representing a Gmail conversation thread.

    Aggregates message-level data for thread-level analytics.
    Maintained by the sync pipeline, which updates thread records whenever
    any message in the thread changes.

    Attributes:
        id:               Gmail thread ID.
        subject:          Subject of the most recent message, or None.
        message_count:    Total number of messages in the thread.
        snippet:          Short preview of the most recent message.
        last_message_at:  UTC datetime of the most recent message.
                          None if no messages have been synced yet.
        is_inbox:         True if any message in the thread is in INBOX.
        has_custom_label: True if any message in the thread has a user label.
        last_synced_at:   UTC datetime of the most recent sync of this thread.
    """

    id: str
    subject: str | None
    message_count: int
    snippet: str | None
    last_message_at: datetime | None
    is_inbox: bool
    has_custom_label: bool
    last_synced_at: datetime

    @property
    def age_days(self) -> int:
        """
        Days since the most recent message in this thread.

        Returns 0 if last_message_at is None (thread has no synced messages).
        """
        if self.last_message_at is None:
            return 0
        now = datetime.now(tz=UTC)
        anchor = (
            self.last_message_at
            if self.last_message_at.tzinfo is not None
            else self.last_message_at.replace(tzinfo=UTC)
        )
        return max(0, (now - anchor).days)

    @property
    def is_old(self) -> bool:
        """True if the thread's last message is more than 30 days old."""
        return self.age_days > 30

    @property
    def is_very_old(self) -> bool:
        """True if the thread's last message is more than 90 days old."""
        return self.age_days > 90

    @property
    def is_single_message(self) -> bool:
        """True if the thread contains exactly one message."""
        return self.message_count == 1

    def __str__(self) -> str:
        return (
            f"Thread(id={self.id!r}, messages={self.message_count}, "
            f"age={self.age_days}d, inbox={self.is_inbox})"
        )
