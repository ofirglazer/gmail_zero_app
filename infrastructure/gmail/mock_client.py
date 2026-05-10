"""
MockGmailClient — deterministic fake Gmail API client for demo mode and tests.

Implements AbstractGmailClient with an in-memory dataset of ~200 synthetic
messages.  No network calls, no OAuth, no Gmail credentials required.

The dataset deliberately covers every problem state that the application's
workflows need to surface:

    Inbox Zero targets:
        - Old unread messages (30-900 days old)
        - Large messages in inbox (> 5 MB, > 15 MB)
        - Messages with and without custom labels
        - Multi-message threads

    Archive Hygiene targets:
        - Archived messages with no custom label (the main target)
        - Archived messages that already have a label (already processed)
        - Messages grouped by sender domain for bulk-by-domain workflow

    Sent Review targets:
        - Sent messages where user's message is the last in thread (no reply)
        - Sent messages with replies (thread has newer messages)
        - Old sent messages (> 30 days, > 90 days)

    Size Reduction targets:
        - Very large messages (> 15 MB) in inbox and archive
        - Large messages (5-15 MB) across all locations
        - Top senders by total size

    Label coverage:
        - Mix of labelled and unlabelled messages across all locations
        - Multiple label combinations per message

Demo mode details:
    - Reproducible: same data every time (no random seed needed)
    - Mutable: label operations mutate in-memory state, reflected in
      subsequent list_messages / get_message calls
    - History simulation: each ``simulate_sync_advance()`` call adds a
      small batch of new messages and advances the historyId
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

# ── Synthetic dataset helpers ─────────────────────────────────────────────────

_NOW = datetime(2024, 3, 15, 9, 0, 0, tzinfo=UTC)


def _ms(dt: datetime) -> str:
    """Convert datetime to Gmail internalDate (milliseconds since epoch, as string)."""
    return str(int(dt.timestamp() * 1000))


def _days_ago(n: int) -> datetime:
    return _NOW - timedelta(days=n)


def _mb(mb: float) -> int:
    """Convert megabytes to bytes."""
    return int(mb * 1024 * 1024)


def _make_msg(
    msg_id: str,
    thread_id: str,
    history_id: str,
    days_old: int,
    sender: str,
    subject: str,
    label_ids: list[str],
    size_bytes: int = 8_000,
    recipient: str = "me@gmail.com",
    snippet: str = "",
) -> dict[str, Any]:
    """Build a Gmail API-shaped message dict."""
    dt = _days_ago(days_old)
    return {
        "id": msg_id,
        "threadId": thread_id,
        "historyId": history_id,
        "internalDate": _ms(dt),
        "sizeEstimate": size_bytes,
        "snippet": snippet or f"Message content from {sender}...",
        "labelIds": label_ids,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": recipient},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": dt.strftime("%a, %d %b %Y %H:%M:%S +0000")},
            ]
        },
    }


# ── Synthetic dataset ─────────────────────────────────────────────────────────

# Label IDs used across the dataset
_LABEL_NEEDS_ACTION = "Label_NeedsAction001"
_LABEL_COMPLETE = "Label_Complete001"
_LABEL_AWAITING = "Label_AwaitingReply001"
_LABEL_NEWSLETTER = "Label_Newsletter001"
_LABEL_RECEIPT = "Label_Receipt001"
_LABEL_LARGE = "Label_LargeMessage001"
_LABEL_REVIEW = "Label_Review001"
_LABEL_FOLLOWUP = "Label_FollowUp001"
_LABEL_TO_REMOVE = "Label_ToRemove001"

# All user-defined labels (for labels list API response)
_USER_LABELS: list[dict[str, Any]] = [
    {
        "id": _LABEL_NEEDS_ACTION,
        "name": "ZeroApp/Needs-Action",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_COMPLETE,
        "name": "ZeroApp/Complete",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_AWAITING,
        "name": "ZeroApp/Awaiting-Reply",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_NEWSLETTER,
        "name": "ZeroApp/Newsletter",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_RECEIPT,
        "name": "ZeroApp/Receipt",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_LARGE,
        "name": "ZeroApp/Large-Message",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_REVIEW,
        "name": "ZeroApp/Review",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_FOLLOWUP,
        "name": "ZeroApp/Follow-Up",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
    {
        "id": _LABEL_TO_REMOVE,
        "name": "ZeroApp/To-Remove",
        "type": "user",
        "messageListVisibility": "show",
        "labelListVisibility": "labelShow",
    },
]

_SYSTEM_LABELS: list[dict[str, Any]] = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "SENT", "name": "SENT", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    {"id": "STARRED", "name": "STARRED", "type": "system"},
    {"id": "IMPORTANT", "name": "IMPORTANT", "type": "system"},
    {"id": "TRASH", "name": "TRASH", "type": "system"},
    {"id": "SPAM", "name": "SPAM", "type": "system"},
    {"id": "DRAFT", "name": "DRAFT", "type": "system"},
    {"id": "CATEGORY_PERSONAL", "name": "CATEGORY_PERSONAL", "type": "system"},
    {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS", "type": "system"},
    {"id": "CATEGORY_SOCIAL", "name": "CATEGORY_SOCIAL", "type": "system"},
    {"id": "CATEGORY_UPDATES", "name": "CATEGORY_UPDATES", "type": "system"},
    {"id": "CATEGORY_FORUMS", "name": "CATEGORY_FORUMS", "type": "system"},
]


def _build_initial_dataset() -> dict[str, dict[str, Any]]:
    """
    Build the full synthetic dataset as a message_id → API dict mapping.

    Returns a new dict each time so mutations don't affect the template.
    """
    msgs: list[dict[str, Any]] = [

        # ── INBOX: Old unread messages (Inbox Zero targets) ───────────────────

        _make_msg("inbox001", "thread001", "h1001", 847,
                  "boss@corp.com", "Q3 budget review — please action",
                  ["INBOX", "UNREAD", "IMPORTANT"], size_bytes=42_000,
                  snippet="Hi, please can you review the Q3 budget before the deadline"),

        _make_msg("inbox002", "thread002", "h1002", 720,
                  "hr@corp.com", "Annual leave policy update 2022",
                  ["INBOX", "UNREAD", _LABEL_COMPLETE], size_bytes=18_500,
                  snippet="Please read the updated annual leave policy attached"),

        _make_msg("inbox003", "thread003", "h1003", 610,
                  "newsletter@acme.com", "Monthly digest — November 2022",
                  ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"], size_bytes=_mb(1.2),
                  snippet="Your November digest is here. Highlights this month include"),

        _make_msg("inbox004", "thread004", "h1004", 540,
                  "alerts@github.com", "Security alert: new sign-in from Chrome on Windows",
                  ["INBOX", "UNREAD", "CATEGORY_UPDATES"], size_bytes=9_800,
                  snippet="We noticed a new sign-in to your account from Chrome on Windows"),

        _make_msg("inbox005", "thread005", "h1005", 480,
                  "noreply@aws.com", "Your AWS bill for January 2023 is ready",
                  ["INBOX", "UNREAD"], size_bytes=22_400,
                  snippet="Your AWS bill for January 2023 is $1,247.82"),

        _make_msg("inbox006", "thread006", "h1006", 420,
                  "updates@linkedin.com", "John Smith viewed your profile",
                  ["INBOX", "UNREAD", "CATEGORY_SOCIAL"], size_bytes=7_200,
                  snippet="John Smith viewed your LinkedIn profile 3 times this week"),

        _make_msg("inbox007", "thread007", "h1007", 380,
                  "no-reply@notion.so", "Your workspace summary for March",
                  ["INBOX", "UNREAD", "CATEGORY_UPDATES"], size_bytes=34_000,
                  snippet="Here is your Notion workspace activity summary for March 2023"),

        _make_msg("inbox008", "thread008", "h1008", 320,
                  "cto@partner.com", "RE: Contract renewal Q4 — need your input",
                  ["INBOX", "UNREAD", "IMPORTANT"], size_bytes=15_800,
                  snippet="Following up on the contract renewal. We need to finalise by"),

        _make_msg("inbox009", "thread009", "h1009", 280,
                  "recruiter@bigtech.com", "Exciting opportunity at BigTech",
                  ["INBOX", "UNREAD", "CATEGORY_PERSONAL"], size_bytes=12_000,
                  snippet="Hi, I came across your profile and think you would be a great fit"),

        _make_msg("inbox010", "thread010", "h1010", 240,
                  "newsletter@acme.com", "Monthly digest — July 2023",
                  ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"], size_bytes=_mb(1.4),
                  snippet="Your July digest is here with highlights from this month"),

        _make_msg("inbox011", "thread011", "h1011", 210,
                  "support@stripe.com", "Action required: verify your account",
                  ["INBOX", "UNREAD", "IMPORTANT"], size_bytes=8_900,
                  snippet="We need you to verify your Stripe account to continue"),

        _make_msg("inbox012", "thread012", "h1012", 180,
                  "noreply@slack.com", "You have 47 unread messages in #general",
                  ["INBOX", "UNREAD", "CATEGORY_UPDATES"], size_bytes=6_500,
                  snippet="You have missed messages in 5 channels including #general"),

        _make_msg("inbox013", "thread013", "h1013", 150,
                  "alerts@github.com", "Dependabot alert: 3 vulnerabilities found",
                  ["INBOX", "UNREAD", "CATEGORY_UPDATES"], size_bytes=11_200,
                  snippet="Dependabot found 3 security vulnerabilities in your repository"),

        _make_msg("inbox014", "thread014", "h1014", 120,
                  "boss@corp.com", "Q1 planning kickoff — save the date",
                  ["INBOX", "UNREAD", _LABEL_NEEDS_ACTION], size_bytes=5_800,
                  snippet="Saving the date for Q1 planning kickoff. Please review the agenda"),

        _make_msg("inbox015", "thread015", "h1015", 90,
                  "team@co.com", "Sprint retrospective notes",
                  ["INBOX", "UNREAD", _LABEL_REVIEW], size_bytes=28_000,
                  snippet="Attached are the notes from last week's sprint retrospective"),

        _make_msg("inbox016", "thread016", "h1016", 75,
                  "vendor@supply.com", "Invoice #2024-0847 is overdue",
                  ["INBOX", "UNREAD", "IMPORTANT", _LABEL_NEEDS_ACTION], size_bytes=14_200,
                  snippet="Invoice #2024-0847 for $3,420 is now 30 days overdue"),

        _make_msg("inbox017", "thread017", "h1017", 60,
                  "newsletter@tech.io", "This week in tech",
                  ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"], size_bytes=_mb(0.8),
                  snippet="Top stories this week in tech: AI legislation, chip shortage update"),

        _make_msg("inbox018", "thread018", "h1018", 45,
                  "alerts@github.com", "New pull request: feature/auth-refactor",
                  ["INBOX", "UNREAD", "CATEGORY_UPDATES"], size_bytes=9_300,
                  snippet="Alice opened a new pull request: feature/auth-refactor"),

        _make_msg("inbox019", "thread019", "h1019", 30,
                  "finance@corp.com", "Expense report reminder",
                  ["INBOX", "UNREAD"], size_bytes=7_800,
                  snippet="This is a reminder to submit your expense report for February"),

        _make_msg("inbox020", "thread020", "h1020", 15,
                  "noreply@aws.com", "Your AWS bill for February 2024 is ready",
                  ["INBOX", "UNREAD"], size_bytes=19_600,
                  snippet="Your AWS bill for February 2024 is $1,389.14"),

        # ── INBOX: Large messages (Size Reduction targets) ────────────────────

        _make_msg("inbox_large001", "thread_large001", "h2001", 95,
                  "reports@bigco.com", "Q3 Data Export — Full Dataset",
                  ["INBOX", "UNREAD", _LABEL_LARGE], size_bytes=_mb(22.4),
                  snippet="Attached is the full Q3 data export as requested by leadership"),

        _make_msg("inbox_large002", "thread_large002", "h2002", 62,
                  "reports@bigco.com", "Annual Report 2023 — Draft for Review",
                  ["INBOX", "UNREAD"], size_bytes=_mb(16.8),
                  snippet="Please find attached the annual report draft for your review"),

        _make_msg("inbox_large003", "thread_large003", "h2003", 40,
                  "analytics@dataplatform.io", "Monthly Analytics Export — March",
                  ["INBOX", "UNREAD", _LABEL_LARGE], size_bytes=_mb(8.9),
                  snippet="Your monthly analytics export for March is ready for download"),

        _make_msg("inbox_large004", "thread_large004", "h2004", 28,
                  "newsletter@acme.com", "Annual Report PDF — Member Edition",
                  ["INBOX", "UNREAD"], size_bytes=_mb(6.2),
                  snippet="Your member edition of the annual report is attached"),

        # ── INBOX: Multi-message thread examples ──────────────────────────────

        # Thread 100: 3-message conversation in inbox
        _make_msg("inbox_th001a", "thread100", "h3001", 45,
                  "alice@startup.io", "RE: Integration proposal",
                  ["INBOX", "UNREAD"], size_bytes=12_000,
                  snippet="Thanks for the proposal. I have a few questions about the API"),

        _make_msg("inbox_th001b", "thread100", "h3002", 43,
                  "me@gmail.com", "RE: RE: Integration proposal",
                  ["INBOX", "SENT"], size_bytes=8_500,
                  snippet="Happy to answer your questions. The API uses OAuth2 for auth"),

        _make_msg("inbox_th001c", "thread100", "h3003", 41,
                  "alice@startup.io", "RE: RE: RE: Integration proposal",
                  ["INBOX", "UNREAD"], size_bytes=9_200,
                  snippet="Great, that clarifies things. When can we schedule a demo?"),

        # Thread 101: 2-message thread where user's last message has no reply
        _make_msg("inbox_th002a", "thread101", "h3010", 60,
                  "supplier@co.com", "New pricing structure 2024",
                  ["INBOX", "UNREAD"], size_bytes=15_200,
                  snippet="Please find our updated pricing structure for 2024 attached"),

        _make_msg("inbox_th002b", "thread101", "h3011", 57,
                  "me@gmail.com", "RE: New pricing structure 2024",
                  ["INBOX", "SENT", _LABEL_AWAITING], size_bytes=6_800,
                  snippet="Thank you for sending this over. I will review and come back to you"),

        # ── SENT: Messages awaiting reply (Sent Review targets) ───────────────

        _make_msg("sent001", "thread200", "h4001", 94,
                  "me@gmail.com", "Contract renewal Q4 — initial proposal",
                  ["SENT"], size_bytes=8_200, recipient="cto@partner.com",
                  snippet="Please find our proposal for Q4 contract renewal attached"),

        _make_msg("sent002", "thread201", "h4002", 58,
                  "me@gmail.com", "Sprint planning notes — action items",
                  ["SENT"], size_bytes=4_800, recipient="team@co.com",
                  snippet="Please find the sprint planning action items from yesterday"),

        _make_msg("sent003", "thread202", "h4003", 42,
                  "me@gmail.com", "Vendor invoice query",
                  ["SENT", _LABEL_FOLLOWUP], size_bytes=3_200, recipient="vendor@supply.com",
                  snippet="I am writing to query invoice #2024-0412 which shows an unexpected"),

        _make_msg("sent004", "thread203", "h4004", 37,
                  "me@gmail.com", "Architecture review feedback",
                  ["SENT"], size_bytes=11_000, recipient="arch@corp.com",
                  snippet="After reviewing the architecture proposal, here are my thoughts"),

        _make_msg("sent005", "thread204", "h4005", 28,
                  "me@gmail.com", "RE: Budget approval request",
                  ["SENT", _LABEL_AWAITING], size_bytes=5_600, recipient="finance@corp.com",
                  snippet="I have reviewed the budget request and would like to discuss"),

        _make_msg("sent006", "thread205", "h4006", 21,
                  "me@gmail.com", "Project milestone update",
                  ["SENT"], size_bytes=7_200, recipient="pm@corp.com",
                  snippet="Here is the project milestone update for the board meeting"),

        _make_msg("sent007", "thread206", "h4007", 14,
                  "me@gmail.com", "RE: Partnership opportunity",
                  ["SENT"], size_bytes=4_100, recipient="biz@partnerco.com",
                  snippet="Thank you for reaching out about the partnership opportunity"),

        _make_msg("sent008", "thread207", "h4008", 7,
                  "me@gmail.com", "Q1 goals — draft for review",
                  ["SENT", _LABEL_REVIEW], size_bytes=9_800, recipient="boss@corp.com",
                  snippet="Please find the Q1 goals draft attached. Happy to discuss"),

        # Sent with replies (not awaiting reply — thread continues)
        _make_msg("sent_replied001", "thread208", "h4020", 3,
                  "me@gmail.com", "Invoice query followup",
                  ["SENT", _LABEL_COMPLETE], size_bytes=3_100, recipient="vendor@supply.com",
                  snippet="Thank you for resolving the invoice issue so quickly"),

        _make_msg("sent_replied002", "thread209", "h4021", 2,
                  "me@gmail.com", "RE: Welcome to the team",
                  ["SENT", _LABEL_COMPLETE], size_bytes=2_800, recipient="newbie@corp.com",
                  snippet="Welcome to the team! Happy to help you get settled in"),

        # ── SENT: Very large sent messages ────────────────────────────────────

        _make_msg("sent_large001", "thread_sl001", "h4030", 45,
                  "me@gmail.com", "Backup export Jan 2023",
                  ["SENT", _LABEL_LARGE], size_bytes=_mb(14.1), recipient="storage@backup.io",
                  snippet="Attaching the January 2023 backup export as discussed"),

        _make_msg("sent_large002", "thread_sl002", "h4031", 90,
                  "me@gmail.com", "Board presentation — full deck",
                  ["SENT"], size_bytes=_mb(7.3), recipient="board@corp.com",
                  snippet="Please find the board presentation for Q3 attached"),

        # ── ARCHIVE: Unlabelled messages (Archive Hygiene targets) ────────────

        # Bulk newsletters from the same domain — easy to bulk-label
        *[
            _make_msg(
                f"arc_news{i:03d}", f"thread_arc_news{i:03d}", f"h5{i:03d}",
                days_old=30 + i * 3,
                sender="newsletter@acme.com",
                subject=(
                    "Monthly digest — "
                    + ["Jan","Feb","Mar","Apr","May","Jun",
                       "Jul","Aug","Sep","Oct","Nov","Dec"][i % 12]
                    + " 2023"
                ),
                label_ids=[],
                size_bytes=_mb(0.8 + (i % 5) * 0.2),
                snippet="Your monthly digest for the month includes the latest updates",
            )
            for i in range(20)
        ],

        # GitHub alerts — another bulk domain
        *[
            _make_msg(
                f"arc_gh{i:03d}", f"thread_arc_gh{i:03d}", f"h6{i:03d}",
                days_old=15 + i * 5,
                sender="alerts@github.com",
                subject=(
                    "Dependabot alert: vulnerability in "
                    + ("requests" if i % 3 == 0 else "django" if i % 3 == 1 else "sqlalchemy")
                ),
                label_ids=[],
                size_bytes=8_500 + i * 200,
                snippet="Dependabot has found a security vulnerability in one of your dependencies",
            )
            for i in range(15)
        ],

        # AWS billing notifications
        *[
            _make_msg(
                f"arc_aws{i:03d}", f"thread_arc_aws{i:03d}", f"h7{i:03d}",
                days_old=28 + i * 30,
                sender="noreply@aws.com",
                subject=f"Your AWS bill for month {i + 1} 2023 is ready",
                label_ids=[],
                size_bytes=21_000 + i * 500,
                snippet=f"Your AWS bill is ready. Total charges this month: ${900 + i * 50:.2f}",
            )
            for i in range(12)
        ],

        # Mixed senders — various archived unlabelled
        _make_msg("arc_misc001", "thread_arc_misc001", "h8001", 180,
                  "recruiter@jobs.io", "Exciting opportunity in SRE",
                  [], size_bytes=13_400,
                  snippet="I came across your profile and believe you would be a great fit"),

        _make_msg("arc_misc002", "thread_arc_misc002", "h8002", 220,
                  "no-reply@calendar.google.com", "Invitation: Q4 All-hands",
                  [], size_bytes=6_200,
                  snippet="You have been invited to Q4 All-hands on December 15"),

        _make_msg("arc_misc003", "thread_arc_misc003", "h8003", 310,
                  "notifications@trello.com", "Bob added a card to your board",
                  [], size_bytes=4_800,
                  snippet="Bob added a new card to the Engineering board: API redesign"),

        _make_msg("arc_misc004", "thread_arc_misc004", "h8004", 400,
                  "updates@linkedin.com", "You have 5 new connection requests",
                  [], size_bytes=8_900,
                  snippet="You have 5 new connection requests this week"),

        _make_msg("arc_misc005", "thread_arc_misc005", "h8005", 510,
                  "noreply@medium.com", "Stories for you this week",
                  [], size_bytes=24_000,
                  snippet="Top stories from publications you follow this week"),

        # ── ARCHIVE: Already labelled (previously processed) ──────────────────

        *[
            _make_msg(
                f"arc_labelled{i:03d}", f"thread_arc_lab{i:03d}", f"h9{i:03d}",
                days_old=60 + i * 10,
                sender=["newsletter@acme.com", "alerts@github.com", "noreply@aws.com"][i % 3],
                subject=f"Previously labelled message {i}",
                label_ids=[
                    [_LABEL_NEWSLETTER, _LABEL_COMPLETE, _LABEL_RECEIPT][i % 3]
                ],
                size_bytes=10_000 + i * 1_000,
                snippet="This message has already been processed and labelled",
            )
            for i in range(15)
        ],

        # ── ARCHIVE: Large archived messages (Size Reduction targets) ─────────

        _make_msg("arc_vl001", "thread_arc_vl001", "hA001", 120,
                  "reports@bigco.com", "Q2 Data Export — Full Dataset",
                  [_LABEL_LARGE], size_bytes=_mb(22.1),
                  snippet="Q2 full data export. Please archive after review"),

        _make_msg("arc_vl002", "thread_arc_vl002", "hA002", 240,
                  "reports@bigco.com", "Annual Report 2022 with Appendices",
                  [_LABEL_LARGE], size_bytes=_mb(18.7),
                  snippet="Annual report 2022 including all appendices and financial statements"),

        _make_msg("arc_vl003", "thread_arc_vl003", "hA003", 180,
                  "analytics@dataplatform.io", "Q3 Analytics Full Export",
                  [], size_bytes=_mb(11.4),
                  snippet="Your Q3 analytics full export is attached"),

        _make_msg("arc_vl004", "thread_arc_vl004", "hA004", 90,
                  "newsletter@acme.com", "Annual Report PDF 2023",
                  [], size_bytes=_mb(7.8),
                  snippet="Your annual report for 2023 is now available"),

        _make_msg("arc_vl005", "thread_arc_vl005", "hA005", 365,
                  "reports@bigco.com", "Year-end Data Archive 2022",
                  [_LABEL_LARGE], size_bytes=_mb(15.2),
                  snippet="Year-end data archive as requested. Please confirm receipt"),
    ]

    return {msg["id"]: msg for msg in msgs}


class MockGmailClient:
    """
    In-memory Gmail API client returning deterministic synthetic data.

    Implements AbstractGmailClient without making any network calls.
    Used in demo mode and integration tests.

    Label operations (``modify_message_labels``) mutate internal state and
    are immediately reflected in subsequent ``get_message`` / ``list_messages``
    calls — simulating a real Gmail account.

    A mock history ID is maintained and advances with each simulated sync
    cycle via ``advance_history()``.

    Args:
        user_email: Email address to report as the authenticated user.
    """

    _DEMO_USER_EMAIL: str = "demo@gmail.com"
    _BASE_HISTORY_ID: str = "1000000"

    def __init__(self, user_email: str = _DEMO_USER_EMAIL) -> None:
        self._user_email = user_email
        self._history_id: int = int(self._BASE_HISTORY_ID)
        # Deep-copy the dataset so mutations don't affect the class-level template
        self._messages: dict[str, dict[str, Any]] = _build_initial_dataset()
        self._history_events: list[dict[str, Any]] = []

    # ── AbstractGmailClient interface ─────────────────────────────────────────

    def list_messages(
        self,
        *,
        max_results: int = 500,
        page_token: str | None = None,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """Return a filtered page of message stubs."""
        filtered = self._filter_messages(label_ids, include_spam_trash)
        # Simple pagination: page_token encodes a numeric offset
        offset = int(page_token) if page_token and page_token.isdigit() else 0
        page = filtered[offset: offset + max_results]
        result: dict[str, Any] = {
            "messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page]
        }
        if offset + max_results < len(filtered):
            result["nextPageToken"] = str(offset + max_results)
        return result

    def get_message(
        self,
        message_id: str,
        *,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a single message by ID."""
        if message_id not in self._messages:
            raise KeyError(f"Message not found: {message_id!r}")
        # Return a deep copy so the caller cannot mutate internal state
        return copy.deepcopy(self._messages[message_id])

    def batch_get_messages(
        self,
        message_ids: list[str],
        *,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return metadata for multiple messages."""
        return [self.get_message(mid, format=format) for mid in message_ids]

    def list_labels(self) -> dict[str, Any]:
        """Return all labels (system + user-defined)."""
        return {"labels": _SYSTEM_LABELS + _USER_LABELS}

    def get_label(self, label_id: str) -> dict[str, Any]:
        """Return a single label by ID."""
        all_labels = {lbl["id"]: lbl for lbl in _SYSTEM_LABELS + _USER_LABELS}
        if label_id not in all_labels:
            raise KeyError(f"Label not found: {label_id!r}")
        return copy.deepcopy(all_labels[label_id])

    def modify_message_labels(
        self,
        message_id: str,
        *,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Apply label changes to a message and advance the history ID.

        Mutates internal state — immediately reflected in subsequent calls.
        Records a history event for incremental sync simulation.
        """
        if message_id not in self._messages:
            raise KeyError(f"Message not found: {message_id!r}")

        msg = self._messages[message_id]
        current_labels: set[str] = set(msg["labelIds"])

        # Increment first so history events carry the new (higher) ID,
        # making them visible in get_history(start_history_id=old_id).
        self._history_id += 1
        new_history_id = str(self._history_id)

        if add_label_ids:
            for lid in add_label_ids:
                current_labels.add(lid)
            self._record_labels_added(message_id, add_label_ids, new_history_id)

        if remove_label_ids:
            for lid in remove_label_ids:
                current_labels.discard(lid)
            self._record_labels_removed(message_id, remove_label_ids, new_history_id)

        msg["labelIds"] = sorted(current_labels)
        msg["historyId"] = new_history_id

        return copy.deepcopy(msg)

    def get_history(
        self,
        *,
        start_history_id: str,
        history_types: list[str] | None = None,
        max_results: int = 500,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Return history events since the given history ID."""
        start = int(start_history_id)
        # Filter events to those after start_history_id
        relevant = [
            e for e in self._history_events
            if int(e.get("id", "0")) > start
        ][:max_results]
        result: dict[str, Any] = {
            "historyId": str(self._history_id),
            "history": relevant,
        }
        return result

    def get_profile(self) -> dict[str, Any]:
        """Return mock user profile."""
        return {
            "emailAddress": self._user_email,
            "messagesTotal": len(self._messages),
            "threadsTotal": len({m["threadId"] for m in self._messages.values()}),
            "historyId": str(self._history_id),
        }

    # ── Demo-specific helpers ─────────────────────────────────────────────────

    def advance_history(self, new_message_count: int = 3) -> None:
        """
        Simulate the passage of time by adding new messages and advancing
        the history ID.

        Called between simulated sync cycles in integration tests and demo mode.

        Args:
            new_message_count: How many new inbox messages to add.
        """
        for i in range(new_message_count):
            self._history_id += 1
            new_id = f"sim_{self._history_id:08d}"
            new_msg = _make_msg(
                msg_id=new_id,
                thread_id=f"thread_sim_{self._history_id}",
                history_id=str(self._history_id),
                days_old=0,
                sender=f"sender{i}@newsender.com",
                subject=f"Simulated new message {self._history_id}",
                label_ids=["INBOX", "UNREAD"],
                size_bytes=5_000 + i * 1_000,
                snippet="This is a simulated new message for testing incremental sync",
            )
            self._messages[new_id] = new_msg
            self._history_events.append({
                "id": str(self._history_id),
                "messagesAdded": [{"message": {"id": new_id, "threadId": new_msg["threadId"]}}],
            })

    @property
    def current_history_id(self) -> str:
        """The current mock history ID."""
        return str(self._history_id)

    @property
    def user_email(self) -> str:
        """The mock user email address."""
        return self._user_email

    def message_count(self) -> int:
        """Total number of messages in the mock dataset."""
        return len(self._messages)

    def inbox_count(self) -> int:
        """Number of messages currently in the mock inbox."""
        return sum(1 for m in self._messages.values() if "INBOX" in m["labelIds"])

    def get_label_ids_for_message(self, message_id: str) -> frozenset[str]:
        """Return the current label IDs for a message (for test assertions)."""
        return frozenset(self._messages[message_id]["labelIds"])

    # ── Private helpers ───────────────────────────────────────────────────────

    def _filter_messages(
        self,
        label_ids: list[str] | None,
        include_spam_trash: bool,
    ) -> list[dict[str, Any]]:
        """
        Filter the internal message store by label IDs.

        Args:
            label_ids:          If provided, only return messages having ALL
                                listed labels.
            include_spam_trash: If False, exclude TRASH and SPAM messages.

        Returns:
            Sorted list of matching message dicts (newest first).
        """
        results: list[dict[str, Any]] = []
        for msg in self._messages.values():
            msg_labels: set[str] = set(msg["labelIds"])

            if not include_spam_trash and ("TRASH" in msg_labels or "SPAM" in msg_labels):
                continue

            if label_ids and not all(lid in msg_labels for lid in label_ids):
                continue

            results.append(msg)

        # Sort by internalDate descending (newest first) — consistent paging
        results.sort(key=lambda m: int(m["internalDate"]), reverse=True)
        return results

    def _record_labels_added(
        self, message_id: str, label_ids: list[str], history_id: str
    ) -> None:
        """Record a labelsAdded history event."""
        self._history_events.append({
            "id": history_id,
            "labelsAdded": [
                {"message": {"id": message_id}, "labelIds": label_ids}
            ],
        })

    def _record_labels_removed(
        self, message_id: str, label_ids: list[str], history_id: str
    ) -> None:
        """Record a labelsRemoved history event."""
        self._history_events.append({
            "id": history_id,
            "labelsRemoved": [
                {"message": {"id": message_id}, "labelIds": label_ids}
            ],
        })
