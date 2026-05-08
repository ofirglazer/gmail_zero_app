"""
Repository implementations for gmail_zero_app.

Each repository provides typed CRUD and query operations for one aggregate.
All repositories accept a SQLAlchemy ``Session`` via constructor injection.

Implemented in Step 3:
    MessageRepository      — messages table
    LabelRepository        — labels + message_labels tables
    SyncStateRepository    — sync_state table
    SnapshotRepository     — daily_snapshots table (Step 8)
"""
