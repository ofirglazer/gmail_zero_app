# TODO

## orders for Claude
* explain or fix each error in mypy check.
* Show class, data flow, sequence and ladder UML diagrams.
* what is the difference between message.py def is_large and GMAIL_ZERO_LARGE_MESSAGE_THRESHOLD_BYTES?
* Finish Step 4 implementation.


## for production 
* GMAIL_ZERO_DEBUG=false
* SECURITY: Generate a strong random key for production.
* settings.py: debug: bool = False


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
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

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

## Implementation Progress

| Step | Description                                | Status     |
|------|--------------------------------------------|------------|
| 1    | Project skeleton & configuration           | ✅ Complete |
| 2    | Domain models & safety guard               | ✅ Complete |
| 3    | Database layer (SQLAlchemy + repositories) | ✅ Complete |
| 4    | Mock Gmail client & OAuth stub             | ⏳ Pending  |
| 5    | Sync engine & application services         | ⏳ Pending  |
| 6    | Flask app & core routes (read-only)        | ⏳ Pending  |
| 7    | Labelling UI & bulk operations             | ⏳ Pending  |
| 8    | Progress graphs, snapshots & hardening     | ⏳ Pending  |
