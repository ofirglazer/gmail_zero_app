"""
API blueprint — JSON endpoints for gmail_zero_app.

Thin endpoints that serialise domain entities to JSON for client-side
consumption (Chart.js graphs, health checks).  No HTML rendering.

``g`` is populated by the ``before_request`` hook in ``presentation.app``.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


@api_bp.route("/progress")
def progress():
    """
    Return snapshot data for Chart.js progress graphs.

    Fetches the last ``settings.graph_history_days`` daily snapshots and
    serialises them to a JSON array consumed by the dashboard's fetch() call.

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
    snapshots = g.analytics_svc.progress_snapshots(days=g.settings.graph_history_days)

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


@api_bp.route("/health")
def health():
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
