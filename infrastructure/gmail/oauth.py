"""
OAuth2 flow handler for gmail_zero_app.

Manages the three phases of OAuth credential lifecycle:
    1. Load existing token from disk (fast path — used on every startup)
    2. Refresh an expired token (transparent, no user interaction)
    3. Initiate a new OAuth2 flow (first run or revoked token)

Safety constraints:
    - Only REQUIRED_SCOPES are ever requested.
    - If a loaded token contains FORBIDDEN_SCOPES, the app refuses to proceed.
    - Credentials are stored locally only (never sent anywhere except Google).

The ``OAuthHandler`` is only used in production mode.  Demo mode uses
``MockGmailClient`` and never calls any OAuth code.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from config.oauth_scopes import FORBIDDEN_SCOPES, REQUIRED_SCOPES
from domain.exceptions import AuthenticationError

if TYPE_CHECKING:
    from pathlib import Path
    pass


class OAuthHandler:
    """
    Manages Gmail OAuth2 credentials for the production client.

    Handles token loading, refresh, and the initial authorisation flow.
    Stores credentials as JSON at the configured token path.

    Args:
        credentials_path: Path to the ``credentials.json`` file downloaded
                          from Google Cloud Console.
        token_path:       Path where the user's OAuth token is stored after
                          the initial authorisation.
    """

    def __init__(
        self,
        credentials_path: Path,
        token_path: Path,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path

    def get_credentials(self) -> Any:
        """
        Return valid OAuth2 credentials, refreshing or re-authorising as needed.

        Lifecycle:
            1. If token file exists and token is valid → return it.
            2. If token file exists but token is expired → refresh and return.
            3. If no token file → run the full OAuth2 flow (opens browser).

        Returns:
            ``google.oauth2.credentials.Credentials`` instance with valid tokens.

        Raises:
            AuthenticationError: If credentials.json is missing, malformed,
                                 or if the OAuth flow fails.
            AuthenticationError: If the granted scopes include any forbidden scope.
        """
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise AuthenticationError(
                "OAuth dependencies are not installed. "
                "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
            ) from exc

        credentials: Any | None = None

        # ── Phase 1: Load existing token ──────────────────────────────────────
        if self._token_path.exists():
            try:
                credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                    str(self._token_path), REQUIRED_SCOPES
                )
            except (ValueError, json.JSONDecodeError) as exc:
                # Malformed token file — will fall through to re-auth below
                raise AuthenticationError(
                    f"Token file at {self._token_path} is malformed: {exc}. "
                    "Delete it and re-authorise."
                ) from exc

        # ── Phase 2: Refresh if expired ───────────────────────────────────────
        if credentials is not None and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                self._save_token(credentials)
            except Exception:
                # Refresh failed (e.g. revoked) — fall through to re-auth
                credentials = None
                if self._token_path.exists():
                    self._token_path.unlink()

        # ── Phase 3: Full OAuth2 flow ─────────────────────────────────────────
        if credentials is None or not credentials.valid:
            if not self._credentials_path.exists():
                raise AuthenticationError(
                    f"OAuth credentials file not found at {self._credentials_path}. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_path), REQUIRED_SCOPES
                )
                credentials = flow.run_local_server(port=0)
                self._save_token(credentials)
            except Exception as exc:
                raise AuthenticationError(
                    f"OAuth2 authorisation flow failed: {exc}"
                ) from exc

        # ── Safety check: forbid over-privileged tokens ───────────────────────
        self._validate_scopes(credentials)
        return credentials

    def revoke(self) -> None:
        """
        Revoke the stored token and delete the token file.

        After calling this, the next ``get_credentials()`` call will trigger
        a fresh OAuth2 flow.

        Raises:
            AuthenticationError: If revocation fails (e.g. network error).
        """
        if not self._token_path.exists():
            return  # Nothing to revoke

        try:
            import requests
            from google.oauth2.credentials import Credentials

            credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                str(self._token_path)
            )
            if credentials.token:
                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": credentials.token},
                    timeout=10,
                )
        except Exception:
            pass  # Best-effort — always delete local token
        finally:
            if self._token_path.exists():
                self._token_path.unlink()

    def _save_token(self, credentials: Any) -> None:
        """
        Persist credentials to the token file.

        Creates parent directories if they do not exist.

        Args:
            credentials: A ``google.oauth2.credentials.Credentials`` instance.
        """
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(credentials.to_json(), encoding="utf-8")

    @staticmethod
    def _validate_scopes(credentials: Any) -> None:
        """
        Ensure no forbidden scopes are present in the token.

        Raises:
            AuthenticationError: If the token includes any scope in
                                 ``FORBIDDEN_SCOPES``.
        """
        # ``credentials.scopes`` may be None if the token predates scope tracking
        granted: set[str] = set(credentials.scopes or [])
        violations = granted & FORBIDDEN_SCOPES
        if violations:
            raise AuthenticationError(
                f"The OAuth token contains forbidden scopes: {sorted(violations)}. "
                "Revoke the token and re-authorise with only the required scopes."
            )

    @property
    def token_exists(self) -> bool:
        """True if a token file exists at the configured path."""
        return self._token_path.exists()

    @property
    def credentials_exist(self) -> bool:
        """True if a credentials.json exists at the configured path."""
        return self._credentials_path.exists()
