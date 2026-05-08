"""
Domain model entities for gmail_zero_app.

All models are frozen dataclasses with no ORM or infrastructure dependencies.
Import from this package rather than from submodules to keep import paths stable.

    from domain.models import Message, Label, Thread, SyncState, DailySnapshot
    from domain.models import LabelType, SyncType, make_message
"""

from domain.models.daily_snapshot import DailySnapshot
from domain.models.label import Label, LabelListVisibility, LabelType, MessageListVisibility
from domain.models.message import Message, make_message
from domain.models.sync_state import SyncState, SyncType
from domain.models.thread import Thread

__all__ = [
    # Entities
    "DailySnapshot",
    "Label",
    # Enums
    "LabelListVisibility",
    "LabelType",
    "Message",
    "MessageListVisibility",
    "SyncState",
    "SyncType",
    "Thread",
    # Factories
    "make_message",
]
