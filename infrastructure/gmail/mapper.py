"""
GmailMapper — translates Gmail API response dicts into domain entities.

This is the single point of translation between the Gmail API's wire format
and the application's domain model.  All field extraction, type coercion,
and derived-field computation lives here.

Design decisions:
    - All datetimes are converted to UTC-aware immediately on ingestion.
    - ``sender_domain`` is extracted once at mapping time and stored; it is
      never re-derived during queries.
    - ``has_custom_label`` is computed from label_ids using the same logic
      as ``make_message()``, keeping them in sync.
    - Missing or malformed fields are handled defensively — a bad API response
      produces a degraded entity rather than an exception (except for the
      mandatory ``id`` and ``threadId`` fields).
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from typing import Any

from domain.models.label import Label, LabelListVisibility, LabelType, MessageListVisibility
from domain.models.message import Message, make_message

# ── Constants ─────────────────────────────────────────────────────────────────

# Metadata headers fetched during sync.  Requesting only what we need keeps
# API response sizes small.
DEFAULT_METADATA_HEADERS: list[str] = [
    "From",
    "To",
    "Subject",
    "Date",
]

# Regex for extracting the email address from a display-name address.
# Handles: "Name <email@example.com>", "<email@example.com>", "email@example.com"
_EMAIL_RE = re.compile(r"<([^>]+)>|([^\s<>]+@[^\s<>]+)")


# ── Mapper class ──────────────────────────────────────────────────────────────


class GmailMapper:
    """
    Stateless mapper from Gmail API response dicts to domain entities.

    Instantiate once and reuse.  All methods are pure (no side effects,
    no I/O, no mutation of arguments).

    Usage::

        mapper = GmailMapper(user_email="me@gmail.com")
        message = mapper.api_dict_to_message(api_response)
        labels  = mapper.api_dict_to_labels(labels_list_response)
    """

    def __init__(self, user_email: str) -> None:
        """
        Args:
            user_email: The authenticated user's email address.  Used to
                        determine whether a message is sent (From == user).
        """
        self._user_email = user_email.lower().strip()

    # ── Message mapping ───────────────────────────────────────────────────────

    def api_dict_to_message(
        self,
        api_dict: dict[str, Any],
        *,
        first_seen_at: datetime | None = None,
    ) -> Message:
        """
        Map a Gmail API message resource dict to a frozen ``Message`` entity.

        Expects the response format returned by ``users.messages.get`` with
        ``format="metadata"``.

        Args:
            api_dict:     Raw Gmail API message dict.
            first_seen_at: Override for the ``first_seen_at`` timestamp.
                           If None, defaults to UTC now (first encounter).

        Returns:
            An immutable ``Message`` domain entity.

        Raises:
            KeyError: If the mandatory ``id`` or ``threadId`` fields are absent.
        """
        message_id: str = api_dict["id"]
        thread_id: str = api_dict["threadId"]
        history_id: str = api_dict.get("historyId", "0")

        # Gmail provides internalDate as milliseconds since Unix epoch (string)
        internal_date = self._parse_internal_date(
            api_dict.get("internalDate", "0")
        )

        # Label IDs are already a list of strings
        label_ids: frozenset[str] = frozenset(api_dict.get("labelIds", []))

        # Extract from metadata headers
        headers: dict[str, str] = self._extract_headers(api_dict)
        sender_raw: str = headers.get("From", "")
        recipient_raw: str = headers.get("To", "")
        subject: str | None = headers.get("Subject") or None

        sender = sender_raw or "unknown@unknown.com"
        sender_domain = self._extract_domain(sender)
        recipient: str | None = recipient_raw or None

        snippet: str | None = api_dict.get("snippet") or None
        size_estimate: int = int(api_dict.get("sizeEstimate", 0))

        now = datetime.now(tz=UTC)
        return make_message(
            id=message_id,
            thread_id=thread_id,
            history_id=history_id,
            internal_date=internal_date,
            sender=sender,
            sender_domain=sender_domain,
            recipient=recipient,
            subject=subject,
            snippet=snippet,
            size_estimate=size_estimate,
            label_ids=label_ids,
            first_seen_at=first_seen_at or now,
            last_synced_at=now,
        )

    def api_dict_to_label(self, api_dict: dict[str, Any]) -> Label:
        """
        Map a Gmail API label resource dict to a frozen ``Label`` entity.

        Expects the format returned by ``users.labels.list`` or
        ``users.labels.get``.

        Args:
            api_dict: Raw Gmail API label dict.

        Returns:
            An immutable ``Label`` domain entity.
        """
        label_id: str = api_dict["id"]
        name: str = api_dict.get("name", label_id)
        raw_type: str = api_dict.get("type", "user")
        label_type = LabelType.SYSTEM if raw_type == "system" else LabelType.USER

        raw_mlv: str | None = api_dict.get("messageListVisibility")
        raw_llv: str | None = api_dict.get("labelListVisibility")

        mlv: MessageListVisibility | None = None
        llv: LabelListVisibility | None = None

        if raw_mlv:
            with contextlib.suppress(ValueError):
                mlv = MessageListVisibility(raw_mlv)

        if raw_llv:
            with contextlib.suppress(ValueError):
                llv = LabelListVisibility(raw_llv)

        return Label(
            id=label_id,
            name=name,
            label_type=label_type,
            message_list_visibility=mlv,
            label_list_visibility=llv,
            synced_at=datetime.now(tz=UTC),
        )

    def api_dict_to_labels(self, list_response: dict[str, Any]) -> list[Label]:
        """
        Map the full ``users.labels.list`` response to a list of ``Label`` entities.

        Args:
            list_response: The full Gmail API ``users.labels.list`` response.

        Returns:
            List of ``Label`` entities.
        """
        raw_labels: list[dict[str, Any]] = list_response.get("labels", [])
        return [self.api_dict_to_label(raw) for raw in raw_labels]

    # ── History event extraction ──────────────────────────────────────────────

    def extract_changed_message_ids(
        self, history_response: dict[str, Any]
    ) -> tuple[set[str], set[str]]:
        """
        Extract added and label-changed message IDs from a history response.

        Used by the incremental sync pipeline to determine which messages
        need to be re-fetched.

        Args:
            history_response: A ``users.history.list`` API response dict.

        Returns:
            A 2-tuple of (added_ids, label_changed_ids) where each element
            is a set of Gmail message IDs.
        """
        added_ids: set[str] = set()
        label_changed_ids: set[str] = set()

        history_records: list[dict[str, Any]] = history_response.get("history", [])
        for record in history_records:
            for added in record.get("messagesAdded", []):
                msg_id: str = added.get("message", {}).get("id", "")
                if msg_id:
                    added_ids.add(msg_id)

            for changed in record.get("labelsAdded", []) + record.get("labelsRemoved", []):
                msg_id = changed.get("message", {}).get("id", "")
                if msg_id:
                    label_changed_ids.add(msg_id)

        return added_ids, label_changed_ids

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_internal_date(raw: str) -> datetime:
        """
        Convert Gmail's ``internalDate`` (ms since epoch, as string) to UTC datetime.

        Args:
            raw: String representation of milliseconds since Unix epoch.

        Returns:
            UTC-aware datetime.
        """
        try:
            ms = int(raw)
        except (ValueError, TypeError):
            ms = 0
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)

    @staticmethod
    def _extract_headers(api_dict: dict[str, Any]) -> dict[str, str]:
        """
        Extract message headers as a flat name→value dict.

        Gmail returns headers as a list of ``{"name": ..., "value": ...}``
        dicts under ``payload.headers``.  When multiple headers share a name
        (e.g. ``Received``), the first value wins.

        Args:
            api_dict: Raw Gmail API message dict.

        Returns:
            Dict mapping header name to value (first occurrence wins).
        """
        result: dict[str, str] = {}
        payload: dict[str, Any] = api_dict.get("payload", {})
        headers_list: list[dict[str, str]] = payload.get("headers", [])
        for header in headers_list:
            name: str = header.get("name", "")
            value: str = header.get("value", "")
            if name and name not in result:
                result[name] = value
        return result

    @staticmethod
    def _extract_domain(address: str) -> str:
        """
        Extract the domain portion from an email address string.

        Handles display-name format: ``"Name <user@domain.com>"`` → ``"domain.com"``
        Handles bare format: ``"user@domain.com"`` → ``"domain.com"``
        Falls back to ``"unknown"`` if no recognisable address is found.

        Args:
            address: Raw email address string, possibly with display name.

        Returns:
            Lowercase domain string.
        """
        match = _EMAIL_RE.search(address)
        if not match:
            return "unknown"
        email = match.group(1) or match.group(2) or ""
        if "@" not in email:
            return "unknown"
        return email.split("@")[-1].lower()

    @staticmethod
    def _extract_email(address: str) -> str | None:
        """
        Extract the bare email address from a display-name string.

        Args:
            address: Raw address string.

        Returns:
            Bare email address, or None if not parseable.
        """
        match = _EMAIL_RE.search(address)
        if not match:
            return None
        return (match.group(1) or match.group(2) or "").lower() or None
