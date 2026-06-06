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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from application.dto.label_operation import BulkLabelOperationRequest, LabelToggleRequest
from domain.models.label import Label, LabelListVisibility, LabelType, MessageListVisibility
from infrastructure.persistence.repositories.message_repository import MessageFilter
from presentation.page_descriptions import get_page_description

if TYPE_CHECKING:
    from collections.abc import Callable

    from werkzeug.wrappers import Response

    from domain.models import Message

main_bp = Blueprint("main", __name__)

# Default rows per page for paginated views
_DEFAULT_PER_PAGE = 50

_WORKFLOW_LABEL_SUFFIXES = {"To-Archive", "To-Remove"}

# Python-side sort keys for the archive view.  The unlabelled query is ordered
# in SQL, but workflow-labelled messages are merged in afterwards, so the
# combined list must be re-sorted on the same key to keep each domain group in
# a consistent order.
_ARCHIVE_SORT_KEYS: dict[str, Callable[[Message], str | int | datetime]] = {
    "sender_domain": lambda m: (m.sender_domain or "").lower(),
    "sender": lambda m: (m.sender or "").lower(),
    "subject": lambda m: (m.subject or "").lower(),
    "internal_date": lambda m: m.internal_date,
    "size_estimate": lambda m: m.size_estimate,
}


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


def _parse_group_param(*, default: bool = False) -> bool:
    """Parse ?group=1/0 URL param; fall back to default when absent."""
    val = request.args.get("group", "").strip()
    if val == "1":
        return True
    if val == "0":
        return False
    return default


def _group_messages_by_domain(messages: list[Message]) -> dict[str, list[Message]]:
    """Group messages by sender_domain, sorted alphabetically."""
    grouped: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        grouped[msg.sender_domain].append(msg)
    return dict(sorted(grouped.items(), key=lambda item: item[0].lower()))


def _group_sent_by_recipient_domain(messages: list[Message]) -> dict[str, list[Message]]:
    """Group sent messages by recipient domain, sorted alphabetically."""
    grouped: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        recipient = msg.recipient or ""
        if "@" in recipient:
            domain = recipient.split("@")[-1].rstrip(">").strip()
        else:
            domain = recipient or "(unknown)"
        grouped[domain].append(msg)
    return dict(sorted(grouped.items(), key=lambda item: item[0].lower()))


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
        page_info=get_page_description("dashboard"),
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


@main_bp.route("/archive/remove-to-archive", methods=["POST"])
def remove_to_archive_label() -> Response:
    """Remove ZeroApp/To-Archive label from all archived messages that have it."""
    to_archive = g.label_repo.get_by_name("ZeroApp/To-Archive")
    if to_archive is None:
        flash("ZeroApp/To-Archive label not found.", "error")
        return redirect(url_for("main.archive"))

    msgs = g.msg_repo.list_by_raw_label(
        to_archive.id, is_archived=True, limit=500
    )
    if not msgs:
        flash("No archived messages with To-Archive label found.", "info")
        return redirect(url_for("main.archive"))

    request_dto = BulkLabelOperationRequest(
        message_ids=tuple(m.id for m in msgs),
        remove_label_ids=frozenset({to_archive.id}),
    )
    updated = g.label_svc.apply_bulk_label_operation(request_dto)
    flash(f"Removed To-Archive label from {len(updated)} message(s).", "success")
    return redirect(url_for("main.archive"))


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

    to_archive_label = g.label_repo.get_by_name("ZeroApp/To-Archive")
    to_remove_label = g.label_repo.get_by_name("ZeroApp/To-Remove")
    workflow_label_ids = frozenset(
        lbl.id for lbl in [to_archive_label, to_remove_label] if lbl is not None
    )
    workflow_labeled_message_ids = frozenset(
        m.id for m in messages if m.label_ids & workflow_label_ids
    )

    group_by = _parse_group_param()
    grouped_by_domain = _group_messages_by_domain(messages) if group_by else {}

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
        workflow_labeled_message_ids=workflow_labeled_message_ids,
        group_by=group_by,
        grouped_by_domain=grouped_by_domain,
        page_info=get_page_description("inbox"),
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
    show_wf = _show_workflow_labels()
    group_by = _parse_group_param(default=True)
    sort_by, sort_dir = _parse_sort_params(
        default_sort_by="sender_domain",
        default_sort_dir="asc",
        allowed_sort_by={"sender_domain", "sender", "subject", "internal_date", "size_estimate"},
    )
    unlabelled = g.msg_repo.list_archive_unlabelled(
        limit=200,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total_count = g.msg_repo.count_archive_unlabelled()
    to_archive_label = g.label_repo.get_by_name("ZeroApp/To-Archive")
    to_remove_label = g.label_repo.get_by_name("ZeroApp/To-Remove")

    archive_to_archive_count = (
        g.msg_repo.count_by_raw_label(to_archive_label.id, is_archived=True)
        if to_archive_label is not None
        else 0
    )

    # Collect workflow label IDs for template filtering
    workflow_label_ids = frozenset(
        lbl.id for lbl in [to_archive_label, to_remove_label] if lbl is not None
    )

    # Archived messages carrying a workflow label are excluded from
    # list_archive_unlabelled (has_custom_label=True), so fetch and merge them
    # here.  They are *always* included in the list — the Show/Hide toggle is a
    # pure presentation concern handled in the template via
    # workflow_labeled_message_ids, mirroring the inbox/sent/size pages.
    # Membership is resolved from raw_label_ids, so it is correct even if the
    # message_labels junction is stale.
    combined: list[Message] = list(unlabelled)
    seen_ids = {m.id for m in combined}
    for wf_label in (to_archive_label, to_remove_label):
        if wf_label is None:
            continue
        wf_msgs = g.msg_repo.list_by_raw_label(
            wf_label.id, is_archived=True, limit=200
        )
        for msg in wf_msgs:
            if msg.id not in seen_ids:
                combined.append(msg)
                seen_ids.add(msg.id)

    workflow_labeled_message_ids = frozenset(
        m.id for m in combined if m.label_ids & workflow_label_ids
    )

    # Re-sort the merged list on the active key so workflow messages appended
    # after the unlabelled query are ordered consistently within their group.
    sort_key = _ARCHIVE_SORT_KEYS.get(sort_by, _ARCHIVE_SORT_KEYS["sender_domain"])
    combined.sort(key=sort_key, reverse=sort_dir == "desc")

    # Group by sender_domain
    grouped_by_domain: dict[str, list[Message]] = defaultdict(list)
    for msg in combined:
        grouped_by_domain[msg.sender_domain].append(msg)
    grouped_by_domain = dict(sorted(grouped_by_domain.items(), key=lambda item: item[0]))

    return render_template(
        "archive.html",
        messages=combined,
        total_count=total_count,
        grouped_by_domain=dict(grouped_by_domain),
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=show_wf,
        archive_to_archive_count=archive_to_archive_count,
        workflow_labeled_message_ids=workflow_labeled_message_ids,
        group_by=group_by,
        page_info=get_page_description("archive"),
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

    to_archive_label = g.label_repo.get_by_name("ZeroApp/To-Archive")
    to_remove_label = g.label_repo.get_by_name("ZeroApp/To-Remove")
    workflow_label_ids = frozenset(
        lbl.id for lbl in [to_archive_label, to_remove_label] if lbl is not None
    )
    workflow_labeled_message_ids = frozenset(
        m.id for m in messages if m.label_ids & workflow_label_ids
    )

    group_by = _parse_group_param()
    grouped_by_domain = _group_sent_by_recipient_domain(messages) if group_by else {}

    return render_template(
        "sent.html",
        messages=messages,
        total_count=total_count,
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=_show_workflow_labels(),
        workflow_labeled_message_ids=workflow_labeled_message_ids,
        group_by=group_by,
        grouped_by_domain=grouped_by_domain,
        page_info=get_page_description("sent"),
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

    to_archive_label = g.label_repo.get_by_name("ZeroApp/To-Archive")
    to_remove_label = g.label_repo.get_by_name("ZeroApp/To-Remove")
    workflow_label_ids = frozenset(
        lbl.id for lbl in [to_archive_label, to_remove_label] if lbl is not None
    )
    workflow_labeled_message_ids = frozenset(
        m.id for m in messages if m.label_ids & workflow_label_ids
    )

    group_by = _parse_group_param()
    grouped_by_domain = _group_messages_by_domain(messages) if group_by else {}

    return render_template(
        "size.html",
        messages=messages,
        total_size_bytes=total_size_bytes,
        total_size_gb=total_size_gb,
        user_labels=g.label_repo.list_user_labels(),
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_workflow_labels=_show_workflow_labels(),
        workflow_labeled_message_ids=workflow_labeled_message_ids,
        group_by=group_by,
        grouped_by_domain=grouped_by_domain,
        page_info=get_page_description("size"),
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

    to_archive_label = g.label_repo.get_by_name("ZeroApp/To-Archive")
    to_remove_label = g.label_repo.get_by_name("ZeroApp/To-Remove")
    workflow_label_ids = frozenset(
        lbl.id for lbl in [to_archive_label, to_remove_label] if lbl is not None
    )
    workflow_labeled_message_ids = frozenset(
        m.id for m in messages if m.label_ids & workflow_label_ids
    )

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

    group_by = _parse_group_param()
    grouped_by_domain = _group_messages_by_domain(messages) if group_by else {}

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
        workflow_labeled_message_ids=workflow_labeled_message_ids,
        group_by=group_by,
        grouped_by_domain=grouped_by_domain,
    )


# ## Labels ##


@main_bp.route("/labels/create", methods=["POST"])
def create_label() -> Response:
    """Create a new ZeroApp-namespaced label in Gmail and sync it to the local DB."""
    name = request.form.get("label_name", "").strip()
    if not name:
        flash("Label name is required.", "error")
        return redirect(url_for("main.settings_page"))
    full_name = f"ZeroApp/{name}" if not name.startswith("ZeroApp/") else name
    raw = g.client.create_label(full_name)
    label = Label(
        id=raw["id"],
        name=raw["name"],
        label_type=LabelType.USER,
        message_list_visibility=(
            MessageListVisibility(raw["messageListVisibility"])
            if raw.get("messageListVisibility")
            else None
        ),
        label_list_visibility=(
            LabelListVisibility(raw["labelListVisibility"])
            if raw.get("labelListVisibility")
            else None
        ),
        synced_at=datetime.now(UTC),
    )
    g.label_repo.upsert(label)
    flash(f"Label '{full_name}' created.", "success")
    return redirect(url_for("main.settings_page"))


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
