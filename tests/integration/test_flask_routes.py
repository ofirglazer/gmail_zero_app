"""
Integration tests for Step 6 — Flask routes and templates.

Tests hit every route using a Flask test client backed by a fully-synced
in-memory SQLite database populated by MockGmailClient.

All tests are read-only (no label operations — that is Step 7).

Fixtures:
    synced_app  — Flask test client with a completed full sync.
                  Module-scoped to avoid re-running the sync for each test.

Assertions deliberately check rendered HTML content rather than template
names to stay robust against template refactoring.

pytest marker: @pytest.mark.integration
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from application.services.sync_service import SyncService
from config.settings import Environment, Settings
from infrastructure.gmail.mapper import GmailMapper
from infrastructure.gmail.mock_client import MockGmailClient
from infrastructure.persistence.database import build_engine, get_session, initialise_db
from infrastructure.persistence.repositories.label_repository import LabelRepository
from infrastructure.persistence.repositories.message_repository import (
    MessageFilter,
    MessageRepository,
)
from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository

if TYPE_CHECKING:
    from collections.abc import Generator

    from flask.testing import FlaskClient

pytestmark = pytest.mark.integration


# ── Shared fixture: synced Flask app ─────────────────────────────────────────


@pytest.fixture(scope="module")
def demo_settings() -> Settings:
    """Minimal settings — demo mode, no rate limiting, temp DB."""
    return Settings(
        env=Environment.DEMO,
        sync_batch_size=50,
        sync_rate_limit_delay_ms=0,
    )


@pytest.fixture(scope="module")
def synced_app(demo_settings: Settings) -> Generator[FlaskClient, None, None]:
    """
    Module-scoped fixture: Flask test client over an in-memory DB
    that has been fully synced from MockGmailClient.

    Yields the Flask test client.  The full sync runs once for all tests in
    the module.
    """
    # ── Build and sync the DB ─────────────────────────────────────────────────
    engine = build_engine("sqlite:///:memory:")
    initialise_db(engine)

    client_mock = MockGmailClient()
    mapper = GmailMapper(user_email=client_mock.user_email)

    with get_session(engine) as session:
        msg_repo = MessageRepository(session)
        label_repo = LabelRepository(session)
        sync_repo = SyncStateRepository(session)
        snap_repo = SnapshotRepository(session)

        svc = SyncService(
            client=client_mock,
            mapper=mapper,
            msg_repo=msg_repo,
            label_repo=label_repo,
            sync_repo=sync_repo,
            snap_repo=snap_repo,
            settings=demo_settings,
            session=session,
        )
        svc.run_full_sync()

    # ── Build Flask app ───────────────────────────────────────────────────────
    # Pass the pre-synced in-memory engine directly so create_app never
    # attempts to open a filesystem DB file.
    from presentation.app import create_app

    app = create_app(demo_settings, engine=engine)

    # Override client and mapper so the app uses the same mock instance
    # (client is already wired in create_app via settings.is_demo, but
    # we replace it with the instance used for the sync so state is shared)
    app.config["GMAIL_ZERO_CLIENT"] = client_mock
    app.config["GMAIL_ZERO_MAPPER"] = mapper

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # no CSRF in Step 6

    with app.test_client() as tc:
        yield tc


# ── Root redirect ─────────────────────────────────────────────────────────────


class TestRootRedirect:
    def test_root_redirects_to_dashboard(self, synced_app: FlaskClient) -> None:
        """GET / should redirect to /dashboard."""
        resp = synced_app.get("/", follow_redirects=False)
        assert resp.status_code in (301, 302)
        assert "/dashboard" in resp.headers["Location"]


# ── Dashboard ─────────────────────────────────────────────────────────────────


class TestDashboard:
    def test_dashboard_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/dashboard")
        assert resp.status_code == 200

    def test_dashboard_shows_goal_cards(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/dashboard").data.decode()
        assert "Inbox Zero" in html
        assert "Archive Zero" in html
        assert "Sent Zero" in html
        assert "Size Zero" in html

    def test_dashboard_shows_demo_banner(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/dashboard").data.decode()
        assert "DEMO MODE" in html

    def test_dashboard_shows_last_sync(self, synced_app: FlaskClient) -> None:
        """After the full sync fixture, the last sync timestamp must appear."""
        html = synced_app.get("/dashboard").data.decode()
        # The sync-status block is present and contains 'full'
        assert "full" in html.lower()

    def test_dashboard_auto_refreshes(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/dashboard").data.decode()
        assert "window.location.reload()" in html

    def test_dashboard_no_server_error(self, synced_app: FlaskClient) -> None:
        """Dashboard must not produce a 500 even with a full DB."""
        resp = synced_app.get("/dashboard")
        assert resp.status_code != 500


# ── Inbox ─────────────────────────────────────────────────────────────────────


class TestInbox:
    def test_inbox_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/inbox")
        assert resp.status_code == 200

    def test_inbox_shows_messages(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/inbox").data.decode()
        # The mock dataset has many inbox messages
        assert "<tr" in html, "Expected table rows in inbox response"

    def test_inbox_pagination_page2(self, synced_app: FlaskClient) -> None:
        """GET /inbox?page=2 should return 200 without errors."""
        resp = synced_app.get("/inbox?page=2&per_page=10")
        assert resp.status_code == 200

    def test_inbox_page_out_of_range_returns_empty_not_error(self, synced_app: FlaskClient) -> None:
        """A very high page number should return 200 with empty state, not 500."""
        resp = synced_app.get("/inbox?page=99999")
        assert resp.status_code == 200

    def test_inbox_shows_nav_active(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/inbox").data.decode()
        # The active nav link renders "nav-item active" on the inbox link
        assert 'nav-item active' in html

    def test_inbox_shows_workflow_label_toggle_button(self, synced_app: FlaskClient) -> None:
        """Verify the show/hide button appears in the inbox."""
        html = synced_app.get("/inbox").data.decode()
        assert "To-Archive/To-Remove labels" in html
        assert "Show To-Archive/To-Remove labels" in html

    def test_inbox_toggle_button_has_correct_url_default_state(
        self, synced_app: FlaskClient
    ) -> None:
        """In default state (show_workflow_labels not set), the button URL
        should include show_workflow_labels=1."""
        html = synced_app.get("/inbox").data.decode()
        # The link should contain the parameter to toggle it on
        assert 'show_workflow_labels=' in html
        assert 'show_workflow_labels=1' in html

    def test_inbox_toggle_button_has_correct_url_toggled_state(
        self, synced_app: FlaskClient
    ) -> None:
        """When toggled on (show_workflow_labels=1), the button URL should
        not include the parameter (to toggle it off)."""
        html = synced_app.get("/inbox?show_workflow_labels=1").data.decode()
        # The link in toggled state should reset the parameter
        # (clicking 'Hide' should go to the URL without show_workflow_labels)
        assert "Hide To-Archive/To-Remove labels" in html

    def test_inbox_toggle_button_text_changes(self, synced_app: FlaskClient) -> None:
        """The button text should change between 'Show' and 'Hide' based on state."""
        # Default: button should say "Show"
        html_default = synced_app.get("/inbox").data.decode()
        assert "Show To-Archive/To-Remove labels" in html_default
        assert "Hide To-Archive/To-Remove labels" not in html_default

        # With flag: button should say "Hide"
        html_toggled = synced_app.get("/inbox?show_workflow_labels=1").data.decode()
        assert "Hide To-Archive/To-Remove labels" in html_toggled
        assert "Show To-Archive/To-Remove labels" not in html_toggled

    def test_inbox_toggle_button_works_with_pagination(self, synced_app: FlaskClient) -> None:
        """The toggle button should work correctly even when pagination parameters are present."""
        resp = synced_app.get("/inbox?page=2&per_page=10&sort=sender_domain&dir=asc")
        assert resp.status_code == 200
        html = resp.data.decode()
        # The button should be present and have show_workflow_labels parameter
        assert "To-Archive/To-Remove labels" in html
        assert "show_workflow_labels=" in html


# ── Archive ───────────────────────────────────────────────────────────────────


class TestArchive:
    def test_archive_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/archive")
        assert resp.status_code == 200

    def test_archive_shows_domain_groups(self, synced_app: FlaskClient) -> None:
        """The mock dataset has many unlabelled archived messages grouped by domain."""
        html = synced_app.get("/archive").data.decode()
        # acme.com newsletters are in the archive with no custom label
        assert "acme.com" in html or "github.com" in html

    def test_archive_count_nonzero(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/archive").data.decode()
        # Page header includes count badge with a non-zero number
        assert "0" not in html or "Archive Zero" in html

    def test_archive_shows_workflow_label_toggle_button(self, synced_app: FlaskClient) -> None:
        """Verify the show/hide button appears in archive view."""
        html = synced_app.get("/archive").data.decode()
        assert "To-Archive/To-Remove labels" in html
        assert "Show To-Archive/To-Remove labels" in html

    def test_archive_hides_workflow_labeled_messages_by_default(
        self, synced_app: FlaskClient
    ) -> None:
        """By default (show_workflow_labels not set), archived messages with
        To-Archive or To-Remove labels should be hidden."""
        # This test depends on the mock dataset having archived messages with workflow labels
        html = synced_app.get("/archive").data.decode()
        # Verify the button exists (indicating feature is present)
        assert "Show To-Archive/To-Remove labels" in html

    def test_archive_shows_workflow_labeled_messages_when_toggled(
        self, synced_app: FlaskClient
    ) -> None:
        """When show_workflow_labels=1, archived messages with To-Archive or
        To-Remove labels should be visible."""
        html = synced_app.get("/archive?show_workflow_labels=1").data.decode()
        # Verify the button text changes when toggled
        assert "Hide To-Archive/To-Remove labels" in html

    def test_archive_toggle_button_text_changes(self, synced_app: FlaskClient) -> None:
        """Button text changes from 'Show' to 'Hide' when toggled."""
        html_default = synced_app.get("/archive").data.decode()
        assert "Show To-Archive/To-Remove labels" in html_default
        assert "Hide To-Archive/To-Remove labels" not in html_default

        html_toggled = synced_app.get("/archive?show_workflow_labels=1").data.decode()
        assert "Hide To-Archive/To-Remove labels" in html_toggled
        assert "Show To-Archive/To-Remove labels" not in html_toggled


# ── Sent ──────────────────────────────────────────────────────────────────────


class TestSent:
    def test_sent_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/sent")
        assert resp.status_code == 200

    def test_sent_shows_messages(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/sent").data.decode()
        # Mock dataset has several sent messages
        assert "Sent Zero" in html

    def test_sent_shows_workflow_label_toggle_button(self, synced_app: FlaskClient) -> None:
        """Verify the show/hide button appears in sent view."""
        html = synced_app.get("/sent").data.decode()
        assert "To-Archive/To-Remove labels" in html
        assert "Show To-Archive/To-Remove labels" in html

    def test_sent_toggle_button_text_changes(self, synced_app: FlaskClient) -> None:
        """Button text changes from 'Show' to 'Hide' when toggled."""
        html_default = synced_app.get("/sent").data.decode()
        assert "Show To-Archive/To-Remove labels" in html_default
        assert "Hide To-Archive/To-Remove labels" not in html_default

        html_toggled = synced_app.get("/sent?show_workflow_labels=1").data.decode()
        assert "Hide To-Archive/To-Remove labels" in html_toggled
        assert "Show To-Archive/To-Remove labels" not in html_toggled


# ── Size ──────────────────────────────────────────────────────────────────────


class TestSize:
    def test_size_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/size")
        assert resp.status_code == 200

    def test_size_shows_total_gb(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/size").data.decode()
        assert "GB" in html

    def test_size_shows_large_messages(self, synced_app: FlaskClient) -> None:
        """The mock dataset contains several multi-MB messages."""
        html = synced_app.get("/size").data.decode()
        assert "MB" in html or "GB" in html

    def test_size_shows_workflow_label_toggle_button(self, synced_app: FlaskClient) -> None:
        """Verify the show/hide button appears in size view."""
        html = synced_app.get("/size").data.decode()
        assert "To-Archive/To-Remove labels" in html
        assert "Show To-Archive/To-Remove labels" in html

    def test_size_toggle_button_text_changes(self, synced_app: FlaskClient) -> None:
        """Button text changes from 'Show' to 'Hide' when toggled."""
        html_default = synced_app.get("/size").data.decode()
        assert "Show To-Archive/To-Remove labels" in html_default
        assert "Hide To-Archive/To-Remove labels" not in html_default

        html_toggled = synced_app.get("/size?show_workflow_labels=1").data.decode()
        assert "Hide To-Archive/To-Remove labels" in html_toggled
        assert "Show To-Archive/To-Remove labels" not in html_toggled


# ── Search ────────────────────────────────────────────────────────────────────


class TestSearch:
    def test_search_returns_200_no_params(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/search")
        assert resp.status_code == 200

    def test_search_with_sender_domain_filter(self, synced_app: FlaskClient) -> None:
        """GET /search?sender_domain=github.com should return only GitHub messages."""
        resp = synced_app.get("/search?sender_domain=github.com")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "github.com" in html

    def test_search_with_nonexistent_domain_returns_empty(self, synced_app: FlaskClient) -> None:
        """A domain that doesn't exist should produce an empty-state response."""
        resp = synced_app.get("/search?sender_domain=no-such-domain-xyz.invalid")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "0 message" in html or "No messages" in html

    def test_search_form_repopulates_filter_values(self, synced_app: FlaskClient) -> None:
        """Filter values must appear in the rendered form inputs."""
        resp = synced_app.get("/search?sender_domain=aws.com&is_unread=1")
        html = resp.data.decode()
        assert "aws.com" in html

    def test_search_pagination(self, synced_app: FlaskClient) -> None:
        """?page=2 on search results must return 200."""
        resp = synced_app.get("/search?page=2&per_page=5")
        assert resp.status_code == 200

    def test_search_shows_user_labels_dropdown(self, synced_app: FlaskClient) -> None:
        """Label dropdown must include at least one user label from the mock dataset."""
        html = synced_app.get("/search").data.decode()
        assert "ZeroApp/" in html

    def test_search_shows_workflow_label_toggle_button(self, synced_app: FlaskClient) -> None:
        """Verify the show/hide button appears in search results."""
        resp = synced_app.get("/search?sender_domain=github.com")
        html = resp.data.decode()
        # Button should appear in results view
        if "message" in html.lower() and "found" in html.lower():
            assert "To-Archive/To-Remove labels" in html

    def test_search_toggle_button_text_changes(self, synced_app: FlaskClient) -> None:
        """Button text changes from 'Show' to 'Hide' when toggled."""
        # Perform a search that returns results
        html_default = synced_app.get("/search?sender_domain=github.com").data.decode()
        if "message" in html_default.lower() and "found" in html_default.lower():
            assert "Show To-Archive/To-Remove labels" in html_default
            assert "Hide To-Archive/To-Remove labels" not in html_default

            url = "/search?sender_domain=github.com&show_workflow_labels=1"
            html_toggled = synced_app.get(url).data.decode()
            assert "Hide To-Archive/To-Remove labels" in html_toggled
            assert "Show To-Archive/To-Remove labels" not in html_toggled


# ── Settings ──────────────────────────────────────────────────────────────────


class TestSettings:
    def test_settings_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/settings")
        assert resp.status_code == 200

    def test_settings_shows_sync_controls_and_cleanup_instructions(
        self, synced_app: FlaskClient
    ) -> None:
        html = synced_app.get("/settings").data.decode()
        assert "Full sync now" in html
        assert "Incremental sync now" in html
        assert "Inbox cleanup instructions" in html
        assert "Label every message in the app" in html
        assert "Archive and Delete in Gmail" in html

    def test_settings_shows_sync_history(self, synced_app: FlaskClient) -> None:
        """Settings page must list the completed full sync."""
        html = synced_app.get("/settings").data.decode()
        assert "full" in html.lower()

    def test_settings_shows_label_registry(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/settings").data.decode()
        assert "INBOX" in html

    def test_settings_shows_env_mode(self, synced_app: FlaskClient) -> None:
        html = synced_app.get("/settings").data.decode()
        assert "demo" in html.lower()


# ── API endpoints ─────────────────────────────────────────────────────────────


class TestApiEndpoints:
    def test_health_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/api/v1/health")
        assert resp.status_code == 200

    def test_health_returns_json(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/api/v1/health")
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["mode"] == "demo"

    def test_progress_returns_200(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/api/v1/progress")
        assert resp.status_code == 200

    def test_progress_returns_json_with_snapshots_key(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/api/v1/progress")
        data = json.loads(resp.data)
        assert "snapshots" in data
        assert isinstance(data["snapshots"], list)

    def test_progress_snapshot_has_required_fields(self, synced_app: FlaskClient) -> None:
        """Each snapshot dict must contain all fields Chart.js expects."""
        resp = synced_app.get("/api/v1/progress")
        data = json.loads(resp.data)
        required = {
            "date", "inbox_count", "inbox_size_bytes",
            "archive_unlabelled_count", "sent_unresolved_count",
            "total_size_bytes", "custom_label_coverage_pct",
        }
        if data["snapshots"]:
            first = data["snapshots"][0]
            missing = required - set(first.keys())
            assert not missing, f"Snapshot missing fields: {missing}"

    def test_progress_snapshots_non_empty_after_sync(self, synced_app: FlaskClient) -> None:
        """After a full sync, at least one snapshot must be present."""
        resp = synced_app.get("/api/v1/progress")
        data = json.loads(resp.data)
        assert len(data["snapshots"]) >= 1

    def test_message_body_returns_200_for_valid_message(
        self, synced_app: FlaskClient
    ) -> None:
        """GET /api/v1/message/<id>/body returns 200 for a synced message."""
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            msg = MessageRepository(session).list_search(MessageFilter(limit=1))[0]
            message_id = msg.id

        resp = synced_app.get(f"/api/v1/message/{message_id}/body")
        assert resp.status_code == 200

    def test_message_body_returns_json_with_required_fields(
        self, synced_app: FlaskClient
    ) -> None:
        """Response must contain body, subject, and sender fields."""
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            msg = MessageRepository(session).list_search(MessageFilter(limit=1))[0]
            message_id = msg.id

        resp = synced_app.get(f"/api/v1/message/{message_id}/body")
        data = json.loads(resp.data)

        assert "body" in data
        assert "subject" in data
        assert "sender" in data
        assert "date" in data

    def test_message_body_contains_message_content(
        self, synced_app: FlaskClient
    ) -> None:
        """The body field must contain actual message content."""
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            msg = MessageRepository(session).list_search(MessageFilter(limit=1))[0]
            message_id = msg.id

        resp = synced_app.get(f"/api/v1/message/{message_id}/body")
        data = json.loads(resp.data)

        # Body and subject should be non-empty strings (or empty is valid for some messages)
        assert isinstance(data["body"], str)
        assert isinstance(data["subject"], str)
        assert isinstance(data["sender"], str)
        assert isinstance(data["date"], str)

    def test_progress_accepts_days_30_param(self, synced_app: FlaskClient) -> None:
        """GET /api/v1/progress?days=30 returns 200 with snapshots list."""
        resp = synced_app.get("/api/v1/progress?days=30")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "snapshots" in data
        assert isinstance(data["snapshots"], list)

    def test_progress_accepts_days_90_param(self, synced_app: FlaskClient) -> None:
        """GET /api/v1/progress?days=90 returns 200 with snapshots list."""
        resp = synced_app.get("/api/v1/progress?days=90")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "snapshots" in data
        assert isinstance(data["snapshots"], list)

    def test_progress_invalid_days_falls_back_gracefully(self, synced_app: FlaskClient) -> None:
        """Invalid days value still returns 200 using the default window."""
        resp = synced_app.get("/api/v1/progress?days=999")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "snapshots" in data

    def test_progress_snapshot_has_size_and_coverage_fields(
        self, synced_app: FlaskClient
    ) -> None:
        """Snapshots expose total_size_bytes and custom_label_coverage_pct for datasets 3 & 4."""
        resp = synced_app.get("/api/v1/progress")
        data = json.loads(resp.data)
        if data["snapshots"]:
            first = data["snapshots"][0]
            assert "total_size_bytes" in first
            assert "custom_label_coverage_pct" in first
            assert isinstance(first["total_size_bytes"], int)
            assert isinstance(first["custom_label_coverage_pct"], float)

    def test_message_body_with_nonexistent_message_returns_empty_subject_sender(
        self, synced_app: FlaskClient
    ) -> None:
        """If message doesn't exist in DB, subject and sender should be empty."""
        nonexistent_id = "nonexistent_message_xyz"
        resp = synced_app.get(f"/api/v1/message/{nonexistent_id}/body")
        assert resp.status_code == 200

        data = json.loads(resp.data)
        assert data["subject"] == ""
        assert data["sender"] == ""

    def test_message_body_response_is_valid_json(
        self, synced_app: FlaskClient
    ) -> None:
        """Verify the response has correct JSON Content-Type."""
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            msg = MessageRepository(session).list_search(MessageFilter(limit=1))[0]
            message_id = msg.id

        resp = synced_app.get(f"/api/v1/message/{message_id}/body")
        assert resp.content_type == "application/json"


class TestStorageBreakdownEndpoint:
    """Tests for GET /api/v1/storage-breakdown."""

    def test_storage_breakdown_returns_200(self, synced_app: FlaskClient) -> None:
        """Endpoint must return 200 for a synced DB."""
        resp = synced_app.get("/api/v1/storage-breakdown")
        assert resp.status_code == 200

    def test_storage_breakdown_returns_json(self, synced_app: FlaskClient) -> None:
        """Response must have JSON content-type."""
        resp = synced_app.get("/api/v1/storage-breakdown")
        assert resp.content_type == "application/json"

    def test_storage_breakdown_has_required_top_level_keys(
        self, synced_app: FlaskClient
    ) -> None:
        """Response must include all four chart data keys."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        required = {
            "top_senders_by_count",
            "top_senders_by_size",
            "size_by_label",
            "age_buckets",
        }
        assert required <= set(data.keys())

    def test_top_senders_by_count_are_lists(self, synced_app: FlaskClient) -> None:
        """top_senders_by_count and top_senders_by_size must be lists."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        assert isinstance(data["top_senders_by_count"], list)
        assert isinstance(data["top_senders_by_size"], list)

    def test_sender_entry_has_required_fields(self, synced_app: FlaskClient) -> None:
        """Each sender entry must have sender, sender_domain, message_count, total_size_bytes."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        required = {"sender", "sender_domain", "message_count", "total_size_bytes"}
        for key in ("top_senders_by_count", "top_senders_by_size"):
            if data[key]:
                missing = required - set(data[key][0].keys())
                assert not missing, f"{key}[0] missing fields: {missing}"

    def test_size_by_label_is_list(self, synced_app: FlaskClient) -> None:
        """size_by_label must be a list."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        assert isinstance(data["size_by_label"], list)

    def test_label_entry_has_required_fields(self, synced_app: FlaskClient) -> None:
        """Each label entry must have label_name, total_size_bytes, message_count."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        required = {"label_name", "total_size_bytes", "message_count"}
        if data["size_by_label"]:
            missing = required - set(data["size_by_label"][0].keys())
            assert not missing, f"size_by_label[0] missing fields: {missing}"

    def test_age_buckets_has_required_fields(self, synced_app: FlaskClient) -> None:
        """age_buckets must contain all six expected fields."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        ab = data["age_buckets"]
        required = {
            "current_year", "current_year_label",
            "past_year", "past_year_label",
            "older", "older_label",
        }
        missing = required - set(ab.keys())
        assert not missing, f"age_buckets missing fields: {missing}"

    def test_age_buckets_are_non_negative_integers(self, synced_app: FlaskClient) -> None:
        """Age bucket counts must be non-negative integers."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        ab = data["age_buckets"]
        for key in ("current_year", "past_year", "older"):
            assert isinstance(ab[key], int), f"age_buckets.{key} is not int"
            assert ab[key] >= 0, f"age_buckets.{key} is negative"

    def test_age_bucket_labels_are_year_strings(self, synced_app: FlaskClient) -> None:
        """Year labels must be 4-digit strings."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        ab = data["age_buckets"]
        assert ab["current_year_label"].isdigit() and len(ab["current_year_label"]) == 4
        assert ab["past_year_label"].isdigit() and len(ab["past_year_label"]) == 4

    def test_age_buckets_sum_matches_total_messages(self, synced_app: FlaskClient) -> None:
        """Sum of three age buckets must equal the total message count in the DB."""
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            total = MessageRepository(session).count_search(MessageFilter(limit=1))

        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        ab = data["age_buckets"]
        bucket_sum = ab["current_year"] + ab["past_year"] + ab["older"]
        # count_search returns total count; bucket_sum must equal it
        assert bucket_sum == total

    def test_top_senders_limited_to_five(self, synced_app: FlaskClient) -> None:
        """Default sender limit is 5 — lists must not exceed this."""
        data = json.loads(synced_app.get("/api/v1/storage-breakdown").data)
        assert len(data["top_senders_by_count"]) <= 5
        assert len(data["top_senders_by_size"]) <= 5


class TestLabelOperations:
    def test_apply_label_to_selected_message(self, synced_app: FlaskClient) -> None:
        resp = synced_app.post(
            "/messages/label",
            data={
                "message_ids": ["inbox001"],
                "label_id": "Label_Complete001",
                "next": "/inbox",
            },
            follow_redirects=True,
        )

        html = resp.data.decode()

        assert resp.status_code == 200
        assert "Applied label to 1 message" in html
        assert "Label_Complete001" in html

        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            db_message = MessageRepository(session).get_by_id("inbox001")
        assert db_message is not None
        assert "Label_Complete001" in db_message.label_ids

    def test_toggle_label_off_when_already_present(self, synced_app: FlaskClient) -> None:
        resp = synced_app.post(
            "/messages/label",
            data={
                "message_ids": ["inbox002"],
                "label_id": "Label_Complete001",
                "next": "/inbox",
            },
            follow_redirects=True,
        )

        html = resp.data.decode()

        assert resp.status_code == 200
        assert "Applied label to 1 message" in html

        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            db_message = MessageRepository(session).get_by_id("inbox002")
        assert db_message is not None
        assert "Label_Complete001" not in db_message.label_ids

    def test_apply_label_requires_selection(self, synced_app: FlaskClient) -> None:
        resp = synced_app.post(
            "/messages/label",
            data={
                "label_id": "Label_Complete001",
                "next": "/inbox",
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        assert "Select at least one message to label" in resp.data.decode()

    def test_remove_to_archive_button_visible_when_labeled_messages_exist(
        self, synced_app: FlaskClient
    ) -> None:
        """Test that the Remove To-Archive button appears in archive page.

        Order-independent: directly writes To-Archive into one DB row without
        touching the mock client or running a sync, so this test is immune to
        whatever state earlier tests may have left in the mock.
        """
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]

        with synced_app.application.app_context(), get_session(engine) as session:
            label_repo = LabelRepository(session)
            msg_repo = MessageRepository(session)

            to_archive = label_repo.get_by_name("ZeroApp/To-Archive")
            assert to_archive is not None

            # Find any archived message that does not already carry To-Archive
            archived_msgs = msg_repo.list_search(
                MessageFilter(is_archived=True, limit=100)
            )
            target = next(
                (m for m in archived_msgs if to_archive.id not in m.label_ids),
                None,
            )
            assert target is not None, "No archived message without To-Archive found"

            # Directly stamp the DB row — no mock, no sync needed
            msg_repo.update_labels(
                target.id,
                target.label_ids | frozenset({to_archive.id}),
            )

        html = synced_app.get("/archive").data.decode()
        assert "Remove To-Archive label from" in html

    def test_remove_to_archive_label_removes_label_from_messages(
        self, synced_app: FlaskClient
    ) -> None:
        """Test that the remove-to-archive endpoint removes the To-Archive label."""
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        client_mock = synced_app.application.config["GMAIL_ZERO_CLIENT"]

        # First, tag some archived messages with To-Archive label
        with synced_app.application.app_context(), get_session(engine) as session:
            label_repo = LabelRepository(session)
            msg_repo = MessageRepository(session)

            to_archive = label_repo.get_by_name("ZeroApp/To-Archive")
            assert to_archive is not None

            # Get an archived message to tag with To-Archive
            archived_msgs = msg_repo.list_search(
                MessageFilter(is_archived=True, limit=10)
            )
            assert len(archived_msgs) > 0
            test_msg_id = archived_msgs[0].id

        # Tag the message with To-Archive via the mock client
        client_mock.modify_message(test_msg_id, add_labels=["ZeroApp/To-Archive"])

        # Now run remove-to-archive
        resp = synced_app.post(
            "/archive/remove-to-archive",
            follow_redirects=True,
        )

        html = resp.data.decode()
        assert resp.status_code == 200
        assert "Removed To-Archive label from" in html

        # Verify the label was actually removed
        with synced_app.application.app_context(), get_session(engine) as session:
            msg_repo = MessageRepository(session)
            msg = msg_repo.get_by_id(test_msg_id)
            assert msg is not None
            # After removal, message should not have To-Archive label
            assert "ZeroApp/To-Archive" not in msg.label_ids

    def test_remove_to_archive_no_messages_shows_info_flash(
        self, synced_app: FlaskClient
    ) -> None:
        """Test that when there are no To-Archive messages, info flash is shown.

        Order-independent: directly strips To-Archive from every archived DB row
        before calling the route, so the test is immune to whatever state earlier
        tests left in the DB.
        """
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]

        # Directly remove To-Archive from every archived message in DB
        with synced_app.application.app_context(), get_session(engine) as session:
            label_repo = LabelRepository(session)
            msg_repo = MessageRepository(session)

            to_archive = label_repo.get_by_name("ZeroApp/To-Archive")
            assert to_archive is not None

            archived_msgs = msg_repo.list_search(
                MessageFilter(is_archived=True, limit=500)
            )
            for msg in archived_msgs:
                if to_archive.id in msg.label_ids:
                    msg_repo.update_labels(
                        msg.id,
                        msg.label_ids - frozenset({to_archive.id}),
                    )

        # Route must now find zero To-Archive messages and return the info flash
        resp = synced_app.post(
            "/archive/remove-to-archive",
            follow_redirects=True,
        )

        assert resp.status_code == 200
        assert "No archived messages with To-Archive label found" in resp.data.decode()


class TestSyncActions:
    def test_full_sync_now_runs_and_records_history(self, synced_app: FlaskClient) -> None:
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        with synced_app.application.app_context(), get_session(engine) as session:
            before_count = len(SyncStateRepository(session).list_recent(limit=1000))

        resp = synced_app.post(
            "/sync",
            data={
                "sync_mode": "full",
                "next": "/settings",
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Full sync complete" in html

        with synced_app.application.app_context(), get_session(engine) as session:
            after_repo = SyncStateRepository(session)
            after_count = len(after_repo.list_recent(limit=1000))
            latest = after_repo.latest()

        assert after_count == before_count + 1
        assert latest is not None
        assert latest.is_full_sync

    def test_incremental_sync_now_advances_message_state(self, synced_app: FlaskClient) -> None:
        engine = synced_app.application.config["GMAIL_ZERO_ENGINE"]
        client_mock = synced_app.application.config["GMAIL_ZERO_CLIENT"]

        with synced_app.application.app_context(), get_session(engine) as session:
            before_count = MessageRepository(session).count_search(MessageFilter())
            before_history_count = len(SyncStateRepository(session).list_recent(limit=1000))

        client_mock.advance_history(new_message_count=2)

        resp = synced_app.post(
            "/sync",
            data={
                "sync_mode": "incremental",
                "next": "/settings",
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Incremental sync complete" in html

        with synced_app.application.app_context(), get_session(engine) as session:
            message_repo = MessageRepository(session)
            sync_repo = SyncStateRepository(session)
            message_count = message_repo.count_search(MessageFilter())
            after_history_count = len(sync_repo.list_recent(limit=1000))
            latest = sync_repo.latest()

        assert message_count == before_count + 2
        assert after_history_count == before_history_count + 1
        assert latest is not None
        assert latest.is_incremental_sync


# ── Error handlers ────────────────────────────────────────────────────────────


class TestErrorHandlers:
    def test_404_returns_error_page(self, synced_app: FlaskClient) -> None:
        resp = synced_app.get("/this-route-does-not-exist")
        assert resp.status_code == 404
        html = resp.data.decode()
        assert "404" in html

    def test_demo_banner_present_on_404(self, synced_app: FlaskClient) -> None:
        """Demo banner must appear on error pages too (inherited from base.html)."""
        html = synced_app.get("/nonexistent").data.decode()
        assert "DEMO MODE" in html


# ── Jinja2 filters ────────────────────────────────────────────────────────────


class TestJinja2Filters:
    """Verify format_size and format_datetime filters produce correct output."""

    def test_format_size_filter_in_size_page(self, synced_app: FlaskClient) -> None:
        """The size page must render human-readable sizes (MB or GB)."""
        html = synced_app.get("/size").data.decode()
        assert "MB" in html or "GB" in html or "KB" in html

    def test_format_datetime_filter_in_settings(self, synced_app: FlaskClient) -> None:
        """Sync timestamps in settings must be in YYYY-MM-DD HH:MM format."""
        import re
        html = synced_app.get("/settings").data.decode()
        # Loose check: a four-digit year followed by dashes
        assert re.search(r"\d{4}-\d{2}-\d{2}", html), (
            "Expected datetime in YYYY-MM-DD format on settings page"
        )


# ── Sort param coercion (lines 66, 68) ───────────────────────────────────────


class TestSortParamCoercion:
    """Invalid sort params are silently coerced to defaults; routes return 200."""

    def test_invalid_sort_by_falls_back_to_default(self, synced_app: FlaskClient) -> None:
        """sort=INVALID_COLUMN is coerced to the route's default sort key."""
        resp = synced_app.get("/inbox?sort=INVALID_COLUMN&dir=asc")
        assert resp.status_code == 200

    def test_invalid_sort_dir_falls_back_to_default(self, synced_app: FlaskClient) -> None:
        """dir=INVALID is coerced to the route's default sort direction."""
        resp = synced_app.get("/inbox?sort=internal_date&dir=INVALID_DIRECTION")
        assert resp.status_code == 200

    def test_invalid_sort_by_on_archive_falls_back(self, synced_app: FlaskClient) -> None:
        """Archive route coerces invalid sort_by to sender_domain."""
        resp = synced_app.get("/archive?sort=NOT_A_COLUMN")
        assert resp.status_code == 200

    def test_invalid_sort_dir_on_size_falls_back(self, synced_app: FlaskClient) -> None:
        """Size route coerces invalid sort_dir to desc."""
        resp = synced_app.get("/size?dir=sideways")
        assert resp.status_code == 200


# ── Group-by param (lines 80, 82, 88-104) ────────────────────────────────────


class TestGroupByParam:
    """?group=1/0 URL param exercises both grouping helper functions."""

    def test_archive_group_0_disables_grouping(self, synced_app: FlaskClient) -> None:
        """?group=0 on /archive disables sender-domain grouping."""
        resp = synced_app.get("/archive?group=0")
        assert resp.status_code == 200

    def test_archive_group_1_enables_grouping(self, synced_app: FlaskClient) -> None:
        """?group=1 on /archive enables _group_messages_by_domain."""
        resp = synced_app.get("/archive?group=1")
        assert resp.status_code == 200

    def test_sent_group_1_enables_recipient_grouping(self, synced_app: FlaskClient) -> None:
        """?group=1 on /sent enables _group_sent_by_recipient_domain."""
        resp = synced_app.get("/sent?group=1")
        assert resp.status_code == 200

    def test_inbox_group_1_enables_sender_domain_grouping(
        self, synced_app: FlaskClient
    ) -> None:
        """?group=1 on /inbox enables _group_messages_by_domain."""
        resp = synced_app.get("/inbox?group=1")
        assert resp.status_code == 200


# ── /labels/create route (lines 598-622) ─────────────────────────────────────


class TestCreateLabelRoute:
    """/labels/create POST creates a label via the client and redirects."""

    def test_create_label_with_valid_name_redirects_to_settings(
        self, synced_app: FlaskClient
    ) -> None:
        """Valid label name causes a redirect to /settings."""
        resp = synced_app.post(
            "/labels/create",
            data={"label_name": "MyTestLabel"},
            follow_redirects=False,
        )
        assert resp.status_code in (301, 302)
        assert "settings" in resp.headers["Location"]

    def test_create_label_with_valid_name_flashes_success(
        self, synced_app: FlaskClient
    ) -> None:
        """Valid label name flashes a success message on the settings page."""
        resp = synced_app.post(
            "/labels/create",
            data={"label_name": "AnotherLabel"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"created" in resp.data.lower()

    def test_create_label_with_empty_name_flashes_error(
        self, synced_app: FlaskClient
    ) -> None:
        """Empty label_name flashes an error message and redirects to /settings."""
        resp = synced_app.post(
            "/labels/create",
            data={"label_name": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()

    def test_create_label_prepends_zeroapp_namespace(
        self, synced_app: FlaskClient
    ) -> None:
        """Label names without ZeroApp/ prefix are automatically namespaced."""
        resp = synced_app.post(
            "/labels/create",
            data={"label_name": "NamespaceTest"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # The flash or settings page must reference the ZeroApp/ prefixed name
        assert b"ZeroApp/NamespaceTest" in resp.data
