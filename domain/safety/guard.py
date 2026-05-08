"""
SafetyGuard — domain-layer safety enforcement for gmail_zero_app.

This is Layer 2 of the three-layer safety architecture:

    Layer 1: OAuth scopes   — Google rejects forbidden API calls at the network level
    Layer 2: SafetyGuard    — domain service validates every label operation (THIS FILE)
    Layer 3: GmailClient    — whitelist enforced in infrastructure (Step 4)

SafetyGuard is intentionally placed in the domain layer:
  - Zero infrastructure dependencies (no Flask, no SQLAlchemy, no Gmail API)
  - Trivially testable with no mocking required
  - Called by LabelService before any API interaction occurs
  - Cannot be accidentally bypassed by a route that skips the service layer

Design pattern — Guard / Chain of Responsibility:
    Each safety rule is an independent private ``_check_*`` method.
    ``validate_label_operation`` and ``validate_bulk_label_operation`` run
    every check in sequence.  New rules are added as new ``_check_*`` methods
    — existing checks are never modified.

⚠️  ADDING NEW CHECKS IS SAFE.  REMOVING OR WEAKENING EXISTING CHECKS IS A
    SECURITY ACTION that requires review and must be accompanied by updated
    tests in tests/safety/.
"""

from __future__ import annotations

# Type-only imports kept in TYPE_CHECKING block to preserve zero-dependency guarantee.
from typing import TYPE_CHECKING

from domain.exceptions import SafetyViolationError
from domain.safety.constants import (
    ARCHIVE_TRIGGER_LABEL_ID,
    MAX_BULK_OPERATION_MESSAGES,
    MAX_LABELS_PER_OPERATION,
    PROTECTED_ADD_LABEL_IDS,
    PROTECTED_LABEL_IDS,
)

if TYPE_CHECKING:
    from application.dto.label_operation import BulkLabelOperationRequest, LabelOperationRequest


class SafetyGuard:
    """
    Domain service that validates label operations against safety rules.

    Stateless — no constructor arguments required.  Instantiate once and
    reuse across requests (thread-safe since it holds no mutable state).

    Usage::

        guard = SafetyGuard()
        guard.validate_label_operation(request)    # raises SafetyViolationError on violation
        guard.validate_bulk_label_operation(bulk)  # raises SafetyViolationError on violation

    Both methods are all-or-nothing: they either complete without raising
    (all rules passed) or raise ``SafetyViolationError`` at the first violation.
    """

    # ── Public validation entry points ────────────────────────────────────────

    def validate_label_operation(self, request: LabelOperationRequest) -> None:
        """
        Validate a single-message label operation against all safety rules.

        Runs every check in order.  Raises at the first violation found.

        Args:
            request: The LabelOperationRequest to validate.

        Raises:
            SafetyViolationError: If any safety rule is violated.
        """
        self._check_no_archive_via_inbox_removal(request.remove_label_ids)
        self._check_no_protected_label_removal(request.remove_label_ids)
        self._check_no_protected_label_addition(request.add_label_ids)
        self._check_labels_per_operation_limit(request.total_label_count)

    def validate_bulk_label_operation(self, request: BulkLabelOperationRequest) -> None:
        """
        Validate a bulk label operation against all safety rules.

        Applies per-operation label checks plus the bulk message count limit.

        Args:
            request: The BulkLabelOperationRequest to validate.

        Raises:
            SafetyViolationError: If any safety rule is violated.
        """
        self._check_bulk_message_limit(request.message_count)
        self._check_no_archive_via_inbox_removal(request.remove_label_ids)
        self._check_no_protected_label_removal(request.remove_label_ids)
        self._check_no_protected_label_addition(request.add_label_ids)
        self._check_labels_per_operation_limit(request.total_label_count)

    # ── Individual safety checks (Guard pattern) ──────────────────────────────

    def _check_no_archive_via_inbox_removal(
        self, remove_label_ids: frozenset[str]
    ) -> None:
        """
        Block any attempt to archive a message by removing the INBOX label.

        Archiving messages is explicitly out of scope for this application.
        Removing INBOX is the Gmail API mechanism for archiving; this check
        closes that specific vector.

        This check is run FIRST and SEPARATELY from the general protected-label
        check to produce the most informative error message.

        Raises:
            SafetyViolationError: If INBOX appears in remove_label_ids.
        """
        if ARCHIVE_TRIGGER_LABEL_ID in remove_label_ids:
            raise SafetyViolationError(
                reason=(
                    "Removing the INBOX label archives the message, which is a "
                    "forbidden operation. This application does not support archiving. "
                    "Use Gmail directly if you need to archive messages."
                ),
                label_id=ARCHIVE_TRIGGER_LABEL_ID,
            )

    def _check_no_protected_label_removal(
        self, remove_label_ids: frozenset[str]
    ) -> None:
        """
        Block removal of any Gmail system label.

        System labels (INBOX, SENT, DRAFT, TRASH, SPAM, STARRED, IMPORTANT,
        UNREAD, and all CATEGORY_* labels) define Gmail's internal message
        categorisation.  Removing them would corrupt the mailbox state and,
        in the case of INBOX, perform an archive operation.

        Raises:
            SafetyViolationError: If any protected label ID is in remove_label_ids.
        """
        violations = remove_label_ids & PROTECTED_LABEL_IDS
        if violations:
            # Sort for deterministic error messages in tests.
            sorted_violations = sorted(violations)
            raise SafetyViolationError(
                reason=(
                    f"Attempted to remove protected system label(s): {sorted_violations}. "
                    "System labels cannot be removed by this application."
                ),
                label_id=sorted_violations[0],
            )

    def _check_no_protected_label_addition(
        self, add_label_ids: frozenset[str]
    ) -> None:
        """
        Block addition of system labels that must not be user-assigned.

        TRASH and SPAM must only be assigned by Gmail's internal spam/trash
        mechanism — user-initiated assignment via the API is a form of
        modification this application does not support.  DRAFT and SENT are
        internal state markers that should not be manually applied.

        Raises:
            SafetyViolationError: If any protected-add label ID is in add_label_ids.
        """
        violations = add_label_ids & PROTECTED_ADD_LABEL_IDS
        if violations:
            sorted_violations = sorted(violations)
            raise SafetyViolationError(
                reason=(
                    f"Attempted to add protected system label(s): {sorted_violations}. "
                    "These labels cannot be added by this application."
                ),
                label_id=sorted_violations[0],
            )

    def _check_bulk_message_limit(self, message_count: int) -> None:
        """
        Enforce the maximum number of messages in a single bulk operation.

        Limits the blast radius of an accidental or malicious bulk label
        operation.  The limit is defined in domain.safety.constants and
        intentionally conservative.

        Args:
            message_count: Number of messages in the bulk operation.

        Raises:
            SafetyViolationError: If message_count exceeds MAX_BULK_OPERATION_MESSAGES.
        """
        if message_count > MAX_BULK_OPERATION_MESSAGES:
            raise SafetyViolationError(
                reason=(
                    f"Bulk operation targets {message_count} messages, which exceeds "
                    f"the maximum of {MAX_BULK_OPERATION_MESSAGES}. "
                    "Split the operation into smaller batches."
                ),
            )

    def _check_labels_per_operation_limit(self, total_label_count: int) -> None:
        """
        Enforce the maximum number of label changes per operation.

        Prevents accidentally constructing an operation that applies an
        unreasonable number of label changes at once.

        Args:
            total_label_count: Sum of add and remove label IDs.

        Raises:
            SafetyViolationError: If total_label_count exceeds MAX_LABELS_PER_OPERATION.
        """
        if total_label_count > MAX_LABELS_PER_OPERATION:
            raise SafetyViolationError(
                reason=(
                    f"Operation specifies {total_label_count} label changes, which exceeds "
                    f"the maximum of {MAX_LABELS_PER_OPERATION} per operation."
                ),
            )
