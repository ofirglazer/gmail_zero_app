## Not Yet Implemented (Future Enhancements)

Features listed in the PRD but not yet implemented:

### UI/UX
- **Keyboard shortcuts** — keyboard-driven navigation and labeling
- **Mobile responsiveness** — touch-friendly interface, swipe gestures
- **Dashboard counter styling** — "Ready to archive" and "Ready to delete" counters need visual improvement to match goal cards
- **Font size selector** — small/medium/large theme options

### Settings & Configuration
- **Sync schedule UI** — configure auto-sync frequency and timing
- **Label customization** — edit label names and create custom labels via UI
- **Desktop notifications** — browser/system notifications for sync completion and daily reminders
- **Data export** — export session history and label operations as CSV

### Core Features
- **CLI interface** — command-line management of labels and syncs
- **Automatic archiving** — auto-archive messages after labeling (currently user archives manually in Gmail)
- **OAuth token revocation** — logout button to revoke Gmail access
- **Import/export** — backup and restore local database

### Production Features
- **Log rotation** — implement `RotatingFileHandler` with 10MB max size
- **Email encryption at rest** — encrypt cached message bodies (optional)
- **Audit dashboard** — view all label operations with timestamps

---

## Setup & Deployment

### Environment Configuration

```bash
# Demo mode (default — synthetic data)
GMAIL_ZERO_ENV=demo python -m presentation.app

# Production mode (real Gmail)
GMAIL_ZERO_ENV=production GMAIL_ZERO_DEBUG=false python -m presentation.app
```

**Production requirements:**
- Generate strong `SECRET_KEY` (replace default in `.env`)
- Enable HTTPS (reverse proxy with Nginx or similar)
- Encrypt token file on disk

---

## Database & Backup

### Copying the Database to Another PC

The database is a standard SQLite file. To move it to another PC:

**Step 1: Locate the database**
```bash
# Default location (check .env or config/settings.py)
data/gmail_zero_app.db
```

**Step 2: Back up the database**
```powershell
# On source PC, with app stopped
Copy-Item -Path "data/gmail_zero_app.db" -Destination "backup_gmail_zero.db"
```

**Step 3: Transfer to target PC**
- Use USB drive, cloud sync, or email
- Place in target PC's `data/gmail_zero_app.db` (same path structure)

**Step 4: Verify**
```bash
# On target PC, start the app (it will read the existing database)
python -m presentation.app
```

**Notes:**
- The database contains only **metadata** (sender, subject, date, labels, size, snippets) — no email bodies or attachments
- OAuth credentials are stored separately in `data/credentials/` — transfer those too if using production mode
- The database is not encrypted by default; encrypt the backup if it contains sensitive metadata
- SQLite is cross-platform (Windows/Mac/Linux); the `.db` file works unchanged

**Backup strategy:**
```bash
# Automated daily backup
cp data/gmail_zero_app.db "backups/gmail_zero_$(date +%Y-%m-%d).db"
```

---

# gmail_zero_app

A production-grade local web application for Gmail mailbox metadata analysis
and safe label management, built around four operational zero-goals:

| Goal             | Target                                        |
|------------------|-----------------------------------------------|
| **Inbox Zero**   | Process inbox to zero messages                |
| **Archive Zero** | Zero archived messages without a custom label |
| **Sent Zero**    | Zero sent items requiring follow-up action    |
| **Size Zero**    | Reduce total mailbox storage footprint        |

> **Safety by design**: this application can only read metadata and
> manage labels. It cannot send, delete, archive, draft, or modify
> message bodies — by OAuth scope, by API whitelist, and by enforced
> safety guard.

---

## Requirements

- Python 3.11+
- A Google account with Gmail API access enabled
- Google Cloud project with OAuth 2.0 credentials (personal account type)

---

## Quick Start (Demo Mode)

Demo mode runs with synthetic data — no Gmail credentials required.

```bash
# 1. Clone and enter the project
git clone <repo-url> gmail_zero_app
cd gmail_zero_app

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements-dev.txt

# 4. Configure environment
cp .env.example .env
# .env already defaults to GMAIL_ZERO_ENV=demo — no further edits needed

# 5. Run tests (Step 1: skeleton validation)
pytest

# 6. Start the application
python -m presentation.app
```

Open http://127.0.0.1:5000 in your browser.

---

## Production Setup (Real Gmail)

Full setup instructions including OAuth configuration are documented in
`docs/setup_production.md` — generated in Step 8 of the build process.

---

## Project Structure

```
gmail_zero_app/
├── config/          # Settings, OAuth scopes, label configuration
├── domain/          # Pure domain models, exceptions, safety constants
├── application/     # Use cases and application services
├── infrastructure/  # Gmail API client, SQLite persistence, scheduler
├── presentation/    # Flask routes, Jinja2 templates, static assets
├── tests/           # Pytest suite (safety / unit / integration)
└── data/            # Runtime data — SQLite DB and OAuth credentials
```

---

## Safety Model

See `THREAT_MODEL.md` (generated in Step 8) for the full safety explanation.

The short version:

1. **OAuth scopes** — only `gmail.readonly` + `gmail.labels` are ever requested
2. **GmailClient whitelist** — only explicitly approved API methods are callable
3. **SafetyGuard** — validates every label operation against protected-label rules

All three layers must be independently defeated to perform a forbidden operation.

---

## Development

```bash
# Run all tests
pytest

# Run only safety tests (always run these first)
pytest -m safety

# Type checking
mypy .

# Linting
ruff check .

# Formatting check
ruff format --check .
ruff format --diff .
```

---

## Architecture

The application follows a strict layered architecture with no dependency leakage
between layers.

```
┌─────────────────────────────────────────────────────────┐
│  Presentation  Flask routes · Jinja2 templates · JS      │
├─────────────────────────────────────────────────────────┤
│  Application   LabelService · SyncService · Analytics   │
│                SearchService · LabelConfigService        │
├─────────────────────────────────────────────────────────┤
│  Domain        Message · Thread · Label · SyncState     │
│                DailySnapshot · SafetyGuard (stateless)  │
├─────────────────────────────────────────────────────────┤
│  Infrastructure  GmailClient · GmailMapper · OAuth      │
│                  MessageRepo · LabelRepo · SnapshotRepo  │
│                  SyncStateRepo · SyncScheduler          │
├─────────────────────────────────────────────────────────┤
│  Config        Settings (Pydantic) · OAuthScopes        │
└─────────────────────────────────────────────────────────┘
```

**Key design decisions:**

- **Immutable domain entities** — all domain models are frozen dataclasses;
  label changes produce new instances rather than mutating in place.
- **Three-layer safety** — OAuth scopes (Google-enforced), SafetyGuard
  (domain-layer validation), and GmailClient method whitelist (infrastructure
  enforcement). All three must be defeated independently to perform a forbidden
  operation.
- **Repository pattern** — data access is abstracted behind repositories;
  services never query the ORM directly.
- **Dependency injection** — services receive all dependencies through their
  constructors; Flask `g` carries request-scoped services.
- **Demo mode** — `MockGmailClient` provides synthetic data so the app runs
  fully without Gmail credentials.

See `docs/uml_diagrams.md` for class, data-flow, sequence, and ER diagrams.

---

## Operation

### First run (demo mode)

```bash
python -m presentation.app
```

On first launch the app seeds 90 days of synthetic snapshots and starts the
background sync scheduler. Open `http://127.0.0.1:5000`.

### Sync behaviour

- **Full sync** — fetches all message metadata in batches of 100; rate-limited
  at 50 ms between batches to stay within Gmail quota.
- **Incremental sync** — uses the Gmail History API to fetch only changed
  messages since the last known `history_id`; falls back to a full sync if the
  history watermark has expired.
- **Scheduler** — APScheduler triggers incremental syncs in the background;
  full syncs run daily.

### Label workflow

All label changes go through a five-step pipeline:

1. `SafetyGuard` validates the request (raises `SafetyViolationError` if
   forbidden).
2. `GmailClient.modify_message_labels()` writes the change to Gmail
   immediately.
3. `MessageRepository.update_labels()` updates the local SQLite cache.
4. `LabelRepository.sync_message_labels()` rebuilds the junction table.
5. `LabelRepository.log_label_operation()` appends an audit record.

### Safety constraints

The application **cannot**:
- Delete or archive messages
- Send or draft emails
- Modify message bodies
- Add TRASH, SPAM, DRAFT, or SENT labels
- Remove INBOX, SENT, STARRED, IMPORTANT, or CATEGORY_* labels
- Exceed 500 messages per bulk operation

---

## Implementation Progress

| Step | Description                                | Status      |
|------|--------------------------------------------|-------------|
| 1    | Project skeleton & configuration           | ✅ Complete  |
| 2    | Domain models & safety guard               | ✅ Complete  |
| 3    | Database layer (SQLAlchemy + repositories) | ✅ Complete  |
| 4    | Mock Gmail client & OAuth stub             | ✅ Complete  |
| 5    | Sync engine & application services         | ✅ Complete  |
| 6    | Flask app & core routes (read-only)        | ✅ Complete  |
| 7    | Labelling UI & bulk operations             | ✅ Complete  |
| 8    | Progress graphs, snapshots & hardening     | ✅ Complete  |
| 9    | Documentation (UML, README, PRD, usage)    | ✅ Complete  |

---

## Troubleshooting

### Common Issues

**"Port 5000 already in use"**
```bash
# Find and stop the process
Get-NetTCPConnection -LocalPort 5000 | Stop-Process -Force
```

**"Gmail API disabled for this project"**
- Enable the Gmail API in Google Cloud Console
- Create OAuth 2.0 credentials (Desktop/Personal type)
- See `docs/setup_production.md` for full instructions

**"No messages found after sync"**
- Check that you're in demo mode: `echo $env:GMAIL_ZERO_ENV` should be `demo`
- Production mode: verify OAuth credentials in `data/credentials/`
- Run a manual sync from Settings page

**"Database locked"**
- Ensure only one instance of the app is running
- If crashed, delete `data/.lock` (if it exists)

---

## Contributing

Contributions follow these standards:

- All tests must pass: `pytest`
- Type checking: `mypy .`
- Formatting: `ruff format .`
- Linting: `ruff check .`
- PRD must be updated for new features

---

## License

MIT
