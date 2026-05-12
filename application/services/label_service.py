"""
LabelService — application-layer orchestration of Gmail label operations.

Every label operation follows the same five-step pipeline:

    1. SafetyGuard validates the request (Layer 2 safety check).
       SafetyViolationError propagates — never caught here.
    2. Gmail API call (modify_message_labels) mutates the mailbox.
    3. MessageRepository is updated with the new label set.
    4. LabelRepository junction table is kept in sync.
    5. Audit log entry is written regardless of success or failure.

Bulk operations expand to individual requests (step 2 in BulkLabelOperationRequest)
and apply them one at a time, collecting per-message errors.  All messages are
attempted even when some fail; a summary exception is raised at the end if any
errors occurred.

Critical invariants from README_HANDOFF.md:
    - NEVER catch SafetyViolationError — let it propagate to the route layer.
    - NEVER suppress ForbiddenOperationError.
    - Always call log_label_operation regardless of success/failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.exceptions import LabelOperationError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from application.dto.label_operation import BulkLabelOperationRequest, LabelOperationRequest
    from domain.models.message import Message
    from domain.safety.guard import SafetyGuard
    from infrastructure.gmail.client import AbstractGmailClient
    from infrastructure.persistence.repositories.label_repository import LabelRepository
    from infrastructure.persistence.repositories.message_repository import MessageRepository


class LabelService:
    """
    Application service for adding and removing Gmail labels.

    All operations pass through SafetyGuard before touching the Gmail API.
    The audit log receives an entry for every attempt, success or failure.

    Args:
        client:     Gmail API client (AbstractGmailClient).
        guard:      SafetyGuard instance for pre-flight safety validation.
        msg_repo:   Message persistence repository.
        label_repo: Label persistence and audit-log repository.
        session:    Open SQLAlchemy session (caller owns commit/rollback).
    """

    def __init__(
        self,
        client: AbstractGmailClient,
        guard: SafetyGuard,
        msg_repo: MessageRepository,
        label_repo: LabelRepository,
        session: Session,
    ) -> None:
        self._client = client
        self._guard = guard
        self._msg_repo = msg_repo
        self._label_repo = label_repo
        self._session = session

    # ── Public operation entry points ─────────────────────────────────────────

    def apply_label_operation(self, request: LabelOperationRequest) -> Message:
        """
        Apply a label add/remove operation to a single message.

        Pipeline:
            1. guard.validate_label_operation → raises SafetyViolationError on violation.
            2. Verify the message exists locally.
            3. Call Gmail API modify_message_labels.
            4. Update MessageRepository with the new label set.
            5. Sync the message_labels junction table.
            6. Log the operation (success=True per add/remove label).
            7. Return the updated domain Message entity.

        On any exception after step 3 (API call succeeded but local update
        failed), the audit log is written with success=False so the discrepancy
        is visible for debugging.

        Args:
            request: Validated LabelOperationRequest DTO.

        Returns:
            The updated Message entity reflecting the new label state.

        Raises:
            SafetyViolationError:   If SafetyGuard rejects the operation.
                                    Propagates — NOT caught here.
            ForbiddenOperationError: If GmailClient whitelist rejects the call.
                                    Propagates — NOT caught here.
            LabelOperationError:    If the message does not exist locally or
                                    the API call fails for any other reason.
        """
        # ── Step 1: Safety validation (raises SafetyViolationError if invalid) ─
        # ⚠️  DO NOT catch SafetyViolationError — it must reach the route layer.
        self._guard.validate_label_operation(request)

        # ── Step 2: Verify message exists locally ─────────────────────────────
        existing_message = self._msg_repo.get_by_id(request.message_id)
        if existing_message is None:
            raise LabelOperationError(
                operation="modify",
                message_id=request.message_id,
                label_id=next(
                    iter(request.add_label_ids | request.remove_label_ids), ""
                ),
                reason=(
                    f"Message {request.message_id!r} not found in the local database. "
                    "Run a sync before applying label operations."
                ),
            )

        # ── Step 3: Gmail API call ────────────────────────────────────────────
        api_response = self._client.modify_message_labels(
            request.message_id,
            add_label_ids=list(request.add_label_ids) if request.add_label_ids else None,
            remove_label_ids=(
                list(request.remove_label_ids) if request.remove_label_ids else None
            ),
        )

        # ── Steps 4–6: Persist the new label state; log each changed label ───
        new_label_ids: frozenset[str] = frozenset(api_response.get("labelIds", []))

        try:
            self._msg_repo.update_labels(request.message_id, new_label_ids)
            self._label_repo.sync_message_labels(request.message_id, new_label_ids)

            # Log each label that was added
            for label_id in request.add_label_ids:
                self._label_repo.log_label_operation(
                    message_id=request.message_id,
                    operation="add",
                    label_id=label_id,
                    label_name=self._resolve_label_name(label_id),
                    success=True,
                )

            # Log each label that was removed
            for label_id in request.remove_label_ids:
                self._label_repo.log_label_operation(
                    message_id=request.message_id,
                    operation="remove",
                    label_id=label_id,
                    label_name=self._resolve_label_name(label_id),
                    success=True,
                )

        except Exception as exc:
            # Post-API-call failure: DB update failed. Log the discrepancy so
            # the operator can investigate, then re-raise.
            error_msg = str(exc)
            for label_id in request.add_label_ids | request.remove_label_ids:
                self._label_repo.log_label_operation(
                    message_id=request.message_id,
                    operation="modify",
                    label_id=label_id,
                    label_name=self._resolve_label_name(label_id),
                    success=False,
                    error_message=error_msg,
                )
            raise

        # ── Step 7: Return updated domain entity ──────────────────────────────
        updated = self._msg_repo.get_by_id(request.message_id)
        # get_by_id should always find the row we just upserted
        assert updated is not None, f"Message {request.message_id!r} vanished after update"
        return updated

    def apply_bulk_label_operation(
        self, request: BulkLabelOperationRequest
    ) -> list[Message]:
        """
        Apply the same label operation to every message in the bulk request.

        Expands the bulk request into individual per-message requests and calls
        apply_label_operation for each.  Processing continues even when
        individual messages fail — all errors are collected and re-raised
        together at the end as a LabelOperationError summary.

        Args:
            request: BulkLabelOperationRequest DTO.

        Returns:
            List of updated Message entities for successfully processed messages.

        Raises:
            SafetyViolationError:  If SafetyGuard rejects the bulk operation.
                                   Propagates — NOT caught here.
            LabelOperationError:   If one or more individual operations failed.
                                   The error message lists all failed message IDs.
        """
        # ── Bulk-level safety validation (message count + label count limits) ─
        # ⚠️  DO NOT catch SafetyViolationError.
        self._guard.validate_bulk_label_operation(request)

        individual_requests = request.to_individual_requests()
        results: list[Message] = []
        errors: list[str] = []

        for individual_request in individual_requests:
            try:
                updated = self.apply_label_operation(individual_request)
                results.append(updated)
            except LabelOperationError as exc:
                # Record the failure; continue processing remaining messages
                errors.append(f"[{individual_request.message_id}] {exc!s}")
            # SafetyViolationError and ForbiddenOperationError intentionally
            # propagate immediately — they indicate a systemic problem, not a
            # per-message failure.

        if errors:
            # Raise a summary error after all messages have been attempted
            error_summary = "; ".join(errors)
            raise LabelOperationError(
                operation="bulk_modify",
                message_id=f"{len(errors)}/{request.message_count} messages",
                label_id="(multiple)",
                reason=f"Partial failure in bulk operation: {error_summary}",
            )

        return results

    # ── Private helpers ───────────────────────────────────────────────────────

    def _resolve_label_name(self, label_id: str) -> str:
        """
        Return the human-readable name for a label ID, or the ID itself as fallback.

        The audit log captures a snapshot of the label name at operation time
        so historical log entries remain meaningful even if labels are renamed.

        Args:
            label_id: Gmail label ID to resolve.

        Returns:
            Label display name, or label_id if the label is not in the local DB.
        """
        label = self._label_repo.get_by_id(label_id)
        return label.name if label is not None else label_id
