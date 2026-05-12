"""
LabelConfigService — label configuration bootstrap and Gmail label management.

Reads ``config/labels.toml`` (the single source of truth for label names),
compares the app-managed labels against the user's Gmail mailbox, and creates
any labels that do not yet exist.

This service is called once at application startup (or on demand via a CLI
command) to ensure the Gmail mailbox has the expected label structure.

⚠️  WHITELIST NOTE:
    This service requires ``users.labels.create`` (and optionally
    ``users.labels.update``) to be on ``GmailClient._PERMITTED_OPERATIONS``.
    Both methods were added to AbstractGmailClient and GmailClient as part
    of Step 5.  They are intentionally absent from earlier steps since label
    creation was not needed before this service existed.

Label sections in labels.toml that contribute managed labels:
    [workflow]       — needs_action, awaiting_reply, complete, review,
                        followup, to_remove
    [classification] — newsletters, notifications, receipts, bulk
    [size]           — large, very_large
    [custom]         — user-defined additions

The [app] section is metadata (label_namespace), not a label definition.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from domain.exceptions import LabelConfigError

if TYPE_CHECKING:
    from infrastructure.gmail.client import AbstractGmailClient
    from infrastructure.persistence.repositories.label_repository import LabelRepository

# Sections in labels.toml whose values are label names to be managed.
# [app] is excluded — it contains the namespace prefix, not a label name.
_LABEL_SECTIONS: frozenset[str] = frozenset({
    "workflow",
    "classification",
    "size",
    "custom",
})


class LabelConfigService:
    """
    Bootstraps the Gmail label structure from labels.toml.

    Reads the TOML config, lists existing Gmail labels, and creates any
    app-managed labels that are missing.  Idempotent — safe to call multiple
    times; existing labels are not modified.

    Args:
        client:             Gmail client (AbstractGmailClient).
        label_repo:         Label persistence repository.
        labels_config_path: Path to ``config/labels.toml``.
    """

    def __init__(
        self,
        client: AbstractGmailClient,
        label_repo: LabelRepository,
        labels_config_path: Path,
    ) -> None:
        self._client = client
        self._label_repo = label_repo
        self._labels_config_path = labels_config_path

        # Lazy-loaded: populated on first call to ensure_labels_exist()
        # Maps config key (e.g. "newsletter") → Gmail label ID
        self._key_to_id: dict[str, str] = {}

    # ── Public operations ─────────────────────────────────────────────────────

    def ensure_labels_exist(self) -> None:
        """
        Create any ZeroApp/* labels defined in labels.toml that do not yet
        exist in the user's Gmail mailbox.

        Idempotent — already-existing labels are left untouched.  The
        key → ID mapping is cached in ``_key_to_id`` after the first call.

        Raises:
            LabelConfigError: If labels.toml cannot be read or is malformed.
        """
        managed_labels = self._read_managed_labels()

        # Fetch existing Gmail labels and build a name → id lookup
        existing_response = self._client.list_labels()
        existing_by_name: dict[str, str] = {
            lbl["name"]: lbl["id"]
            for lbl in existing_response.get("labels", [])
        }

        for config_key, label_name in managed_labels.items():
            if label_name in existing_by_name:
                # Label already exists — record the mapping and move on
                self._key_to_id[config_key] = existing_by_name[label_name]
            else:
                # Label is missing — create it via the API
                created = self._client.create_label(label_name)
                new_id: str = created["id"]
                self._key_to_id[config_key] = new_id

    def get_label_id(self, config_key: str) -> str | None:
        """
        Return the Gmail label ID for a labels.toml config key.

        ``ensure_labels_exist()`` must have been called first to populate the
        key → ID cache.

        Args:
            config_key: Left-hand side of a labels.toml entry, e.g. ``"newsletter"``.

        Returns:
            Gmail label ID string, or None if the key is not a managed label.
        """
        return self._key_to_id.get(config_key)

    def get_all_app_label_ids(self) -> frozenset[str]:
        """
        Return the frozen set of all Gmail label IDs managed by this app.

        ``ensure_labels_exist()`` must have been called first.

        Returns:
            Frozen set of Gmail label ID strings.
        """
        return frozenset(self._key_to_id.values())

    # ── Private helpers ───────────────────────────────────────────────────────

    def _read_managed_labels(self) -> dict[str, str]:
        """
        Parse labels.toml and return a flat ``{config_key: label_name}`` dict
        for all sections in _LABEL_SECTIONS.

        Returns:
            Mapping from config key (e.g. ``"newsletter"``) to Gmail label name
            (e.g. ``"ZeroApp/Newsletter"``).

        Raises:
            LabelConfigError: If the file cannot be found, read, or parsed.
        """
        path = self._labels_config_path
        if not path.exists():
            raise LabelConfigError(
                path=str(path),
                reason=(
                    f"File not found: {path}. "
                    "Ensure the application is run from the project root or "
                    "GMAIL_ZERO_LABELS_CONFIG_PATH is set correctly."
                ),
            )

        try:
            raw: dict = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise LabelConfigError(
                path=str(path),
                reason=f"TOML parse error: {exc}",
            ) from exc

        managed: dict[str, str] = {}
        for section_name, section_data in raw.items():
            # Skip non-label sections (e.g. [app] contains namespace config)
            if section_name not in _LABEL_SECTIONS:
                continue

            if not isinstance(section_data, dict):
                raise LabelConfigError(
                    path=str(path),
                    reason=(
                        f"Section [{section_name}] must be a table of key = \"label name\" "
                        f"pairs; got {type(section_data).__name__!r}."
                    ),
                )

            for key, label_name in section_data.items():
                if not isinstance(label_name, str):
                    raise LabelConfigError(
                        path=str(path),
                        reason=(
                            f"[{section_name}].{key} must be a string label name; "
                            f"got {type(label_name).__name__!r}."
                        ),
                    )
                if not label_name.strip():
                    raise LabelConfigError(
                        path=str(path),
                        reason=f"[{section_name}].{key} must not be an empty string.",
                    )
                managed[key] = label_name

        return managed
