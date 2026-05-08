"""
Unit tests for application.dto.label_operation DTOs.

Covers:
  - Construction validation (__post_init__ guards)
  - Property derivations (is_add_only, is_remove_only, total_label_count)
  - BulkLabelOperationRequest.to_individual_requests() expansion
  - Immutability (frozen dataclasses)
  - __str__ representations
  - Edge cases: single-label sets, max-size sets
"""

from __future__ import annotations

import pytest

from application.dto.label_operation import BulkLabelOperationRequest, LabelOperationRequest

# ── LabelOperationRequest ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestLabelOperationRequestConstruction:
    """Construction validation via __post_init__."""

    def test_add_only_request_valid(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
        )
        assert req.message_id == "msg001"
        assert req.add_label_ids == frozenset({"Label_a"})
        assert req.remove_label_ids == frozenset()

    def test_remove_only_request_valid(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_a"}),
        )
        assert req.remove_label_ids == frozenset({"Label_a"})
        assert req.add_label_ids == frozenset()

    def test_add_and_remove_request_valid(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_complete"}),
            remove_label_ids=frozenset({"Label_needsAction"}),
        )
        assert "Label_complete" in req.add_label_ids
        assert "Label_needsAction" in req.remove_label_ids

    def test_empty_message_id_raises(self) -> None:
        with pytest.raises(ValueError, match="message_id"):
            LabelOperationRequest(
                message_id="",
                add_label_ids=frozenset({"Label_a"}),
            )

    def test_both_empty_sets_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            LabelOperationRequest(
                message_id="msg001",
                add_label_ids=frozenset(),
                remove_label_ids=frozenset(),
            )

    def test_overlapping_add_remove_raises(self) -> None:
        with pytest.raises(ValueError, match="same label"):
            LabelOperationRequest(
                message_id="msg001",
                add_label_ids=frozenset({"Label_a", "Label_b"}),
                remove_label_ids=frozenset({"Label_a"}),
            )

    def test_all_overlapping_raises(self) -> None:
        with pytest.raises(ValueError, match="same label"):
            LabelOperationRequest(
                message_id="msg001",
                add_label_ids=frozenset({"Label_a"}),
                remove_label_ids=frozenset({"Label_a"}),
            )

    def test_default_label_sets_are_frozensets(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
        )
        assert isinstance(req.add_label_ids, frozenset)
        assert isinstance(req.remove_label_ids, frozenset)

    def test_request_is_frozen(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
        )
        with pytest.raises(AttributeError):
            req.message_id = "mutated"  # type: ignore[misc]


@pytest.mark.unit
class TestLabelOperationRequestProperties:
    """Derived property correctness."""

    def test_is_add_only_true(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
        )
        assert req.is_add_only is True
        assert req.is_remove_only is False

    def test_is_remove_only_true(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_a"}),
        )
        assert req.is_remove_only is True
        assert req.is_add_only is False

    def test_neither_add_only_nor_remove_only_when_both_set(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
            remove_label_ids=frozenset({"Label_b"}),
        )
        assert req.is_add_only is False
        assert req.is_remove_only is False

    def test_total_label_count_add_only(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a", "Label_b", "Label_c"}),
        )
        assert req.total_label_count == 3

    def test_total_label_count_remove_only(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_x"}),
        )
        assert req.total_label_count == 1

    def test_total_label_count_both(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a", "Label_b"}),
            remove_label_ids=frozenset({"Label_c"}),
        )
        assert req.total_label_count == 3

    def test_total_label_count_zero_remove_set(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
        )
        # Only add_label_ids contributes
        assert req.total_label_count == 1


@pytest.mark.unit
class TestLabelOperationRequestStr:
    """__str__ contains key identifying information."""

    def test_str_contains_message_id(self) -> None:
        req = LabelOperationRequest(
            message_id="msg_unique_001",
            add_label_ids=frozenset({"Label_a"}),
        )
        assert "msg_unique_001" in str(req)

    def test_str_contains_add_labels(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_complete"}),
        )
        assert "Label_complete" in str(req)

    def test_str_contains_remove_labels(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            remove_label_ids=frozenset({"Label_needsAction"}),
        )
        assert "Label_needsAction" in str(req)

    def test_str_non_empty(self) -> None:
        req = LabelOperationRequest(
            message_id="msg001",
            add_label_ids=frozenset({"Label_a"}),
        )
        assert str(req)


# ── BulkLabelOperationRequest ─────────────────────────────────────────────────


@pytest.mark.unit
class TestBulkLabelOperationRequestConstruction:
    """Construction validation for bulk requests."""

    def test_valid_bulk_request(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002", "msg003"),
            add_label_ids=frozenset({"Label_newsletter"}),
        )
        assert req.message_count == 3

    def test_empty_message_ids_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one message"):
            BulkLabelOperationRequest(
                message_ids=(),
                add_label_ids=frozenset({"Label_a"}),
            )

    def test_empty_label_sets_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one label"):
            BulkLabelOperationRequest(
                message_ids=("msg001",),
                add_label_ids=frozenset(),
                remove_label_ids=frozenset(),
            )

    def test_overlapping_add_remove_raises(self) -> None:
        with pytest.raises(ValueError, match="same label"):
            BulkLabelOperationRequest(
                message_ids=("msg001",),
                add_label_ids=frozenset({"Label_a"}),
                remove_label_ids=frozenset({"Label_a"}),
            )

    def test_single_message_bulk_valid(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_a"}),
        )
        assert req.message_count == 1

    def test_message_ids_stored_as_tuple(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002"),
            add_label_ids=frozenset({"Label_a"}),
        )
        assert isinstance(req.message_ids, tuple)

    def test_request_is_frozen(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_a"}),
        )
        with pytest.raises(AttributeError):
            req.message_ids = ("mutated",)  # type: ignore[misc]


@pytest.mark.unit
class TestBulkLabelOperationRequestProperties:
    """Derived property correctness for bulk requests."""

    def test_message_count(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=tuple(f"msg{i}" for i in range(42)),
            add_label_ids=frozenset({"Label_a"}),
        )
        assert req.message_count == 42

    def test_total_label_count_add_only(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_a", "Label_b"}),
        )
        assert req.total_label_count == 2

    def test_total_label_count_remove_only(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            remove_label_ids=frozenset({"Label_x", "Label_y", "Label_z"}),
        )
        assert req.total_label_count == 3

    def test_total_label_count_both(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_a"}),
            remove_label_ids=frozenset({"Label_b"}),
        )
        assert req.total_label_count == 2


@pytest.mark.unit
class TestBulkToIndividualExpansion:
    """to_individual_requests() must produce one request per message."""

    def test_expansion_count_matches_message_count(self) -> None:
        message_ids = ("msg001", "msg002", "msg003")
        bulk = BulkLabelOperationRequest(
            message_ids=message_ids,
            add_label_ids=frozenset({"Label_complete"}),
        )
        individual = bulk.to_individual_requests()
        assert len(individual) == 3

    def test_expansion_preserves_message_ids(self) -> None:
        message_ids = ("msg_alpha", "msg_beta", "msg_gamma")
        bulk = BulkLabelOperationRequest(
            message_ids=message_ids,
            add_label_ids=frozenset({"Label_a"}),
        )
        individual = bulk.to_individual_requests()
        expanded_ids = {req.message_id for req in individual}
        assert expanded_ids == set(message_ids)

    def test_expansion_preserves_label_ids(self) -> None:
        add_ids = frozenset({"Label_a", "Label_b"})
        remove_ids = frozenset({"Label_c"})
        bulk = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002"),
            add_label_ids=add_ids,
            remove_label_ids=remove_ids,
        )
        individual = bulk.to_individual_requests()
        for req in individual:
            assert req.add_label_ids == add_ids
            assert req.remove_label_ids == remove_ids

    def test_expansion_preserves_order(self) -> None:
        message_ids = tuple(f"msg{i:03d}" for i in range(10))
        bulk = BulkLabelOperationRequest(
            message_ids=message_ids,
            add_label_ids=frozenset({"Label_a"}),
        )
        individual = bulk.to_individual_requests()
        for original, expanded in zip(message_ids, individual, strict=False):
            assert expanded.message_id == original

    def test_expansion_produces_valid_requests(self) -> None:
        """Each expanded request must itself be a valid LabelOperationRequest."""
        bulk = BulkLabelOperationRequest(
            message_ids=("msg001", "msg002"),
            add_label_ids=frozenset({"Label_a"}),
            remove_label_ids=frozenset({"Label_b"}),
        )
        for req in bulk.to_individual_requests():
            assert isinstance(req, LabelOperationRequest)
            assert req.message_id
            assert not req.add_label_ids & req.remove_label_ids  # No overlap

    def test_single_message_expansion(self) -> None:
        bulk = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_a"}),
        )
        individual = bulk.to_individual_requests()
        assert len(individual) == 1
        assert individual[0].message_id == "msg001"


@pytest.mark.unit
class TestBulkLabelOperationRequestStr:
    """__str__ contains key identifying information."""

    def test_str_contains_message_count(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=tuple(f"msg{i}" for i in range(7)),
            add_label_ids=frozenset({"Label_a"}),
        )
        assert "7" in str(req)

    def test_str_contains_add_label(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_newsletter"}),
        )
        assert "Label_newsletter" in str(req)

    def test_str_non_empty(self) -> None:
        req = BulkLabelOperationRequest(
            message_ids=("msg001",),
            add_label_ids=frozenset({"Label_a"}),
        )
        assert str(req)
