"""Minimal gettext-like translation (no Qt needed for lookups).

Source language is English: the English text is the key. A catalog is a JSON file
``gui/locales/<code>.json`` that maps English source text to the translation.

Usage in GUI code:
    from i18n import _, N_
    QLabel(_("Connect..."))                       # translated at call time
    _("Recipient: {member}").format(member=m)     # placeholders via str.format, never f-strings
    CHOICES = ((N_("current"), 0xFA), ...)        # N_ only marks the text, translate where shown: _(label)

Rules:
- Missing key or missing catalog: the English text is returned (never raises).
- The language is chosen once at startup (init_from_settings), before the tab modules are imported,
  because module-level constants are translated at import time. Changing it needs a restart.
- Tests and headless tools never read settings: the default is English.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT_LANGUAGE = "en"
ENV_LANGUAGE = "STIEBEL_GUI_LANG"
SETTINGS_ORG = "ha-stiebel-control-gui"
SETTINGS_APP = "gui"
SETTINGS_KEY = "language"

# Names shown in the language menu (each in its own language).
LANGUAGE_NAMES = {"en": "English", "de": "Deutsch"}

_catalog: dict[str, str] = {}
_language = DEFAULT_LANGUAGE


def available_languages() -> list[str]:
    """'en' plus every catalog file in locales/ (sorted)."""
    codes = {p.stem for p in LOCALES_DIR.glob("*.json")} if LOCALES_DIR.is_dir() else set()
    codes.discard(DEFAULT_LANGUAGE)
    return [DEFAULT_LANGUAGE] + sorted(codes)


def load_catalog(code: str) -> dict[str, str]:
    """Catalog of a language; empty for English or an unknown or broken file."""
    if code == DEFAULT_LANGUAGE:
        return {}
    try:
        with open(LOCALES_DIR / f"{code}.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}


def set_language(code: str) -> bool:
    """Activate a language. False (and English) if there is no catalog for it."""
    global _catalog, _language
    if code == DEFAULT_LANGUAGE:
        _catalog, _language = {}, DEFAULT_LANGUAGE
        return True
    catalog = load_catalog(code)
    if not catalog:
        _catalog, _language = {}, DEFAULT_LANGUAGE
        return False
    _catalog, _language = catalog, code
    return True


def current_language() -> str:
    return _language


def _(text: str) -> str:
    """Translate an English source text; unknown text is returned unchanged."""
    return _catalog.get(text, text)


def N_(text: str) -> str:
    """Mark a text for extraction without translating it now (translate with _() where it is shown)."""
    return text


def _settings():
    from PySide6.QtCore import QSettings
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def saved_language() -> str:
    """Language from $STIEBEL_GUI_LANG, else from the saved settings, else English."""
    env = os.environ.get(ENV_LANGUAGE, "").strip()
    if env:
        return env
    try:
        value = _settings().value(SETTINGS_KEY, DEFAULT_LANGUAGE)
    except Exception:  # noqa: BLE001 - settings are a convenience only
        return DEFAULT_LANGUAGE
    return str(value or DEFAULT_LANGUAGE)


def save_language(code: str) -> None:
    try:
        _settings().setValue(SETTINGS_KEY, code)
    except Exception:  # noqa: BLE001
        pass


def init_from_settings() -> str:
    """Call once at GUI startup, before importing the tab modules. Returns the active language."""
    set_language(saved_language())
    return current_language()
