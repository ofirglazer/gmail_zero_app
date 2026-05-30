"""
Main blueprint — HTML page routes for gmail_zero_app.

Most page routes are read-only.  The label action and manual sync action
mutate state, but they still route through the application services rather
than touching repositories directly.

Route → service call → template render.  No repository access from routes
directly — everything goes through services or g.{repo} only where no
matching service method exists (e.g. label_repo.list_user_labels for the
search dropdown).

``g`` is populated by the ``before_request`` hook in ``presentation.app``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from application.dto.label_operation import LabelToggleRequest
from infrastructure.persistence.repositories.message_repository import MessageFilter

if TYPE_CHECKING:
    from werkzeug.wrappers import Response

    from domain.models import Message

main_bp = Blueprint("main", __name__)

# Default rows per page for paginated views
_DEFAULT_PER_PAGE = 50

_WORKFLOW_LABEL_SUFFIXES = {"To-Archive", "To-Remove"}


def _parse_sort_params(
    *,
    default_sort_by: str,
    default_sort_dir: str,
    allowed_sort_by: set[str],
) -> tuple[str, str]:
    sort_by = request.args.get("sort", default_sort_by).strip()
    sort_dir = request.args.get("dir", default_sort_dir).strip().lower()
    if sort_by not in allowed_sort_by:
        sort_by = default_sort_by
    if sort_dir not in {"asc", "desc"}:
        sort_dir = default_sort_dir
    return sort_by, sort_dir


def _show_workflow_labels() -> bool:
    return request.args.get("show_workflow_labels", "").strip() in {"1", "true", "yes"}


# ## Root redirect ##


@main_bp.route("/")
def index() -> Response:
    """Redirect root to the dashboard."""
    return redirect(url_for("main.dashboard"))


# ## Dashboard ##


@main_bp.route("/dashboard")
def dashboard() -> str:
    """
    Dashboard: four goal-status cards and 30-day progress graphs.

    Context:
        summary     DashboardSummary — all current counts and derived props.
        snapshots   list[DailySnapshot] — last 30 days, oldest-first.
        last_sync   SyncState | None — most recent sync record.
    """
    summary = g.analytics_svc.dashboard_summary()
    snapshots = g.analytics_svc.progress_snapshots(days=g.settings.graph_history_days)
    last_sync = g.sync_repo.latest()

    return render_template(
        "dashboard.html",
        summary=summary,
        snapshots=snapshots,
        last_sync=last_sync,
    )


@main_bp.route("/messages/label", methods=["POST"])
def apply_label() -> Response:
    """Apply one user label to the selected message IDs."""
    message_ids = tuple(mid for mid in request.form.getlist("message_ids") if mid)
    label_id = request.form.get("label_id", "").strip()
    next_url = request.form.get("next", "").strip() or url_for("main.dashboard")

    if not message_ids:
        flash("Select at least one message to label.", "error")
        return redirect(next_url)
    if not label_id:
        flash("Choose a label to apply.", "error")
        return redirect(next_url)

    request_dto = LabelToggleRequest(
        message_ids=message_ids,
        label_id=label_id,
    )
    updated = g.label_svc.toggle_label_operation(request_dto)
    flash(f"Applied label to {len(updated)} message(s).", "success")
    return redirect(next_url)


@main_bp.route("/sync", methods=["POST"])
def sync_now() -> Response:
    """
    Trigger a manual sync from the UI.

    The caller chooses between a full sync and an incremental sync; the
    service executes the selected strategy and may fall back internally.
    """
    sync_mode = request.form.get("sync_mode", "").strip().lower()
    next_url = request.form.get("next", "").strip() or url_for("main.settings_page")

    if sync_mode not in {"full", "incremental"}:
        flash("Choose a sync mode.", "error")
        return redirect(next_url)

    try:
        if sync_mode == "full":
            state = g.sync_svc.run_full_sync()
        else:
            state = g.sync_svc.run_incremental_sync()
    except Exception as exc:
        flash(f"Sync failed: {exc}", "error")
        return redirect(next_url)

    flash(
        f"{state.sync_type.value.title()} sync complete: {state.messages_synced} messages synced.",
        "success",
    )
    return redirect(next_url)


# ## Inbox Zero ##


@main_bp.route("/inbox")
def inbox() -> str:
    """
    Inbox Zero workflow: oldest messages first, paginated.

    Query params:
        page     int ≥ 1   (default 1)
        per_page int 1-200 (default 50)
    """
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(200, max(1, request.args.get("per_page", _DEFAULT_PER_PAGE, type=int)))
    offset = (page - 1) * per_page
    sort_by, sort_dir = _parse_sort_params(
        default_sort_by="internal_date",
        default_sort_dir="desc",
        allowed_sort_by={"internal_date", "sender_domain", "sender", "recipient", "subject", "size_estimate"},
    )
    sort_by, sort_dir = _parse_sort_params(
        default_sort_by="internal_date",
        default_sort_dir="asc",
        allowed_sort_by={"internal_date", "sender_domain", "subject", "size_estimate"},
    )

    messages = g.msg_repo.list_inbox(
        limit=per_page,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total_count = g.msg_repo.count_inbox()
    total_pages = max(1, math.ceil(total_count / per_page))

    return render_template(
        "inbox.html",
        messages=messages,
        page=page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=_show_workflow_labels(),
    )


# ## Archive Hygiene ##


@main_bp.route("/archive")
def archive() -> str:
    """
    Archive Hygiene workflow: unlabelled archived messages grouped by sender domain.

    The grouping is computed in the route rather than the template because
    Jinja2 has no equivalent of Python's ``itertools.groupby``.

    Context:
        messages          list[Message] — up to 200 unlabelled archived messages.
        total_count       int — total unlabelled archived messages (unpaginated).
        grouped_by_domain dict[str, list[Message]] — domain → messages mapping
                          for the domain-based bulk-action UX in Step 7.
    """
    sort_by, sort_dir = _parse_sort_params(
        default_sort_by="sender_domain",
        default_sort_dir="asc",
        allowed_sort_by={"sender_domain", "sender", "subject", "internal_date", "size_estimate"},
    )
    messages = g.msg_repo.list_archive_unlabelled(
        limit=200,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total_count = g.msg_repo.count_archive_unlabelled()
    to_archive_label = g.label_repo.get_by_name("ZeroApp/To-Archive")
    archive_to_archive_count = (
        g.msg_repo.count_search(
            MessageFilter(
                label_id=to_archive_label.id,
                is_archived=True,
                limit=1,
            )
        )
        if to_archive_label is not None
        else 0
    )

    # Group by sender_domain; preserve order (domains appear in the order
    # list_archive_unlabelled returns them — already sorted by domain ASC)
    grouped_by_domain: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        grouped_by_domain[msg.sender_domain].append(msg)
    grouped_by_domain = dict(sorted(grouped_by_domain.items(), key=lambda item: item[0]))

    return render_template(
        "archive.html",
        messages=messages,
        total_count=total_count,
        grouped_by_domain=dict(grouped_by_domain),
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=_show_workflow_labels(),
        archive_to_archive_count=archive_to_archive_count,
    )


# ## Sent Review ##


@main_bp.route("/sent")
def sent() -> str:
    """
    Sent Review workflow: sent messages without a workflow label, oldest first.

    Context:
        messages     list[Message] — up to 200 sent messages.
        total_count  int — count of unresolved sent messages.
    """
    sort_by, sort_dir = _parse_sort_params(
        default_sort_by="internal_date",
        default_sort_dir="asc",
        allowed_sort_by={"internal_date", "recipient", "subject", "size_estimate"},
    )
    messages = g.msg_repo.list_sent(
        limit=200,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total_count = g.msg_repo.count_sent_unresolved()

    return render_template(
        "sent.html",
        messages=messages,
        total_count=total_count,
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=_show_workflow_labels(),
    )


# ## Size Reduction ##


@main_bp.route("/size")
def size() -> str:
    """
    Size Reduction workflow: the 100 largest messages across all locations.

    Context:
        messages         list[Message] — up to 100 messages, largest first.
        total_size_bytes int — total size of all messages in the mailbox.
        total_size_gb    float — total_size_bytes expressed in GB.
    """
    sort_by, sort_dir = _parse_sort_params(
        default_sort_by="size_estimate",
        default_sort_dir="desc",
        allowed_sort_by={"size_estimate", "sender_domain", "sender", "recipient", "subject", "internal_date"},
    )
    messages = g.msg_repo.list_largest(limit=100, sort_by=sort_by, sort_dir=sort_dir)
    total_size_bytes = g.msg_repo.total_size_bytes()
    total_size_gb = round(total_size_bytes / (1024 ** 3), 3)

    return render_template(
        "size.html",
        messages=messages,
        total_size_bytes=total_size_bytes,
        total_size_gb=total_size_gb,
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=_show_workflow_labels(),
    )


# ## Search ##


@main_bp.route("/search")
def search() -> str:
    """
    Search / filter view.

    Accepts GET query parameters matching MessageFilter fields.  All
    parameters are optional; omitted parameters are not applied as filters.

    Query params (all optional):
        sender              str   — partial match on sender address
        sender_domain       str   — partial match on sender domain
        subject_contains    str   — partial match on subject
        label_id            str   — exact Gmail label ID
        date_from           str   — ISO date YYYY-MM-DD (inclusive lower bound)
        date_to             str   — ISO date YYYY-MM-DD (inclusive upper bound)
        min_size_bytes      int   — minimum message size
        is_inbox            bool  — '1' or 'true' for inbox only
        is_sent             bool  — '1' or 'true' for sent only
        is_unread           bool  — '1' or 'true' for unread only
        page                int   — pagination page (default 1)
        per_page            int   — rows per page (default 50)

    Context:
        messages     list[Message] — matching messages for current page.
        filters      dict          — current filter values (for form repopulation).
        total_count  int           — total matching count (all pages).
        total_pages  int           — total number of pages.
        user_labels  list[Label]   — all user labels for the label dropdown.
    """
    from datetime import UTC, datetime

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(200, max(1, request.args.get("per_page", _DEFAULT_PER_PAGE, type=int)))
    offset = (page - 1) * per_page

    # Collect and coerce raw query parameters
    raw_date_from = request.args.get("date_from", "").strip() or None
    raw_date_to = request.args.get("date_to", "").strip() or None

    def _parse_date(raw: str | None) -> datetime | None:
        """Parse YYYY-MM-DD string into a UTC-aware datetime, or return None."""
        if raw is None:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None

    def _parse_bool(raw: str | None) -> bool | None:
        """Parse '1', 'true', 'yes' → True; '0', 'false', 'no' → False; else None."""
        if raw is None:
            return None
        return raw.lower() in ("1", "true", "yes")

    mf = MessageFilter(
        sender=request.args.get("sender", "").strip() or None,
        sender_domain=request.args.get("sender_domain", "").strip() or None,
        subject_contains=request.args.get("subject_contains", "").strip() or None,
        label_id=request.args.get("label_id", "").strip() or None,
        date_from=_parse_date(raw_date_from),
        date_to=_parse_date(raw_date_to),
        min_size_bytes=request.args.get("min_size_bytes", None, type=int),
        is_inbox=_parse_bool(request.args.get("is_inbox")),
        is_sent=_parse_bool(request.args.get("is_sent")),
        is_unread=_parse_bool(request.args.get("is_unread")),
        limit=per_page,
        offset=offset,
    )

    messages, total_count = g.search_svc.search(mf)
    total_pages = max(1, math.ceil(total_count / per_page))
    user_labels = g.label_repo.list_user_labels()

    # Filters dict for repopulating the form inputs in the template
    filters = {
        "sender": mf.sender or "",
        "sender_domain": mf.sender_domain or "",
        "subject_contains": mf.subject_contains or "",
        "label_id": mf.label_id or "",
        "date_from": raw_date_from or "",
        "date_to": raw_date_to or "",
        "min_size_bytes": request.args.get("min_size_bytes", ""),
        "is_inbox": request.args.get("is_inbox", ""),
        "is_sent": request.args.get("is_sent", ""),
        "is_unread": request.args.get("is_unread", ""),
    }

    return render_template(
        "search.html",
        messages=messages,
        filters=filters,
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        per_page=per_page,
        user_labels=user_labels,
        show_workflow_labels=_show_workflow_labels(),
    )


# ## Settings ##


@main_bp.route("/settings")
def settings_page() -> str:
    """
    Settings view: sync history, label registry, environment summary.

    Read-only — no configuration changes are possible via the UI.

    Context:
        recent_syncs     list[SyncState] — 20 most recent sync records.
        labels           list[Label]     — all labels (system + user).
        settings_summary dict            — key settings for display.
    """
    recent_syncs = g.sync_repo.list_recent(limit=20)
    labels = g.label_repo.list_all()

    settings_summary = {
        "env": g.settings.env.value,
        "db_path": str(g.settings.db_path),
        "host": g.settings.host,
        "port": g.settings.port,
        "sync_batch_size": g.settings.sync_batch_size,
        "sync_rate_limit_delay_ms": g.settings.sync_rate_limit_delay_ms,
        "large_threshold": g.settings.large_message_threshold_bytes,
        "very_large_threshold": g.settings.very_large_message_threshold_bytes,
        "old_thread_threshold_days": g.settings.old_thread_threshold_days,
    }

    return render_template(
        "settings.html",
        recent_syncs=recent_syncs,
        labels=labels,
        settings_summary=settings_summary,
    )
