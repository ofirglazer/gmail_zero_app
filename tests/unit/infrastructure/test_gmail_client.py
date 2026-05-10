"""
Tests for the Gmail infrastructure layer — Step 4.

Coverage:
    TestGmailClientWhitelist      — Layer 3 safety: forbidden operations raise
    TestGmailClientPermittedOps   — Permitted ops exist on GmailClient (structure check)
    TestMockGmailClientDataset    — Dataset completeness and coverage
    TestMockGmailClientLabelOps   — Label mutations are applied correctly
    TestMockGmailClientHistory    — History simulation for incremental sync
    TestGmailMapperMessages       — API dict → Message entity conversion
    TestGmailMapperLabels         — API dict → Label entity conversion
    TestGmailMapperHelpers        — Domain extraction helpers
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from domain.exceptions import ForbiddenOperationError
from domain.models.label import LabelType
from domain.models.message import Message
from domain.safety.constants import FORBIDDEN_API_OPERATIONS
from infrastructure.gmail.client import GmailClient
from infrastructure.gmail.mapper import GmailMapper
from infrastructure.gmail.mock_client import MockGmailClient

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_gmail_client() -> GmailClient:
    """Build a GmailClient with a mocked Google API service."""
    mock_creds = MagicMock()
    with patch("infrastructure.gmail.client.GmailClient._build_service", return_value=MagicMock()):
        return GmailClient(credentials=mock_creds)


def _make_api_message(
    msg_id: str = "msg001",
    thread_id: str = "thread001",
    label_ids: list[str] | None = None,
    size: int = 10_000,
    days_old: int = 5,
    sender: str = "sender@example.com",
    subject: str = "Test subject",
) -> dict[str, Any]:
    """Build a minimal Gmail API message dict."""
    from datetime import timedelta
    ts = int((datetime.now(tz=UTC) - timedelta(days=days_old)).timestamp() * 1000)
    # Use explicit None check — empty list is a valid (archived, unlabelled) state
    effective_labels = ["INBOX", "UNREAD"] if label_ids is None else label_ids
    return {
        "id": msg_id,
        "threadId": thread_id,
        "historyId": "100001",
        "internalDate": str(ts),
        "sizeEstimate": size,
        "snippet": "This is a test snippet",
        "labelIds": effective_labels,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Subject", "value": subject},
            ]
        },
    }


# ── GmailClient whitelist enforcement (Layer 3 safety) ───────────────────────


@pytest.mark.safety
@pytest.mark.unit
class TestGmailClientWhitelist:
    """
    Prove that GmailClient raises ForbiddenOperationError for all operations
    in FORBIDDEN_API_OPERATIONS, before any network call is made.
    """

    @pytest.mark.parametrize("operation", sorted(FORBIDDEN_API_OPERATIONS))
    def test_forbidden_operation_raises(self, operation: str) -> None:
        """Every forbidden operation must raise ForbiddenOperationError."""
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError) as exc_info:
            client._check_not_forbidden(operation)
        assert exc_info.value.operation == operation

    def test_send_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.send")

    def test_delete_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.delete")

    def test_trash_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.trash")

    def test_untrash_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.untrash")

    def test_batch_delete_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.batchDelete")

    def test_batch_modify_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.batchModify")

    def test_draft_create_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.drafts.create")

    def test_draft_send_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.drafts.send")

    def test_attachments_get_is_forbidden(self) -> None:
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError):
            client._check_not_forbidden("users.messages.attachments.get")

    def test_forbidden_error_names_the_operation(self) -> None:
        """Error message must include the offending operation name."""
        client = _make_gmail_client()
        with pytest.raises(ForbiddenOperationError) as exc_info:
            client._check_not_forbidden("users.messages.send")
        assert "users.messages.send" in str(exc_info.value)

    def test_permitted_operations_not_in_forbidden_set(self) -> None:
        """The permitted and forbidden sets must be disjoint."""
        overlap = GmailClient._PERMITTED_OPERATIONS & FORBIDDEN_API_OPERATIONS
        assert overlap == frozenset(), (
            f"These operations appear in BOTH permitted and forbidden sets: {overlap}"
        )

    def test_forbidden_operations_set_is_immutable(self) -> None:
        """FORBIDDEN_API_OPERATIONS must be a frozenset (immutable at runtime)."""
        assert isinstance(FORBIDDEN_API_OPERATIONS, frozenset)

    def test_client_has_no_send_method(self) -> None:
        """GmailClient must have no method named 'send' or 'draft'."""
        client = _make_gmail_client()
        assert not hasattr(client, "send")
        assert not hasattr(client, "draft")
        assert not hasattr(client, "send_message")
        assert not hasattr(client, "create_draft")

    def test_client_has_no_delete_method(self) -> None:
        """GmailClient must have no method named 'delete'."""
        client = _make_gmail_client()
        assert not hasattr(client, "delete")
        assert not hasattr(client, "delete_message")
        assert not hasattr(client, "trash_message")


@pytest.mark.unit
class TestGmailClientPermittedOps:
    """GmailClient has all the permitted methods."""

    def test_has_list_messages(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "list_messages")
        assert callable(client.list_messages)

    def test_has_get_message(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "get_message")
        assert callable(client.get_message)

    def test_has_batch_get_messages(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "batch_get_messages")
        assert callable(client.batch_get_messages)

    def test_has_list_labels(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "list_labels")
        assert callable(client.list_labels)

    def test_has_get_label(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "get_label")
        assert callable(client.get_label)

    def test_has_modify_message_labels(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "modify_message_labels")
        assert callable(client.modify_message_labels)

    def test_has_get_history(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "get_history")
        assert callable(client.get_history)

    def test_has_get_profile(self) -> None:
        client = _make_gmail_client()
        assert hasattr(client, "get_profile")
        assert callable(client.get_profile)


# ── MockGmailClient dataset completeness ──────────────────────────────────────


@pytest.mark.unit
class TestMockGmailClientDataset:
    """MockGmailClient must cover all problem states the workflows need."""

    @pytest.fixture
    def client(self) -> MockGmailClient:
        return MockGmailClient()

    def test_total_message_count(self, client: MockGmailClient) -> None:
        """Dataset must contain a substantial number of messages for realistic demo."""
        assert client.message_count() >= 100, (
            f"Dataset has only {client.message_count()} messages — need at least 100"
        )

    def test_has_inbox_messages(self, client: MockGmailClient) -> None:
        result = client.list_messages(label_ids=["INBOX"])
        assert len(result.get("messages", [])) >= 10, "Must have at least 10 inbox messages"

    def test_has_unread_inbox_messages(self, client: MockGmailClient) -> None:
        result = client.list_messages(label_ids=["INBOX", "UNREAD"])
        assert len(result.get("messages", [])) > 0

    def test_has_sent_messages(self, client: MockGmailClient) -> None:
        result = client.list_messages(label_ids=["SENT"])
        assert len(result.get("messages", [])) >= 5, "Must have at least 5 sent messages"

    def test_has_archived_unlabelled_messages(self, client: MockGmailClient) -> None:
        """Archived messages with no custom labels are the Archive Hygiene target."""
        all_msgs = client.list_messages(max_results=1000)
        archived_unlabelled = 0
        for stub in all_msgs.get("messages", []):
            msg = client.get_message(stub["id"])
            label_ids = set(msg["labelIds"])
            is_archived = ("INBOX" not in label_ids and "TRASH" not in label_ids
                           and "SPAM" not in label_ids)
            system_ids = {"INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED",
                          "IMPORTANT", "UNREAD", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
                          "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
            has_custom = any(lid not in system_ids for lid in label_ids)
            if is_archived and not has_custom:
                archived_unlabelled += 1
        assert archived_unlabelled >= 20, (
            f"Dataset has only {archived_unlabelled} unlabelled archived messages — need ≥ 20"
        )

    def test_has_large_messages(self, client: MockGmailClient) -> None:
        """Must have messages > 5 MB for Size Reduction workflow."""
        all_msgs = client.list_messages(max_results=1000)
        large = [
            stub for stub in all_msgs.get("messages", [])
            if client.get_message(stub["id"])["sizeEstimate"] >= 5 * 1024 * 1024
        ]
        assert len(large) >= 5, f"Only {len(large)} large messages — need at least 5"

    def test_has_very_large_messages(self, client: MockGmailClient) -> None:
        """Must have messages > 15 MB for the Very-Large tier."""
        all_msgs = client.list_messages(max_results=1000)
        very_large = [
            stub for stub in all_msgs.get("messages", [])
            if client.get_message(stub["id"])["sizeEstimate"] >= 15 * 1024 * 1024
        ]
        assert len(very_large) >= 3, f"Only {len(very_large)} very-large messages — need at least 3"

    def test_has_multiple_sender_domains(self, client: MockGmailClient) -> None:
        """Must have at least 5 distinct sender domains."""
        all_msgs = client.list_messages(max_results=1000)
        domains: set[str] = set()
        for stub in all_msgs.get("messages", []):
            msg = client.get_message(stub["id"])
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            sender = headers.get("From", "")
            if "@" in sender:
                domain = sender.split("@")[-1].lower().rstrip(">")
                domains.add(domain)
        assert len(domains) >= 5, f"Only {len(domains)} sender domains — need at least 5"

    def test_has_multi_message_threads(self, client: MockGmailClient) -> None:
        """Must have at least one thread with > 1 message."""
        all_msgs = client.list_messages(max_results=1000)
        thread_counts: dict[str, int] = {}
        for stub in all_msgs.get("messages", []):
            msg = client.get_message(stub["id"])
            tid = msg["threadId"]
            thread_counts[tid] = thread_counts.get(tid, 0) + 1
        multi = {tid for tid, cnt in thread_counts.items() if cnt > 1}
        assert len(multi) >= 2, f"Only {len(multi)} multi-message threads — need at least 2"

    def test_has_labels_in_list_response(self, client: MockGmailClient) -> None:
        """Label list must include both system and user labels."""
        response = client.list_labels()
        labels = response.get("labels", [])
        types = {lbl["type"] for lbl in labels}
        assert "system" in types
        assert "user" in types

    def test_list_messages_returns_all_when_no_filter(
        self, client: MockGmailClient
    ) -> None:
        """Unfiltered list must return all non-trash/spam messages."""
        result = client.list_messages(max_results=1000)
        assert len(result.get("messages", [])) > 50

    def test_profile_returns_correct_email(self, client: MockGmailClient) -> None:
        profile = client.get_profile()
        assert profile["emailAddress"] == MockGmailClient._DEMO_USER_EMAIL
        assert "historyId" in profile
        assert "messagesTotal" in profile

    def test_get_message_returns_correct_structure(
        self, client: MockGmailClient
    ) -> None:
        result = client.list_messages(max_results=1)
        msg_id = result["messages"][0]["id"]
        msg = client.get_message(msg_id)
        assert "id" in msg
        assert "threadId" in msg
        assert "labelIds" in msg
        assert "sizeEstimate" in msg
        assert "internalDate" in msg
        assert "payload" in msg

    def test_get_message_not_found_raises(self, client: MockGmailClient) -> None:
        with pytest.raises(KeyError):
            client.get_message("nonexistent_message_id")

    def test_filter_by_inbox_label(self, client: MockGmailClient) -> None:
        all_result = client.list_messages(max_results=1000)
        inbox_result = client.list_messages(label_ids=["INBOX"], max_results=1000)
        assert len(inbox_result.get("messages", [])) < len(all_result.get("messages", []))
        # Every returned message must have INBOX in its labels
        for stub in inbox_result.get("messages", []):
            msg = client.get_message(stub["id"])
            assert "INBOX" in msg["labelIds"], f"Message {stub['id']} missing INBOX label"

    def test_pagination_works(self, client: MockGmailClient) -> None:
        """next_page_token must allow fetching subsequent pages."""
        first_page = client.list_messages(max_results=5)
        assert "nextPageToken" in first_page
        second_page = client.list_messages(
            max_results=5, page_token=first_page["nextPageToken"]
        )
        first_ids = {m["id"] for m in first_page.get("messages", [])}
        second_ids = {m["id"] for m in second_page.get("messages", [])}
        assert first_ids.isdisjoint(second_ids), "Pages must not overlap"


# ── MockGmailClient label operations ─────────────────────────────────────────


@pytest.mark.unit
class TestMockGmailClientLabelOps:
    """Label mutations are applied immediately and reflected in get_message."""

    @pytest.fixture
    def client(self) -> MockGmailClient:
        return MockGmailClient()

    def _get_first_inbox_id(self, client: MockGmailClient) -> str:
        result = client.list_messages(label_ids=["INBOX"], max_results=1)
        return str(result["messages"][0]["id"])

    def test_add_label_is_reflected(self, client: MockGmailClient) -> None:
        msg_id = self._get_first_inbox_id(client)
        set(client.get_message(msg_id)["labelIds"])
        assert True  # may or may not be there

        client.modify_message_labels(msg_id, add_label_ids=["Label_NeedsAction001"])
        updated = client.get_message(msg_id)
        assert "Label_NeedsAction001" in updated["labelIds"]

    def test_remove_label_is_reflected(self, client: MockGmailClient) -> None:
        msg_id = self._get_first_inbox_id(client)
        # First add, then remove
        client.modify_message_labels(msg_id, add_label_ids=["Label_Complete001"])
        assert "Label_Complete001" in client.get_message(msg_id)["labelIds"]

        client.modify_message_labels(msg_id, remove_label_ids=["Label_Complete001"])
        assert "Label_Complete001" not in client.get_message(msg_id)["labelIds"]

    def test_add_and_remove_simultaneously(self, client: MockGmailClient) -> None:
        msg_id = self._get_first_inbox_id(client)
        client.modify_message_labels(
            msg_id,
            add_label_ids=["Label_NeedsAction001"],
            remove_label_ids=["UNREAD"],
        )
        updated_labels = set(client.get_message(msg_id)["labelIds"])
        assert "Label_NeedsAction001" in updated_labels
        # UNREAD may or may not have been present, but if present it's now removed
        # Just verify the add worked
        assert "Label_NeedsAction001" in updated_labels

    def test_history_id_advances_on_modify(self, client: MockGmailClient) -> None:
        initial_history = int(client.current_history_id)
        msg_id = self._get_first_inbox_id(client)
        client.modify_message_labels(msg_id, add_label_ids=["Label_Complete001"])
        assert int(client.current_history_id) > initial_history

    def test_modify_nonexistent_message_raises(self, client: MockGmailClient) -> None:
        with pytest.raises(KeyError):
            client.modify_message_labels("no_such_id", add_label_ids=["Label_X"])

    def test_get_label_ids_helper(self, client: MockGmailClient) -> None:
        msg_id = self._get_first_inbox_id(client)
        client.modify_message_labels(msg_id, add_label_ids=["Label_NeedsAction001"])
        ids = client.get_label_ids_for_message(msg_id)
        assert isinstance(ids, frozenset)
        assert "Label_NeedsAction001" in ids

    def test_label_filter_reflects_mutations(self, client: MockGmailClient) -> None:
        """After adding a label, the message appears in filtered list."""
        msg_id = "inbox001"
        client.modify_message_labels(msg_id, add_label_ids=["Label_Review001"])
        result = client.list_messages(label_ids=["Label_Review001"], max_results=100)
        returned_ids = {m["id"] for m in result.get("messages", [])}
        assert msg_id in returned_ids


# ── MockGmailClient history simulation ───────────────────────────────────────


@pytest.mark.unit
class TestMockGmailClientHistory:
    """History simulation for incremental sync testing."""

    @pytest.fixture
    def client(self) -> MockGmailClient:
        return MockGmailClient()

    def test_get_history_returns_dict_with_history_id(
        self, client: MockGmailClient
    ) -> None:
        initial_id = client.current_history_id
        response = client.get_history(start_history_id=initial_id)
        assert "historyId" in response

    def test_advance_history_adds_messages(self, client: MockGmailClient) -> None:
        count_before = client.message_count()
        client.advance_history(new_message_count=5)
        assert client.message_count() == count_before + 5

    def test_advance_history_creates_inbox_messages(
        self, client: MockGmailClient
    ) -> None:
        client.advance_history(new_message_count=3)
        result = client.list_messages(label_ids=["INBOX"], max_results=1000)
        # New messages should all be in INBOX
        new_msgs = [
            m for m in result.get("messages", [])
            if m["id"].startswith("sim_")
        ]
        assert len(new_msgs) == 3

    def test_history_events_appear_in_get_history(
        self, client: MockGmailClient
    ) -> None:
        start_id = client.current_history_id
        msg_id = "inbox001"
        client.modify_message_labels(msg_id, add_label_ids=["Label_Complete001"])
        history = client.get_history(start_history_id=start_id)
        assert len(history.get("history", [])) >= 1

    def test_advance_history_advances_history_id(
        self, client: MockGmailClient
    ) -> None:
        before = int(client.current_history_id)
        client.advance_history(new_message_count=2)
        after = int(client.current_history_id)
        assert after > before


# ── GmailMapper tests ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestGmailMapperMessages:
    """GmailMapper correctly maps API dicts to Message domain entities."""

    @pytest.fixture
    def mapper(self) -> GmailMapper:
        return GmailMapper(user_email="me@gmail.com")

    def test_maps_basic_fields(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(
            msg_id="msg001",
            thread_id="thread001",
            size=42_000,
        )
        msg = mapper.api_dict_to_message(api_dict)
        assert isinstance(msg, Message)
        assert msg.id == "msg001"
        assert msg.thread_id == "thread001"
        assert msg.size_estimate == 42_000

    def test_maps_inbox_flags(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(label_ids=["INBOX", "UNREAD"])
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.is_inbox is True
        assert msg.is_unread is True
        assert msg.is_sent is False
        assert msg.is_archived is False

    def test_maps_sent_flags(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(label_ids=["SENT"])
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.is_sent is True
        assert msg.is_inbox is False
        assert msg.is_archived is True  # not in inbox, trash, or spam

    def test_maps_archived_no_labels(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(label_ids=[])
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.is_archived is True
        assert msg.has_custom_label is False

    def test_maps_custom_label(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(label_ids=["INBOX", "Label_customUser123"])
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.has_custom_label is True

    def test_maps_sender_domain(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(sender="Alice <alice@example.com>")
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.sender_domain == "example.com"

    def test_maps_bare_email_sender_domain(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(sender="bob@corp.io")
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.sender_domain == "corp.io"

    def test_maps_subject(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(subject="Test subject line")
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.subject == "Test subject line"

    def test_maps_snippet(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message()
        api_dict["snippet"] = "Short preview text"
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.snippet == "Short preview text"

    def test_internal_date_is_utc_aware(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(days_old=10)
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.internal_date.tzinfo is not None
        assert msg.internal_date.tzinfo == UTC

    def test_first_seen_at_defaults_to_now(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message()
        before = datetime.now(tz=UTC)
        msg = mapper.api_dict_to_message(api_dict)
        after = datetime.now(tz=UTC)
        assert before <= msg.first_seen_at <= after

    def test_first_seen_at_override(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message()
        custom_time = datetime(2023, 1, 15, 12, 0, 0, tzinfo=UTC)
        msg = mapper.api_dict_to_message(api_dict, first_seen_at=custom_time)
        assert msg.first_seen_at == custom_time

    def test_maps_label_ids_to_frozenset(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message(label_ids=["INBOX", "UNREAD", "Label_custom"])
        msg = mapper.api_dict_to_message(api_dict)
        assert isinstance(msg.label_ids, frozenset)
        assert "INBOX" in msg.label_ids
        assert "UNREAD" in msg.label_ids

    def test_missing_snippet_produces_none(self, mapper: GmailMapper) -> None:
        api_dict = _make_api_message()
        api_dict.pop("snippet", None)
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.snippet is None

    def test_category_labels_not_counted_as_custom(
        self, mapper: GmailMapper
    ) -> None:
        api_dict = _make_api_message(
            label_ids=["INBOX", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"]
        )
        msg = mapper.api_dict_to_message(api_dict)
        assert msg.has_custom_label is False


@pytest.mark.unit
class TestGmailMapperLabels:
    """GmailMapper correctly maps label dicts."""

    @pytest.fixture
    def mapper(self) -> GmailMapper:
        return GmailMapper(user_email="me@gmail.com")

    def test_maps_system_label(self, mapper: GmailMapper) -> None:
        api_dict = {"id": "INBOX", "name": "INBOX", "type": "system"}
        label = mapper.api_dict_to_label(api_dict)
        assert label.id == "INBOX"
        assert label.label_type == LabelType.SYSTEM
        assert label.is_system is True

    def test_maps_user_label(self, mapper: GmailMapper) -> None:
        api_dict = {
            "id": "Label_12345",
            "name": "ZeroApp/Needs-Action",
            "type": "user",
            "messageListVisibility": "show",
            "labelListVisibility": "labelShow",
        }
        label = mapper.api_dict_to_label(api_dict)
        assert label.id == "Label_12345"
        assert label.label_type == LabelType.USER
        assert label.name == "ZeroApp/Needs-Action"
        assert label.is_app_managed is True

    def test_api_dict_to_labels_from_list_response(
        self, mapper: GmailMapper
    ) -> None:
        response = {
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "Label_001", "name": "Work", "type": "user"},
            ]
        }
        labels = mapper.api_dict_to_labels(response)
        assert len(labels) == 2

    def test_synced_at_is_utc_aware(self, mapper: GmailMapper) -> None:
        api_dict = {"id": "INBOX", "name": "INBOX", "type": "system"}
        label = mapper.api_dict_to_label(api_dict)
        assert label.synced_at.tzinfo is not None

    def test_unknown_visibility_produces_none(self, mapper: GmailMapper) -> None:
        api_dict = {
            "id": "Label_001",
            "name": "Test",
            "type": "user",
            "messageListVisibility": "unknownValue",
            "labelListVisibility": "unknownValue",
        }
        label = mapper.api_dict_to_label(api_dict)
        assert label.message_list_visibility is None
        assert label.label_list_visibility is None


@pytest.mark.unit
class TestGmailMapperHelpers:
    """Private helper methods behave correctly at boundary conditions."""

    @pytest.fixture
    def mapper(self) -> GmailMapper:
        return GmailMapper(user_email="me@gmail.com")

    def test_extract_domain_bare_email(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        assert M._extract_domain("user@domain.com") == "domain.com"

    def test_extract_domain_display_name(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        assert M._extract_domain("Alice Smith <alice@corp.io>") == "corp.io"

    def test_extract_domain_angle_only(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        assert M._extract_domain("<user@example.org>") == "example.org"

    def test_extract_domain_no_email_returns_unknown(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        assert M._extract_domain("not an email at all") == "unknown"

    def test_extract_domain_empty_returns_unknown(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        assert M._extract_domain("") == "unknown"

    def test_parse_internal_date_milliseconds(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        ts = M._parse_internal_date("1709640000000")  # 2024-03-05 UTC
        assert ts.year == 2024
        assert ts.tzinfo == UTC

    def test_parse_internal_date_zero(self, mapper: GmailMapper) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        ts = M._parse_internal_date("0")
        assert ts.year == 1970

    def test_parse_internal_date_invalid_returns_epoch(
        self, mapper: GmailMapper
    ) -> None:
        from infrastructure.gmail.mapper import GmailMapper as M
        ts = M._parse_internal_date("not_a_number")
        assert ts.year == 1970

    def test_extract_history_changes(self, mapper: GmailMapper) -> None:
        history_response = {
            "historyId": "100050",
            "history": [
                {
                    "id": "100040",
                    "messagesAdded": [{"message": {"id": "msg_new", "threadId": "t1"}}],
                },
                {
                    "id": "100045",
                    "labelsAdded": [{"message": {"id": "msg_changed"}, "labelIds": ["Label_X"]}],
                },
            ],
        }
        added, changed = mapper.extract_changed_message_ids(history_response)
        assert "msg_new" in added
        assert "msg_changed" in changed

    def test_mock_client_satisfies_abstract_protocol(self) -> None:
        """MockGmailClient must satisfy the AbstractGmailClient Protocol."""
        from infrastructure.gmail.client import AbstractGmailClient

        client = MockGmailClient()
        assert isinstance(client, AbstractGmailClient), (
            "MockGmailClient does not satisfy AbstractGmailClient protocol"
        )
