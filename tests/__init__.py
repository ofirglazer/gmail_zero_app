"""
Test suite for gmail_zero_app.

Marker organisation:
    @pytest.mark.safety       Critical safety tests — run first, block on failure
    @pytest.mark.unit         Pure unit tests, no I/O
    @pytest.mark.integration  Integration tests using in-memory SQLite + MockGmailClient

Run all tests:
    pytest

Run only safety tests:
    pytest -m safety

Run with coverage:
    pytest --cov --cov-report=term-missing
"""
