"""
Gmail infrastructure sub-package for gmail_zero_app.

Exports:
    AbstractGmailClient  — Protocol for all Gmail client implementations
    GmailClient          — Production client with whitelist enforcement
    MockGmailClient      — Deterministic fake client for demo and tests
    GmailMapper          — Translates API dicts to domain entities
    OAuthHandler         — OAuth2 token lifecycle management
"""

from infrastructure.gmail.client import AbstractGmailClient, GmailClient
from infrastructure.gmail.mapper import GmailMapper
from infrastructure.gmail.mock_client import MockGmailClient
from infrastructure.gmail.oauth import OAuthHandler

__all__ = [
    "AbstractGmailClient",
    "GmailClient",
    "GmailMapper",
    "MockGmailClient",
    "OAuthHandler",
]
