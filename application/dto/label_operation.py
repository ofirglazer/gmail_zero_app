"""
Data Transfer Objects for label operations in gmail_zero_app.

These DTOs carry operation parameters from the presentation layer to the
application layer (LabelService).  They are plain frozen dataclasses with
basic self-validation — they do not perform safety checks (that is the
SafetyGuard's responsibility).

Design note:
    DTOs are deliberately anemic — no business logic.  They exist solely
    to make the contract between layers explicit and type-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LabelOperationRequest:
    """
    Parameters for adding or removing labels on a single Gmail message.

    At least one of add_label_ids or remove_label_ids must be non-empty.
    The same label ID must not appear in both sets (would be a no-op and
    indicates a logic error in the caller).

    Attributes:
        message_id:      Gmail message ID to operate on.
        add_label_ids:   Frozen set of label IDs to add.  Empty set = no additions.
        remove_label_ids: Frozen set of label IDs to remove.  Empty = no removals.

    Raises:
        ValueError: On construction if the request is structurally invalid
                    (empty operation or overlapping add/remove sets).
    """

    message_id: str
    add_label_ids: frozenset[str] = field(default_factory=frozenset)
    remove_label_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id must not be empty.")

        if not self.add_label_ids and not self.remove_label_ids:
            raise ValueError(
                "LabelOperationRequest must specify at least one label to add or remove."
            )

        overlap = self.add_label_ids & self.remove_label_ids
        if overlap:
            raise ValueError(
                f"The same label IDs appear in both add and remove sets: {overlap}. "
                "This is a no-op and indicates a logic error."
            )

    @property
    def is_add_only(self) -> bool:
        """True if this request only adds labels (no removals)."""
        return bool(self.add_label_ids) and not self.remove_label_ids

    @property
    def is_remove_only(self) -> bool:
        """True if this request only removes labels (no additions)."""
        return bool(self.remove_label_ids) and not self.add_label_ids

    @property
    def total_label_count(self) -> int:
        """Total number of label operations (additions + removals)."""
        return len(self.add_label_ids) + len(self.remove_label_ids)

    def __str__(self) -> str:
        parts = []
        if self.add_label_ids:
            parts.append(f"add={set(self.add_label_ids)}")
        if self.remove_label_ids:
            parts.append(f"remove={set(self.remove_label_ids)}")
        return f"LabelOperationRequest(message={self.message_id!r}, {', '.join(parts)})"


@dataclass(frozen=True)
class BulkLabelOperationRequest:
    """
    Parameters for applying the same label operation to multiple messages.

    Used by the bulk action bar in the UI (checkbox selection + label apply).
    The SafetyGuard validates message count and label count limits before
    any API call is made.

    Attributes:
        message_ids:     Ordered tuple of Gmail message IDs to operate on.
                         Tuple (not list) enforces immutability.
        add_label_ids:   Labels to add to all messages.
        remove_label_ids: Labels to remove from all messages.

    Raises:
        ValueError: On construction if structurally invalid.
    """

    message_ids: tuple[str, ...]
    add_label_ids: frozenset[str] = field(default_factory=frozenset)
    remove_label_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.message_ids:
            raise ValueError("BulkLabelOperationRequest requires at least one message_id.")

        if not self.add_label_ids and not self.remove_label_ids:
            raise ValueError(
                "BulkLabelOperationRequest must specify at least one label to add or remove."
            )

        overlap = self.add_label_ids & self.remove_label_ids
        if overlap:
            raise ValueError(f"The same label IDs appear in both add and remove sets: {overlap}.")

    @property
    def message_count(self) -> int:
        """Number of messages in this bulk operation."""
        return len(self.message_ids)

    @property
    def total_label_count(self) -> int:
        """Total number of distinct label IDs involved."""
        return len(self.add_label_ids) + len(self.remove_label_ids)

    def to_individual_requests(self) -> list[LabelOperationRequest]:
        """
        Expand this bulk request into individual per-message requests.

        Used by the LabelService when processing messages one at a time.
        """
        return [
            LabelOperationRequest(
                message_id=mid,
                add_label_ids=self.add_label_ids,
                remove_label_ids=self.remove_label_ids,
            )
            for mid in self.message_ids
        ]

    def __str__(self) -> str:
        parts = []
        if self.add_label_ids:
            parts.append(f"add={set(self.add_label_ids)}")
        if self.remove_label_ids:
            parts.append(f"remove={set(self.remove_label_ids)}")
        return f"BulkLabelOperationRequest(messages={self.message_count}, {', '.join(parts)})"
