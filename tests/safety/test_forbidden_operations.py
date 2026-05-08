"""
Mandatory safety test suite for gmail_zero_app.

These tests prove the application CANNOT perform forbidden Gmail operations.
They must ALWAYS pass.  A failure here means the safety architecture has
been compromised and must be treated as a security incident, not a bug.

Coverage in this file (Step 2 — SafetyGuard layer):
    - Cannot archive a message (via INBOX label removal)
    - Cannot remove any Gmail system label
    - Cannot add TRASH or SPAM labels
    - Cannot exceed bulk operation limits
    - SafetyViolationError is always raised, never swallowed

Step 4 will extend this suite with GmailClient whitelist proofs:
    - GmailClient has no send/delete/trash/draft methods
    - GmailClient raises ForbiddenOperationError for all blocked API operations

All tests are marked with both @pytest.mark.safety and @pytest.mark.unit.
"""

import pytest

from application.dto.label_operation import BulkLabelOperationRequest, LabelOperationRequest
from domain.exceptions import SafetyViolationError
from domain.safety.constants import (
    ARCHIVE_TRIGGER_LABEL_ID,
    FORBIDDEN_API_OPERATIONS,
    MAX_BULK_OPERATION_MESSAGES,
    MAX_LABELS_PER_OPERATION,
    PROTECTED_ADD_LABEL_IDS,
    PROTECTED_LABEL_IDS,
)
from domain.safety.guard import SafetyGuard


@pytest.fixture
def guard() -> SafetyGuard:
    """A SafetyGuard instance shared across tests in this module."""
    return SafetyGuard()


# ── Archive prevention ────────────────────────────────────────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestCannotArchiveMessages:
    """
    Prove that archiving a message via INBOX label removal is impossible.

    This is the most critical safety check — removing INBOX from a message
    is exactly what the Gmail API does when archiving.
    """

    def test_cannot_remove_inbox_label_single(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert "INBOX" in str(exc_info.value)
        assert exc_info.value.label_id == ARCHIVE_TRIGGER_LABEL_ID

    def test_cannot_remove_inbox_label_bulk(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002", "msg003"),
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_bulk_label_operation(request)
        assert "INBOX" in str(exc_info.value)

    def test_cannot_remove_inbox_combined_with_other_labels(
        self, guard: SafetyGuard
    ) -> None:
        """Mixing INBOX with other removes must still be blocked."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX", "Label_userDefined"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_archive_trigger_constant_is_inbox(self) -> None:
        """The archive trigger constant must always be INBOX — never change this."""
        assert ARCHIVE_TRIGGER_LABEL_ID == "INBOX"

    def test_archive_trigger_is_in_protected_set(self) -> None:
        """INBOX must be in PROTECTED_LABEL_IDS — double-layer protection."""
        assert ARCHIVE_TRIGGER_LABEL_ID in PROTECTED_LABEL_IDS


# ── System label removal prevention ──────────────────────────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestCannotRemoveSystemLabels:
    """Prove that no Gmail system label can be removed by this application."""

    @pytest.mark.parametrize("label_id", sorted(PROTECTED_LABEL_IDS))
    def test_cannot_remove_protected_label(
        self, guard: SafetyGuard, label_id: str
    ) -> None:
        """Every protected label must be individually blocked."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({label_id}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_sent_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"SENT"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_unread_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"UNREAD"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_important_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"IMPORTANT"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_starred_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"STARRED"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_trash_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"TRASH"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_spam_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"SPAM"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_remove_draft_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"DRAFT"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    @pytest.mark.parametrize("label_id", sorted(PROTECTED_LABEL_IDS))
    def test_cannot_remove_protected_label_in_bulk(
        self, guard: SafetyGuard, label_id: str
    ) -> None:
        """Bulk operations must be equally blocked from removing system labels."""
        request = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002"),
            remove_label_ids=frozenset({label_id}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_bulk_label_operation(request)


# ── System label addition prevention ─────────────────────────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestCannotAddForbiddenSystemLabels:
    """Prove that TRASH and SPAM cannot be added via this application."""

    @pytest.mark.parametrize("label_id", sorted(PROTECTED_ADD_LABEL_IDS))
    def test_cannot_add_protected_label(
        self, guard: SafetyGuard, label_id: str
    ) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({label_id}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_add_trash_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"TRASH"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_add_spam_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"SPAM"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_add_draft_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"DRAFT"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_cannot_add_sent_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"SENT"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)


# ── Bulk operation limit enforcement ─────────────────────────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestBulkOperationLimits:
    """Prove that bulk operations cannot exceed defined safety limits."""

    def test_bulk_at_exact_limit_is_allowed(self, guard: SafetyGuard) -> None:
        """Operations at exactly the limit must be permitted."""
        message_ids = tuple(f"msg{i:04d}" for i in range(MAX_BULK_OPERATION_MESSAGES))
        request = BulkLabelOperationRequest(
            message_ids=message_ids,
            add_label_ids=frozenset({"Label_user123"}),
        )
        # Must not raise
        guard.validate_bulk_label_operation(request)

    def test_bulk_exceeding_limit_is_blocked(self, guard: SafetyGuard) -> None:
        """Operations exceeding the limit must be rejected."""
        message_ids = tuple(
            f"msg{i:04d}" for i in range(MAX_BULK_OPERATION_MESSAGES + 1)
        )
        request = BulkLabelOperationRequest(
            message_ids=message_ids,
            add_label_ids=frozenset({"Label_user123"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_bulk_label_operation(request)
        assert str(MAX_BULK_OPERATION_MESSAGES) in str(exc_info.value)

    def test_labels_at_exact_limit_are_allowed(self, guard: SafetyGuard) -> None:
        """Operations with exactly MAX_LABELS_PER_OPERATION labels must be permitted."""
        # Half add, half remove — all user labels
        add_ids = frozenset(f"Label_add{i}" for i in range(MAX_LABELS_PER_OPERATION // 2))
        remove_ids = frozenset(
            f"Label_rem{i}"
            for i in range(MAX_LABELS_PER_OPERATION - len(add_ids))
        )
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=add_ids,
            remove_label_ids=remove_ids,
        )
        # Must not raise
        guard.validate_label_operation(request)

    def test_labels_exceeding_limit_are_blocked(self, guard: SafetyGuard) -> None:
        """Operations with too many label changes must be rejected."""
        add_ids = frozenset(f"Label_add{i}" for i in range(MAX_LABELS_PER_OPERATION + 1))
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=add_ids,
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)


# ── Permitted operations (regression guard) ───────────────────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestPermittedOperations:
    """
    Verify that legitimate label operations are NOT blocked.

    These tests guard against an overly aggressive SafetyGuard that would
    reject valid user-label operations and make the app unusable.
    """

    def test_can_add_user_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_userDefined123"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_can_remove_user_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_userDefined123"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_can_add_zeroapp_label(self, guard: SafetyGuard) -> None:
        """ZeroApp/* labels (app-managed) must be addable."""
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_ZeroAppNeedsAction"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_can_add_to_remove_label(self, guard: SafetyGuard) -> None:
        """The To-Remove workflow label must be addable — it is user-defined."""
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_ZeroAppToRemove"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_can_add_and_remove_user_labels_simultaneously(
        self, guard: SafetyGuard
    ) -> None:
        """Adding one user label while removing another must be permitted."""
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_complete"}),
            remove_label_ids=frozenset({"Label_needsAction"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_can_perform_single_message_bulk(self, guard: SafetyGuard) -> None:
        """A bulk operation on a single message must be valid."""
        request = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_user123"}),
        )
        guard.validate_bulk_label_operation(request)  # Must not raise


# ── Forbidden API operations constant completeness ────────────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestForbiddenApiOperationsCompleteness:
    """
    Prove the FORBIDDEN_API_OPERATIONS set covers all required operations.

    These tests will catch future developers accidentally narrowing the
    forbidden set.
    """

    def test_send_is_in_forbidden_set(self) -> None:
        assert "users.messages.send" in FORBIDDEN_API_OPERATIONS

    def test_delete_is_in_forbidden_set(self) -> None:
        assert "users.messages.delete" in FORBIDDEN_API_OPERATIONS

    def test_trash_is_in_forbidden_set(self) -> None:
        assert "users.messages.trash" in FORBIDDEN_API_OPERATIONS

    def test_untrash_is_in_forbidden_set(self) -> None:
        assert "users.messages.untrash" in FORBIDDEN_API_OPERATIONS

    def test_draft_create_is_in_forbidden_set(self) -> None:
        assert "users.drafts.create" in FORBIDDEN_API_OPERATIONS

    def test_draft_update_is_in_forbidden_set(self) -> None:
        assert "users.drafts.update" in FORBIDDEN_API_OPERATIONS

    def test_draft_delete_is_in_forbidden_set(self) -> None:
        assert "users.drafts.delete" in FORBIDDEN_API_OPERATIONS

    def test_draft_send_is_in_forbidden_set(self) -> None:
        assert "users.drafts.send" in FORBIDDEN_API_OPERATIONS

    def test_batch_delete_is_in_forbidden_set(self) -> None:
        assert "users.messages.batchDelete" in FORBIDDEN_API_OPERATIONS

    def test_batch_modify_is_in_forbidden_set(self) -> None:
        assert "users.messages.batchModify" in FORBIDDEN_API_OPERATIONS

    def test_messages_import_is_in_forbidden_set(self) -> None:
        assert "users.messages.import" in FORBIDDEN_API_OPERATIONS

    def test_messages_insert_is_in_forbidden_set(self) -> None:
        assert "users.messages.insert" in FORBIDDEN_API_OPERATIONS

    def test_attachments_get_is_in_forbidden_set(self) -> None:
        assert "users.messages.attachments.get" in FORBIDDEN_API_OPERATIONS

    def test_forbidden_set_is_immutable(self) -> None:
        """frozenset cannot be mutated at runtime."""
        assert isinstance(FORBIDDEN_API_OPERATIONS, frozenset)

    def test_forbidden_set_is_not_empty(self) -> None:
        assert len(FORBIDDEN_API_OPERATIONS) > 0
