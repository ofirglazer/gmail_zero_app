"""
SyncService — application-layer orchestration of Gmail sync operations.

Implements two sync strategies:

    FullSyncStrategy    — fetches every message from scratch; used on first
                          run or after a stale historyId forces a resync.

    IncrementalSyncStrategy — fetches only changes since the last known
                               historyId; used for routine background syncs.

Both strategies share the same pipeline shape:
    1. Fetch message IDs (paginated)
    2. Batch-fetch full metadata
    3. Map API dicts → domain entities
    4. Upsert to the local DB (committed in batches to cap transaction size)
    5. Sync label registry
    6. Record a SyncState watermark
    7. Write a DailySnapshot for the dashboard graphs

Design:
    SyncService is stateless per request — it holds only injected dependencies.
    A ``session`` reference is required because the service commits per batch
    to cap transaction size on large initial syncs.  Repositories never commit
    themselves; this service is the explicit owner of that responsibility.

    Rate limiting is applied between batch API calls via
    ``settings.sync_rate_limit_delay_ms`` to stay within Gmail's quota.

⚠️  INVARIANT: Never call batch_get_messages across more than
    settings.sync_batch_size IDs per call; sleep between calls.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from domain.exceptions import IncrementalSyncError
from domain.models.daily_snapshot import DailySnapshot
from domain.models.sync_state import SyncState, SyncType
from domain.models.thread import Thread

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from config.settings import Settings
    from infrastructure.gmail.client import AbstractGmailClient
    from infrastructure.gmail.mapper import GmailMapper
    from infrastructure.persistence.repositories.label_repository import LabelRepository
    from infrastructure.persistence.repositories.message_repository import MessageRepository
    from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
    from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository


class SyncService:
    """
    Orchestrates full and incremental Gmail sync pipelines.

    Args:
        client:     Gmail client (real or mock) satisfying AbstractGmailClient.
        mapper:     Converts raw Gmail API dicts to domain entities.
        msg_repo:   Message persistence repository.
        label_repo: Label and audit-log persistence repository.
        sync_repo:  Sync-state watermark repository.
        snap_repo:  Daily snapshot repository.
        settings:   Application settings (batch sizes, rate limits, etc.).
        session:    The open SQLAlchemy session.  SyncService calls
                    ``session.commit()`` at the end of each message batch to
                    cap transaction size.  The caller's ``get_session`` context
                    manager owns the final commit/rollback.
    """

    def __init__(
        self,
        client: AbstractGmailClient,
        mapper: GmailMapper,
        msg_repo: MessageRepository,
        label_repo: LabelRepository,
        sync_repo: SyncStateRepository,
        snap_repo: SnapshotRepository,
        settings: Settings,
        session: Session,
    ) -> None:
        self._client = client
        self._mapper = mapper
        self._msg_repo = msg_repo
        self._label_repo = label_repo
        self._sync_repo = sync_repo
        self._snap_repo = snap_repo
        self._settings = settings
        self._session = session

    # ── Public sync entry points ──────────────────────────────────────────────

    def run_full_sync(self) -> SyncState:
        """
        Fetch all Gmail messages from scratch and populate the local database.

        Pipeline:
            1. Paginate through list_messages until no nextPageToken remains.
            2. Batch-fetch full message metadata; map → domain entities; upsert.
               Commits after each batch to cap transaction size.
            3. Fetch and upsert the complete label registry.
            4. Call get_profile to obtain the current historyId high-water mark.
            5. Persist a SyncState(FULL) watermark.
            6. Write a DailySnapshot.

        Returns:
            The persisted SyncState for this run.
        """
        total_synced: int = 0

        # ── Step 1 & 2: Paginated list → batch-get → upsert ──────────────────
        page_token: str | None = None
        while True:
            list_response = self._client.list_messages(
                max_results=500,
                page_token=page_token,
            )
            message_stubs: list[dict] = list_response.get("messages", [])

            # Process the page in sub-batches of sync_batch_size
            for batch_start in range(0, len(message_stubs), self._settings.sync_batch_size):
                batch_ids = [
                    m["id"]
                    for m in message_stubs[batch_start: batch_start + self._settings.sync_batch_size]
                ]
                api_dicts = self._client.batch_get_messages(batch_ids)
                messages = [self._mapper.api_dict_to_message(d) for d in api_dicts]

                # Threads must exist before messages due to the FK constraint
                # on messages.thread_id → threads.id.  Upsert minimal stubs
                # derived directly from the mapped Message entities.
                threads = self._build_thread_stubs(messages)
                self._sync_repo.upsert_threads(threads)

                self._msg_repo.upsert_many(messages)
                total_synced += len(messages)

                # Commit each batch independently to avoid giant transactions
                self._session.commit()

                # Respect Gmail quota between batch API calls
                self._rate_limit_sleep()

            page_token = list_response.get("nextPageToken")
            if not page_token:
                break  # All pages consumed

        # ── Step 3: Sync label registry ───────────────────────────────────────
        labels_response = self._client.list_labels()
        labels = self._mapper.api_dict_to_labels(labels_response)
        self._label_repo.upsert_many(labels)
        self._session.commit()

        # ── Step 4: Capture historyId high-water mark ─────────────────────────
        profile = self._client.get_profile()
        history_id: str = profile["historyId"]

        # ── Step 5: Persist sync state watermark ──────────────────────────────
        now = datetime.now(tz=UTC)
        state = self._sync_repo.save(
            SyncState(
                id=None,
                sync_type=SyncType.FULL,
                history_id=history_id,
                messages_synced=total_synced,
                last_synced_at=now,
                created_at=now,
            )
        )
        self._session.commit()

        # ── Step 6: Write dashboard snapshot ─────────────────────────────────
        self._write_snapshot()

        return state

    def run_incremental_sync(self) -> SyncState:
        """
        Fetch only changes since the last recorded historyId.

        Falls back to a full sync automatically when no prior sync state
        is found in the database.

        Pipeline:
            1. Retrieve the last SyncState to obtain the start historyId.
            2. Paginate through get_history to collect all change events.
            3. Extract added and modified message IDs from history events.
            4. Re-fetch changed messages; map and upsert.
            5. Persist a SyncState(INCREMENTAL) watermark.
            6. Write a DailySnapshot.

        Raises:
            IncrementalSyncError: When Gmail signals that the stored historyId
                has expired (typically after ~7 days of no sync).  The caller
                should catch this and call ``run_full_sync()`` instead.

        Returns:
            The persisted SyncState for this run.
        """
        last_state = self._sync_repo.latest()

        # No prior sync — fall back to a full sync
        if last_state is None:
            return self.run_full_sync()

        start_history_id: str = last_state.history_id
        all_added_ids: set[str] = set()
        all_changed_ids: set[str] = set()
        final_history_id: str = start_history_id

        # ── Step 2: Paginated history fetch ───────────────────────────────────
        page_token: str | None = None
        try:
            while True:
                history_response = self._client.get_history(
                    start_history_id=start_history_id,
                    history_types=["messageAdded", "labelAdded", "labelRemoved"],
                    max_results=500,
                    page_token=page_token,
                )
                # Track the advancing historyId high-water mark
                final_history_id = history_response.get("historyId", start_history_id)

                # ── Step 3: Extract changed message IDs ───────────────────────
                added_ids, changed_ids = self._mapper.extract_changed_message_ids(
                    history_response
                )
                all_added_ids.update(added_ids)
                all_changed_ids.update(changed_ids)

                page_token = history_response.get("nextPageToken")
                if not page_token:
                    break

        except Exception as exc:
            # Gmail returns a 404 / "Start history id ... is not in the past"
            # when the historyId has expired.  Surface as IncrementalSyncError
            # so the caller can schedule a full resync.
            raise IncrementalSyncError(history_id=start_history_id) from exc

        # ── Step 4: Re-fetch all changed messages ─────────────────────────────
        all_changed: set[str] = all_added_ids | all_changed_ids
        total_synced: int = 0

        changed_list = sorted(all_changed)  # sorted for determinism
        for batch_start in range(0, len(changed_list), self._settings.sync_batch_size):
            batch_ids = changed_list[batch_start: batch_start + self._settings.sync_batch_size]
            api_dicts = self._client.batch_get_messages(batch_ids)
            messages = [self._mapper.api_dict_to_message(d) for d in api_dicts]

            # Satisfy the FK (Foreign Key) constraint: threads must pre-exist messages
            threads = self._build_thread_stubs(messages)
            self._sync_repo.upsert_threads(threads)

            self._msg_repo.upsert_many(messages)
            total_synced += len(messages)
            self._session.commit()
            self._rate_limit_sleep()

        # ── Step 5: Persist sync state watermark ──────────────────────────────
        now = datetime.now(tz=UTC)
        state = self._sync_repo.save(
            SyncState(
                id=None,
                sync_type=SyncType.INCREMENTAL,
                history_id=final_history_id,
                messages_synced=total_synced,
                last_synced_at=now,
                created_at=now,
            )
        )
        self._session.commit()

        # ── Step 6: Write dashboard snapshot ─────────────────────────────────
        self._write_snapshot()

        return state

    # ── Private helpers ───────────────────────────────────────────────────────

    def _write_snapshot(self) -> None:
        """
        Compute and upsert today's DailySnapshot from current repository counts.

        Called at the end of every successful sync (full or incremental) so
        the dashboard graphs always reflect the post-sync state.
        """
        snapshot = DailySnapshot(
            snapshot_date=date.today(),
            inbox_count=self._msg_repo.count_inbox(),
            inbox_size_bytes=self._msg_repo.inbox_size_bytes(),
            archive_unlabelled_count=self._msg_repo.count_archive_unlabelled(),
            sent_unresolved_count=self._msg_repo.count_sent_unresolved(),
            total_size_bytes=self._msg_repo.total_size_bytes(),
            custom_label_coverage_pct=self._msg_repo.custom_label_coverage_pct(),
        )
        self._snap_repo.upsert(snapshot)
        self._session.commit()

    def _build_thread_stubs(self, messages: list) -> list[Thread]:
        """
        Derive minimal Thread stubs from a batch of mapped Message entities.

        The ``messages`` table has a FOREIGN KEY on ``thread_id → threads.id``.
        A stub must be upserted for every distinct thread_id before the
        messages themselves are inserted.

        Stub values are conservative: subject/snippet/message_count are taken
        from the newest message in each thread group within the batch.  The
        authoritative thread record is updated by the full mapper when a
        dedicated threads fetch runs (future step); stubs satisfy the FK now.

        Args:
            messages: Batch of domain Message entities about to be upserted.

        Returns:
            One Thread stub per distinct thread_id in the batch.
        """
        now = datetime.now(tz=UTC)
        # Group by thread_id; pick the message with the latest internal_date
        # as the representative for subject/snippet
        best: dict[str, object] = {}
        for msg in messages:
            existing = best.get(msg.thread_id)
            if existing is None or msg.internal_date > existing.internal_date:  # type: ignore[union-attr]
                best[msg.thread_id] = msg

        return [
            Thread(
                id=msg.thread_id,  # type: ignore[union-attr]
                subject=msg.subject,  # type: ignore[union-attr]
                message_count=1,      # stub; not authoritative
                snippet=msg.snippet,  # type: ignore[union-attr]
                last_message_at=msg.internal_date,  # type: ignore[union-attr]
                is_inbox=msg.is_inbox,  # type: ignore[union-attr]
                has_custom_label=msg.has_custom_label,  # type: ignore[union-attr]
                last_synced_at=now,
            )
            for msg in best.values()
        ]

    def _rate_limit_sleep(self) -> None:
        """
        Sleep between batch API calls to respect Gmail's rate limits.

        Gmail quota: 250 units/user/second; each messages.get costs 5 units.
        The default delay (50 ms) allows ~20 batch calls per second safely.
        """
        delay_secs = self._settings.sync_rate_limit_delay_ms / 1000
        if delay_secs > 0:
            time.sleep(delay_secs)
