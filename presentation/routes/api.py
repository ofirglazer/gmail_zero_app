"""
API blueprint — JSON endpoints for gmail_zero_app.

Thin endpoints that serialise domain entities to JSON for client-side
consumption (Chart.js graphs, health checks).  No HTML rendering.

``g`` is populated by the ``before_request`` hook in ``presentation.app``.
"""

from __future__ import annotations

from flask import Blueprint, Response, g, jsonify, request

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")

_ALLOWED_DAYS = frozenset({30, 90})


@api_bp.route("/progress")
def progress() -> Response:
    """
    Return snapshot data for Chart.js progress graphs.

    Accepts an optional ``days`` query parameter (30 or 90) to control the
    history window.  Invalid values fall back to ``settings.graph_history_days``.

    Query parameters:
        days: History window in calendar days. Accepted values: 30, 90.
              Defaults to ``settings.graph_history_days`` (typically 30).

    Response shape:
        {
          "snapshots": [
            {
              "date": "2024-03-01",
              "inbox_count": 50,
              "inbox_size_bytes": 1234567,
              "archive_unlabelled_count": 200,
              "sent_unresolved_count": 30,
              "total_size_bytes": 987654321,
              "custom_label_coverage_pct": 12.5
            },
            ...
          ]
        }
    """
    default_days: int = g.settings.graph_history_days
    days_param = request.args.get("days", type=int)
    days: int = days_param if days_param in _ALLOWED_DAYS else default_days

    snapshots = g.analytics_svc.progress_snapshots(days=days)

    return jsonify({
        "snapshots": [
            {
                "date": s.snapshot_date.isoformat(),
                "inbox_count": s.inbox_count,
                "inbox_size_bytes": s.inbox_size_bytes,
                "archive_unlabelled_count": s.archive_unlabelled_count,
                "sent_unresolved_count": s.sent_unresolved_count,
                "total_size_bytes": s.total_size_bytes,
                "custom_label_coverage_pct": s.custom_label_coverage_pct,
            }
            for s in snapshots
        ]
    })


@api_bp.route("/storage-breakdown")
def storage_breakdown() -> Response:
    """Return storage breakdown data for the three secondary dashboard charts.

    Fetches top senders, label size distribution, and age-bucket message
    counts in a single call so the frontend can render all three charts with
    one request.

    Response shape:
        {
          "top_senders_by_count": [
            {"sender": "...", "sender_domain": "...",
             "message_count": 10, "total_size_bytes": 102400}
          ],
          "top_senders_by_size": [...same shape...],
          "size_by_label": [
            {"label_name": "Work", "total_size_bytes": 204800, "message_count": 30}
          ],
          "age_buckets": {
            "current_year": 100, "current_year_label": "2026",
            "past_year": 200,    "past_year_label": "2025",
            "older": 300,        "older_label": "2024 & older"
          }
        }
    """
    bd = g.analytics_svc.storage_breakdown()

    def _sender(s: object) -> dict[str, object]:
        """Serialize one SenderStats to a JSON-safe dict."""
        return {
            "sender": s.sender,  # type: ignore[attr-defined]
            "sender_domain": s.sender_domain,  # type: ignore[attr-defined]
            "message_count": s.message_count,  # type: ignore[attr-defined]
            "total_size_bytes": s.total_size_bytes,  # type: ignore[attr-defined]
        }

    return jsonify({
        "top_senders_by_count": [_sender(s) for s in bd.top_senders_by_count],
        "top_senders_by_size": [_sender(s) for s in bd.top_senders_by_size],
        "size_by_label": [
            {
                "label_name": lbl.label_name,
                "total_size_bytes": lbl.total_size_bytes,
                "message_count": lbl.message_count,
            }
            for lbl in bd.size_by_label
        ],
        "age_buckets": {
            "current_year": bd.age_buckets.current_year,
            "current_year_label": bd.age_buckets.current_year_label,
            "past_year": bd.age_buckets.past_year,
            "past_year_label": bd.age_buckets.past_year_label,
            "older": bd.age_buckets.older,
            "older_label": bd.age_buckets.older_label,
        },
    })


@api_bp.route("/health")
def health() -> Response:
    """
    Health check endpoint.

    Returns the application's operating mode so monitoring tools and the
    demo banner can verify the server is reachable.

    Response:
        {"status": "ok", "mode": "demo" | "production"}
    """
    return jsonify({
        "status": "ok",
        "mode": g.settings.env.value,
    })


@api_bp.route("/message/<message_id>/body")
def message_body(message_id: str) -> Response:
    """
    Fetch the full message body for a single message.

    Used by the reading pane modal to display message content.

    Args:
        message_id: Gmail message ID from the URL path.

    Response:
        {
            "body": "<plain text message body>",
            "subject": "<message subject>",
            "sender": "<from address>"
        }
    """
    body = g.client.get_message_body(message_id)
    msg = g.msg_repo.get_by_id(message_id)
    return jsonify({
        "body": body,
        "subject": msg.subject if msg else "",
        "sender": msg.sender if msg else "",
        "date": msg.internal_date.isoformat() if msg else "",
    })
