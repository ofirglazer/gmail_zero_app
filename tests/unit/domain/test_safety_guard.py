"""
Unit tests for domain.safety.guard.SafetyGuard.

The safety/ suite (test_forbidden_operations.py) proves the invariants hold.
This suite tests the guard's internal behaviour in detail:
  - Correct check ordering (INBOX removal caught before generic protected check)
  - Error message quality and label_id attribute accuracy
  - Guard is stateless and reusable across calls
  - Each check is independent (a passing check does not mask a failing one)
  - Edge cases: empty label sets, single-label sets, overlapping concerns

All tests are marked @pytest.mark.unit.
"""

from __future__ import annotations

import pytest

from application.dto.label_operation import BulkLabelOperationRequest, LabelOperationRequest
from domain.exceptions import SafetyViolationError
from domain.safety.constants import (
    MAX_BULK_OPERATION_MESSAGES,
    MAX_LABELS_PER_OPERATION,
)
from domain.safety.guard import SafetyGuard


@pytest.fixture
def guard() -> SafetyGuard:
    return SafetyGuard()


# ── Archive check fires before generic protected-label check ──────────────────


@pytest.mark.unit
class TestCheckOrdering:
    """
    The INBOX-removal check must fire first and produce the most informative
    error message — before the generic protected-label check catches it.
    """

    def test_inbox_removal_error_mentions_archive(self, guard: SafetyGuard) -> None:
        """Error message must explain *why* INBOX removal is forbidden."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert "archive" in str(exc_info.value).lower()

    def test_inbox_removal_sets_label_id_to_inbox(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert exc_info.value.label_id == "INBOX"

    def test_inbox_plus_other_protected_raises_on_inbox_first(
        self, guard: SafetyGuard
    ) -> None:
        """When INBOX and SENT are both in remove set, INBOX check fires first."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX", "SENT"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        # The archive-specific message is expected (not the generic protected-label message)
        assert "archive" in str(exc_info.value).lower()
        assert exc_info.value.label_id == "INBOX"


# ── Error message quality ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestErrorMessageQuality:
    """Safety errors must be human-readable and actionable."""

    def test_protected_removal_error_names_the_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"SENT"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert "SENT" in str(exc_info.value)

    def test_protected_addition_error_names_the_label(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"TRASH"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert "TRASH" in str(exc_info.value)

    def test_bulk_limit_error_mentions_both_counts(self, guard: SafetyGuard) -> None:
        """Error must state both the attempted count and the allowed limit."""
        over_limit = MAX_BULK_OPERATION_MESSAGES + 1
        request = BulkLabelOperationRequest(
            message_ids=tuple(f"msg{i}" for i in range(over_limit)),
            add_label_ids=frozenset({"Label_user"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_bulk_label_operation(request)
        error_text = str(exc_info.value)
        assert str(over_limit) in error_text
        assert str(MAX_BULK_OPERATION_MESSAGES) in error_text

    def test_label_count_limit_error_mentions_both_counts(
        self, guard: SafetyGuard
    ) -> None:
        over_limit = MAX_LABELS_PER_OPERATION + 1
        add_ids = frozenset(f"Label_add{i}" for i in range(over_limit))
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=add_ids,
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        error_text = str(exc_info.value)
        assert str(over_limit) in error_text
        assert str(MAX_LABELS_PER_OPERATION) in error_text

    def test_multiple_protected_label_removals_all_named_in_error(
        self, guard: SafetyGuard
    ) -> None:
        """When multiple non-INBOX protected labels are in remove_set, all appear."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"SENT", "STARRED", "IMPORTANT"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        error_text = str(exc_info.value)
        # At least one of the labels is named (sorted list in error message)
        assert any(lbl in error_text for lbl in ("SENT", "STARRED", "IMPORTANT"))

    def test_safety_violation_has_non_empty_reason(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert exc_info.value.reason
        assert len(exc_info.value.reason) > 10


# ── Guard statelesness ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestGuardStatelessness:
    """SafetyGuard is stateless — repeated calls must behave identically."""

    def test_guard_blocks_consistently_across_calls(self, guard: SafetyGuard) -> None:
        bad_request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        for _ in range(5):
            with pytest.raises(SafetyViolationError):
                guard.validate_label_operation(bad_request)

    def test_guard_allows_consistently_across_calls(self, guard: SafetyGuard) -> None:
        good_request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_userDefined"}),
        )
        for _ in range(5):
            guard.validate_label_operation(good_request)  # Must not raise

    def test_failed_call_does_not_poison_subsequent_calls(
        self, guard: SafetyGuard
    ) -> None:
        bad = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        good = LabelOperationRequest(
            message_id="msg002",
            add_label_ids=frozenset({"Label_userDefined"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(bad)
        # Guard must still work correctly for subsequent valid requests
        guard.validate_label_operation(good)  # Must not raise

    def test_two_guard_instances_behave_identically(self) -> None:
        guard_a = SafetyGuard()
        guard_b = SafetyGuard()
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError):
            guard_a.validate_label_operation(request)
        with pytest.raises(SafetyViolationError):
            guard_b.validate_label_operation(request)


# ── Label ID attribute on SafetyViolationError ────────────────────────────────


@pytest.mark.unit
class TestSafetyViolationLabelId:
    """
    label_id on SafetyViolationError should identify the offending label
    where applicable, and be None for non-label-specific violations.
    """

    def test_inbox_removal_label_id_is_inbox(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert exc_info.value.label_id == "INBOX"

    def test_protected_removal_label_id_set(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"STARRED"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert exc_info.value.label_id == "STARRED"

    def test_protected_addition_label_id_set(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"SPAM"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert exc_info.value.label_id == "SPAM"

    def test_bulk_limit_label_id_is_none(self, guard: SafetyGuard) -> None:
        """Bulk message count violation is not label-specific."""
        request = BulkLabelOperationRequest(
            message_ids=tuple(
                f"msg{i}" for i in range(MAX_BULK_OPERATION_MESSAGES + 1)
            ),
            add_label_ids=frozenset({"Label_user"}),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_bulk_label_operation(request)
        assert exc_info.value.label_id is None

    def test_label_count_limit_label_id_is_none(self, guard: SafetyGuard) -> None:
        """Label count violation is not label-specific."""
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset(
                f"Label_add{i}" for i in range(MAX_LABELS_PER_OPERATION + 1)
            ),
        )
        with pytest.raises(SafetyViolationError) as exc_info:
            guard.validate_label_operation(request)
        assert exc_info.value.label_id is None


# ── Bulk vs single operation validation symmetry ──────────────────────────────


@pytest.mark.unit
class TestBulkVsSingleSymmetry:
    """
    Every label-level rule enforced on single operations must equally apply
    to bulk operations.
    """

    def test_bulk_catches_inbox_removal(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=("msg001",),
            remove_label_ids=frozenset({"INBOX"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_bulk_label_operation(request)

    def test_bulk_catches_sent_removal(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002"),
            remove_label_ids=frozenset({"SENT"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_bulk_label_operation(request)

    def test_bulk_catches_trash_addition(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"TRASH"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_bulk_label_operation(request)

    def test_bulk_catches_label_count_excess(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset(
                f"Label_add{i}" for i in range(MAX_LABELS_PER_OPERATION + 1)
            ),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_bulk_label_operation(request)

    def test_single_has_no_message_count_limit(self, guard: SafetyGuard) -> None:
        """Single LabelOperationRequest has no concept of message count limit."""
        # This must not raise — single ops target one message by definition
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_user"}),
        )
        guard.validate_label_operation(request)

    def test_bulk_allows_valid_user_label_add(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=tuple(f"msg{i}" for i in range(50)),
            add_label_ids=frozenset({"Label_ZeroAppComplete"}),
        )
        guard.validate_bulk_label_operation(request)  # Must not raise


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestEdgeCases:
    """Boundary conditions and unusual but valid inputs."""

    def test_validate_add_only_request(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_user"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_validate_remove_only_request(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_user"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_validate_add_and_remove_user_labels(self, guard: SafetyGuard) -> None:
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_complete"}),
            remove_label_ids=frozenset({"Label_needsAction"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_single_message_bulk_is_valid(self, guard: SafetyGuard) -> None:
        request = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_user"}),
        )
        guard.validate_bulk_label_operation(request)  # Must not raise

    def test_max_labels_exactly_at_limit(self, guard: SafetyGuard) -> None:
        """Exactly MAX_LABELS_PER_OPERATION changes must be permitted."""
        half = MAX_LABELS_PER_OPERATION // 2
        remainder = MAX_LABELS_PER_OPERATION - half
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset(f"Label_a{i}" for i in range(half)),
            remove_label_ids=frozenset(f"Label_r{i}" for i in range(remainder)),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_category_label_removal_blocked(self, guard: SafetyGuard) -> None:
        """CATEGORY_* labels are in PROTECTED_LABEL_IDS and must be blocked."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"CATEGORY_PROMOTIONS"}),
        )
        with pytest.raises(SafetyViolationError):
            guard.validate_label_operation(request)

    def test_to_remove_zeroapp_label_permitted(self, guard: SafetyGuard) -> None:
        """The ZeroApp/To-Remove workflow label must be addable."""
        request = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_ZeroAppToRemove"}),
        )
        guard.validate_label_operation(request)  # Must not raise

    def test_to_remove_zeroapp_label_removable(self, guard: SafetyGuard) -> None:
        """The ZeroApp/To-Remove label must also be removable (user changed mind)."""
        request = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_ZeroAppToRemove"}),
        )
        guard.validate_label_operation(request)  # Must not raise
