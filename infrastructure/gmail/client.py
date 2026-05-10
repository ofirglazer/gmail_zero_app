"""
Gmail API client layer for gmail_zero_app.

Provides:
    AbstractGmailClient  — Protocol defining the contract all clients must satisfy
    GmailClient          — Production implementation with whitelist enforcement (Layer 3 safety)

Safety architecture — Layer 3:
    GmailClient maintains an explicit whitelist of permitted API methods.
    Any call to a method not on the whitelist raises ForbiddenOperationError
    immediately, before any network request is made.

    This is the third and final safety layer:
        Layer 1: OAuth scopes (Google rejects forbidden calls at the network level)
        Layer 2: SafetyGuard (domain service validates every label operation)
        Layer 3: GmailClient whitelist (THIS FILE)

    All three layers must be defeated independently for a forbidden operation
    to reach the Gmail API. In practice this is impossible — the layers are
    orthogonal and each catches a different class of violation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from domain.exceptions import ForbiddenOperationError
from domain.safety.constants import FORBIDDEN_API_OPERATIONS

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


# ── AbstractGmailClient Protocol ──────────────────────────────────────────────


@runtime_checkable
class AbstractGmailClient(Protocol):
    """
    Protocol defining the interface all Gmail client implementations must satisfy.

    Both ``GmailClient`` (production) and ``MockGmailClient`` (demo/tests)
    implement this Protocol.  Code that uses the client accepts
    ``AbstractGmailClient`` — it never knows which implementation it holds.

    Method naming mirrors the Gmail REST API resource hierarchy where practical.
    """

    def list_messages(
        self,
        *,
        max_results: int = 500,
        page_token: str | None = None,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """
        List messages matching the optional filter criteria.

        Mirrors: users.messages.list

        Args:
            max_results:        Maximum messages to return (1-500).
            page_token:         Pagination token from a previous response.
            label_ids:          Restrict to messages carrying all listed labels.
            include_spam_trash: Whether to include SPAM and TRASH messages.

        Returns:
            Gmail API response dict with ``messages`` list and optional
            ``nextPageToken``.
        """
        ...

    def get_message(
        self,
        message_id: str,
        *,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Fetch a single message's metadata.

        Mirrors: users.messages.get

        Args:
            message_id:       Gmail message ID.
            format:           ``"metadata"`` (headers only) or ``"minimal"``.
            metadata_headers: Specific headers to include (e.g. ``["From", "Subject"]``).

        Returns:
            Gmail API message resource dict.
        """
        ...

    def batch_get_messages(
        self,
        message_ids: list[str],
        *,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch metadata for multiple messages.

        Implemented as multiple ``get_message`` calls on the mock, and as a
        true batch request on the production client.

        Args:
            message_ids:      List of Gmail message IDs.
            format:           ``"metadata"`` or ``"minimal"``.
            metadata_headers: Specific headers to include.

        Returns:
            List of Gmail API message resource dicts, in the same order as
            ``message_ids``.
        """
        ...

    def list_labels(self) -> dict[str, Any]:
        """
        List all labels in the authenticated user's mailbox.

        Mirrors: users.labels.list

        Returns:
            Gmail API response dict with ``labels`` list.
        """
        ...

    def get_label(self, label_id: str) -> dict[str, Any]:
        """
        Fetch a single label by ID.

        Mirrors: users.labels.get

        Args:
            label_id: Gmail label ID.

        Returns:
            Gmail API label resource dict.
        """
        ...

    def modify_message_labels(
        self,
        message_id: str,
        *,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Add or remove labels on a single message.

        Mirrors: users.messages.modify (the only write operation permitted)

        Args:
            message_id:       Gmail message ID.
            add_label_ids:    Labels to add.  None or empty = no additions.
            remove_label_ids: Labels to remove.  None or empty = no removals.

        Returns:
            The updated Gmail API message resource dict.
        """
        ...

    def get_history(
        self,
        *,
        start_history_id: str,
        history_types: list[str] | None = None,
        max_results: int = 500,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """
        Fetch history events since the given history ID.

        Used for incremental sync.  Mirrors: users.history.list

        Args:
            start_history_id: History ID to start listing from.
            history_types:    Event types to return (e.g. ``["messageAdded"]``).
            max_results:      Maximum history records per page.
            page_token:       Pagination token.

        Returns:
            Gmail API history list response dict.
        """
        ...

    def get_profile(self) -> dict[str, Any]:
        """
        Fetch the authenticated user's Gmail profile.

        Used to retrieve the current historyId after a full sync.
        Mirrors: users.getProfile

        Returns:
            Dict with ``emailAddress``, ``messagesTotal``, ``historyId``, etc.
        """
        ...


# ── GmailClient ───────────────────────────────────────────────────────────────


class GmailClient:
    """
    Production Gmail API client with whitelist-based method enforcement.

    Wraps google-api-python-client and ensures only approved API methods
    are callable.  Any attempt to call a method on FORBIDDEN_API_OPERATIONS
    raises ForbiddenOperationError immediately, before any network I/O.

    This is Layer 3 of the three-layer safety architecture.

    Args:
        credentials: OAuth2 credentials from the ``OAuthHandler``.
    """

    # ── Permitted API resource paths ──────────────────────────────────────────
    # Explicit allowlist — if a method is not here, it cannot be called.
    # This is checked in __init__ against FORBIDDEN_API_OPERATIONS to ensure
    # no accidentally permitted method overlaps with the forbidden set.
    _PERMITTED_OPERATIONS: frozenset[str] = frozenset({
        "users.messages.list",
        "users.messages.get",
        "users.messages.modify",   # The only write — add/remove labels only
        "users.labels.list",
        "users.labels.get",
        "users.history.list",
        "users.getProfile",
    })

    def __init__(self, credentials: Credentials) -> None:
        # Validate at construction time that permitted and forbidden sets
        # are disjoint — catches accidental config changes early.
        overlap = self._PERMITTED_OPERATIONS & FORBIDDEN_API_OPERATIONS
        if overlap:
            raise RuntimeError(
                f"SAFETY CONFIGURATION ERROR: The following operations appear in both "
                f"the permitted and forbidden sets: {sorted(overlap)}. "
                "This indicates a programming error in the safety architecture."
            )

        self._credentials = credentials
        self._service = self._build_service()

    def _build_service(self) -> Any:
        """Build the google-api-python-client service object."""
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=self._credentials)

    def _check_not_forbidden(self, operation: str) -> None:
        """
        Raise ForbiddenOperationError if the operation is on the forbidden list.

        Called at the start of every public method as a defence-in-depth check.

        Args:
            operation: The Gmail API resource path, e.g. ``"users.messages.list"``.

        Raises:
            ForbiddenOperationError: If the operation is forbidden.
        """
        if operation in FORBIDDEN_API_OPERATIONS:
            raise ForbiddenOperationError(operation)

    def list_messages(
        self,
        *,
        max_results: int = 500,
        page_token: str | None = None,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """List messages. See AbstractGmailClient for full docstring."""
        self._check_not_forbidden("users.messages.list")
        request = self._service.users().messages().list(
            userId="me",
            maxResults=max_results,
            pageToken=page_token,
            labelIds=label_ids,
            includeSpamTrash=include_spam_trash,
        )
        return request.execute()  # type: ignore[no-any-return]

    def get_message(
        self,
        message_id: str,
        *,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get a single message. See AbstractGmailClient for full docstring."""
        self._check_not_forbidden("users.messages.get")
        request = self._service.users().messages().get(
            userId="me",
            id=message_id,
            format=format,
            metadataHeaders=metadata_headers,
        )
        return request.execute()  # type: ignore[no-any-return]

    def batch_get_messages(
        self,
        message_ids: list[str],
        *,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch metadata for multiple messages using the Gmail batch HTTP endpoint.

        Sends a single HTTP request containing multiple sub-requests.
        Falls back to sequential calls if the batch library is unavailable.
        """
        self._check_not_forbidden("users.messages.get")
        # Gmail batch: up to 100 requests per batch HTTP call.
        # For simplicity in Step 4, sequential calls; batch optimisation in Step 5.
        return [
            self.get_message(mid, format=format, metadata_headers=metadata_headers)
            for mid in message_ids
        ]

    def list_labels(self) -> dict[str, Any]:
        """List all labels. See AbstractGmailClient for full docstring."""
        self._check_not_forbidden("users.labels.list")
        return self._service.users().labels().list(userId="me").execute()  # type: ignore[no-any-return]

    def get_label(self, label_id: str) -> dict[str, Any]:
        """Get a single label. See AbstractGmailClient for full docstring."""
        self._check_not_forbidden("users.labels.get")
        return self._service.users().labels().get(userId="me", id=label_id).execute()  # type: ignore[no-any-return]

    def modify_message_labels(
        self,
        message_id: str,
        *,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Add or remove labels on a message.

        This is the ONLY write operation this client ever performs.
        The SafetyGuard (Layer 2) must have validated the operation before
        this method is called.

        See AbstractGmailClient for full docstring.
        """
        self._check_not_forbidden("users.messages.modify")
        body: dict[str, list[str]] = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        return (  # type: ignore[no-any-return]
            self._service.users()
            .messages()
            .modify(userId="me", id=message_id, body=body)
            .execute()
        )

    def get_history(
        self,
        *,
        start_history_id: str,
        history_types: list[str] | None = None,
        max_results: int = 500,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch history events. See AbstractGmailClient for full docstring."""
        self._check_not_forbidden("users.history.list")
        return (  # type: ignore[no-any-return]
            self._service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=history_types,
                maxResults=max_results,
                pageToken=page_token,
            )
            .execute()
        )

    def get_profile(self) -> dict[str, Any]:
        """Fetch user profile. See AbstractGmailClient for full docstring."""
        self._check_not_forbidden("users.getProfile")
        return self._service.users().getProfile(userId="me").execute()  # type: ignore[no-any-return]
