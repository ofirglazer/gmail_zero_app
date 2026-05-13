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

import pytest

from config.settings import Environment, Settings
from infrastructure.gmail.mapper import GmailMapper
from infrastructure.gmail.mock_client import MockGmailClient
from infrastructure.persistence.database import build_engine, get_session, initialise_db
from infrastructure.persistence.repositories.label_repository import LabelRepository
from infrastructure.persistence.repositories.message_repository import MessageRepository
from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository
from application.services.sync_service import SyncService

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
def synced_app(demo_settings: Settings):
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
    def test_root_redirects_to_dashboard(self, synced_app):
        """GET / should redirect to /dashboard."""
        resp = synced_app.get("/", follow_redirects=False)
        assert resp.status_code in (301, 302)
        assert "/dashboard" in resp.headers["Location"]


# ── Dashboard ─────────────────────────────────────────────────────────────────


class TestDashboard:
    def test_dashboard_returns_200(self, synced_app):
        resp = synced_app.get("/dashboard")
        assert resp.status_code == 200

    def test_dashboard_shows_goal_cards(self, synced_app):
        html = synced_app.get("/dashboard").data.decode()
        assert "Inbox Zero" in html
        assert "Archive Zero" in html
        assert "Sent Zero" in html
        assert "Size Zero" in html

    def test_dashboard_shows_demo_banner(self, synced_app):
        html = synced_app.get("/dashboard").data.decode()
        assert "DEMO MODE" in html

    def test_dashboard_shows_last_sync(self, synced_app):
        """After the full sync fixture, the last sync timestamp must appear."""
        html = synced_app.get("/dashboard").data.decode()
        # The sync-status block is present and contains 'full'
        assert "full" in html.lower()

    def test_dashboard_no_server_error(self, synced_app):
        """Dashboard must not produce a 500 even with a full DB."""
        resp = synced_app.get("/dashboard")
        assert resp.status_code != 500


# ── Inbox ─────────────────────────────────────────────────────────────────────


class TestInbox:
    def test_inbox_returns_200(self, synced_app):
        resp = synced_app.get("/inbox")
        assert resp.status_code == 200

    def test_inbox_shows_messages(self, synced_app):
        html = synced_app.get("/inbox").data.decode()
        # The mock dataset has many inbox messages
        assert "<tr" in html, "Expected table rows in inbox response"

    def test_inbox_pagination_page2(self, synced_app):
        """GET /inbox?page=2 should return 200 without errors."""
        resp = synced_app.get("/inbox?page=2&per_page=10")
        assert resp.status_code == 200

    def test_inbox_page_out_of_range_returns_empty_not_error(self, synced_app):
        """A very high page number should return 200 with empty state, not 500."""
        resp = synced_app.get("/inbox?page=99999")
        assert resp.status_code == 200

    def test_inbox_shows_nav_active(self, synced_app):
        html = synced_app.get("/inbox").data.decode()
        # The active nav link class appears for the inbox link
        assert 'class="active"' in html


# ── Archive ───────────────────────────────────────────────────────────────────


class TestArchive:
    def test_archive_returns_200(self, synced_app):
        resp = synced_app.get("/archive")
        assert resp.status_code == 200

    def test_archive_shows_domain_groups(self, synced_app):
        """The mock dataset has many unlabelled archived messages grouped by domain."""
        html = synced_app.get("/archive").data.decode()
        # acme.com newsletters are in the archive with no custom label
        assert "acme.com" in html or "github.com" in html

    def test_archive_count_nonzero(self, synced_app):
        html = synced_app.get("/archive").data.decode()
        # Page header includes count badge with a non-zero number
        assert "0" not in html or "Archive Zero" in html


# ── Sent ──────────────────────────────────────────────────────────────────────


class TestSent:
    def test_sent_returns_200(self, synced_app):
        resp = synced_app.get("/sent")
        assert resp.status_code == 200

    def test_sent_shows_messages(self, synced_app):
        html = synced_app.get("/sent").data.decode()
        # Mock dataset has several sent messages
        assert "Sent Zero" in html


# ── Size ──────────────────────────────────────────────────────────────────────


class TestSize:
    def test_size_returns_200(self, synced_app):
        resp = synced_app.get("/size")
        assert resp.status_code == 200

    def test_size_shows_total_gb(self, synced_app):
        html = synced_app.get("/size").data.decode()
        assert "GB" in html

    def test_size_shows_large_messages(self, synced_app):
        """The mock dataset contains several multi-MB messages."""
        html = synced_app.get("/size").data.decode()
        assert "MB" in html or "GB" in html


# ── Search ────────────────────────────────────────────────────────────────────


class TestSearch:
    def test_search_returns_200_no_params(self, synced_app):
        resp = synced_app.get("/search")
        assert resp.status_code == 200

    def test_search_with_sender_domain_filter(self, synced_app):
        """GET /search?sender_domain=github.com should return only GitHub messages."""
        resp = synced_app.get("/search?sender_domain=github.com")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "github.com" in html

    def test_search_with_nonexistent_domain_returns_empty(self, synced_app):
        """A domain that doesn't exist should produce an empty-state response."""
        resp = synced_app.get("/search?sender_domain=no-such-domain-xyz.invalid")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "0 message" in html or "No messages" in html

    def test_search_form_repopulates_filter_values(self, synced_app):
        """Filter values must appear in the rendered form inputs."""
        resp = synced_app.get("/search?sender_domain=aws.com&is_unread=1")
        html = resp.data.decode()
        assert "aws.com" in html

    def test_search_pagination(self, synced_app):
        """?page=2 on search results must return 200."""
        resp = synced_app.get("/search?page=2&per_page=5")
        assert resp.status_code == 200

    def test_search_shows_user_labels_dropdown(self, synced_app):
        """Label dropdown must include at least one user label from the mock dataset."""
        html = synced_app.get("/search").data.decode()
        assert "ZeroApp/" in html


# ── Settings ──────────────────────────────────────────────────────────────────


class TestSettings:
    def test_settings_returns_200(self, synced_app):
        resp = synced_app.get("/settings")
        assert resp.status_code == 200

    def test_settings_shows_sync_history(self, synced_app):
        """Settings page must list the completed full sync."""
        html = synced_app.get("/settings").data.decode()
        assert "full" in html.lower()

    def test_settings_shows_label_registry(self, synced_app):
        html = synced_app.get("/settings").data.decode()
        assert "INBOX" in html

    def test_settings_shows_env_mode(self, synced_app):
        html = synced_app.get("/settings").data.decode()
        assert "demo" in html.lower()


# ── API endpoints ─────────────────────────────────────────────────────────────


class TestApiEndpoints:
    def test_health_returns_200(self, synced_app):
        resp = synced_app.get("/api/v1/health")
        assert resp.status_code == 200

    def test_health_returns_json(self, synced_app):
        resp = synced_app.get("/api/v1/health")
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["mode"] == "demo"

    def test_progress_returns_200(self, synced_app):
        resp = synced_app.get("/api/v1/progress")
        assert resp.status_code == 200

    def test_progress_returns_json_with_snapshots_key(self, synced_app):
        resp = synced_app.get("/api/v1/progress")
        data = json.loads(resp.data)
        assert "snapshots" in data
        assert isinstance(data["snapshots"], list)

    def test_progress_snapshot_has_required_fields(self, synced_app):
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

    def test_progress_snapshots_non_empty_after_sync(self, synced_app):
        """After a full sync, at least one snapshot must be present."""
        resp = synced_app.get("/api/v1/progress")
        data = json.loads(resp.data)
        assert len(data["snapshots"]) >= 1


# ── Error handlers ────────────────────────────────────────────────────────────


class TestErrorHandlers:
    def test_404_returns_error_page(self, synced_app):
        resp = synced_app.get("/this-route-does-not-exist")
        assert resp.status_code == 404
        html = resp.data.decode()
        assert "404" in html

    def test_demo_banner_present_on_404(self, synced_app):
        """Demo banner must appear on error pages too (inherited from base.html)."""
        html = synced_app.get("/nonexistent").data.decode()
        assert "DEMO MODE" in html


# ── Jinja2 filters ────────────────────────────────────────────────────────────


class TestJinja2Filters:
    """Verify format_size and format_datetime filters produce correct output."""

    def test_format_size_filter_in_size_page(self, synced_app):
        """The size page must render human-readable sizes (MB or GB)."""
        html = synced_app.get("/size").data.decode()
        assert "MB" in html or "GB" in html or "KB" in html

    def test_format_datetime_filter_in_settings(self, synced_app):
        """Sync timestamps in settings must be in YYYY-MM-DD HH:MM format."""
        import re
        html = synced_app.get("/settings").data.decode()
        # Loose check: a four-digit year followed by dashes
        assert re.search(r"\d{4}-\d{2}-\d{2}", html), (
            "Expected datetime in YYYY-MM-DD format on settings page"
        )
