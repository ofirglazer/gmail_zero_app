"""
Label domain entity for gmail_zero_app.

Represents a Gmail label as a pure Python dataclass with no infrastructure
dependencies.  Both system labels (INBOX, SENT, etc.) and user-defined labels
are represented by this entity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class LabelType(StrEnum):
    """
    Discriminates between Gmail's built-in system labels and user-created labels.

    SYSTEM labels (INBOX, SENT, TRASH, etc.) have fixed IDs and cannot be
    renamed or deleted.  USER labels are created by the account holder and
    include all ZeroApp/* labels managed by this application.
    """

    SYSTEM = "system"
    USER = "user"


class MessageListVisibility(StrEnum):
    """Controls whether this label appears in the message list view."""

    SHOW = "show"
    HIDE = "hide"


class LabelListVisibility(StrEnum):
    """Controls whether this label appears in the label list / sidebar."""

    LABEL_SHOW = "labelShow"
    LABEL_SHOW_IF_UNREAD = "labelShowIfUnread"
    LABEL_HIDE = "labelHide"


@dataclass(frozen=True)
class Label:
    """
    Immutable domain entity representing a single Gmail label.

    Frozen dataclass — all mutations produce new instances.  The repository
    layer is responsible for persisting changes.

    Attributes:
        id:                       Gmail's label ID string (e.g. "INBOX",
                                  "Label_1234567890123456789").
        name:                     Human-readable label name as shown in Gmail.
        label_type:               SYSTEM or USER.
        message_list_visibility:  How this label is displayed in message list.
                                  None if not set by Gmail.
        label_list_visibility:    How this label appears in the label sidebar.
                                  None if not set by Gmail.
        synced_at:                UTC timestamp of last sync from Gmail API.
    """

    id: str
    name: str
    label_type: LabelType
    message_list_visibility: MessageListVisibility | None
    label_list_visibility: LabelListVisibility | None
    synced_at: datetime

    @property
    def is_system(self) -> bool:
        """True if this is a Gmail system label (cannot be deleted or renamed)."""
        return self.label_type == LabelType.SYSTEM

    @property
    def is_user(self) -> bool:
        """True if this label was created by the user (or by this application)."""
        return self.label_type == LabelType.USER

    @property
    def is_app_managed(self) -> bool:
        """
        True if this label was created by gmail_zero_app.

        Detected by the ZeroApp/ prefix convention defined in labels.toml.
        This is a heuristic — the authoritative source is the LabelConfigService.
        """
        return self.name.startswith("ZeroApp/")

    def __str__(self) -> str:
        return f"Label(id={self.id!r}, name={self.name!r}, type={self.label_type})"
