"""
Configuration package for gmail_zero_app.

Exports the settings factory and label/scope constants used across all layers.
Import from here rather than from submodules to keep import paths stable.

    from config import get_settings
    from config.oauth_scopes import REQUIRED_SCOPES
    from config.label_config import load_label_config
"""

from config.settings import Environment, Settings, get_settings

__all__ = ["Environment", "Settings", "get_settings"]