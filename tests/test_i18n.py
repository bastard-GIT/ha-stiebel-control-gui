"""Tests for gui/i18n.py and the catalog check (tools/i18n_check.py)."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = ROOT / "gui"
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))

import i18n  # noqa: E402

_spec = importlib.util.spec_from_file_location("i18n_check", ROOT / "tools" / "i18n_check.py")
i18n_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(i18n_check)


def teardown_function(_fn):
    i18n.set_language("en")


def test_default_is_english_passthrough():
    assert i18n.current_language() == "en"
    assert i18n._("anything") == "anything"
    assert i18n.N_("anything") == "anything"


def test_set_language_with_catalog(tmp_path, monkeypatch):
    (tmp_path / "xx.json").write_text(json.dumps({"Connect...": "Verbinden..."}), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    assert "xx" in i18n.available_languages()
    assert i18n.set_language("xx") is True
    assert i18n._("Connect...") == "Verbinden..."
    assert i18n._("unknown text") == "unknown text"   # missing key: English


def test_unknown_language_falls_back_to_english():
    assert i18n.set_language("zz") is False
    assert i18n.current_language() == "en"


def test_broken_catalog_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    assert i18n.set_language("bad") is False


def test_env_overrides_settings(monkeypatch):
    monkeypatch.setenv(i18n.ENV_LANGUAGE, "de")
    assert i18n.saved_language() == "de"


def test_no_dynamic_markers_in_gui():
    keys = i18n_check.collect_keys()
    assert "<dynamic>" not in keys, f"_() with a non-literal argument: {keys.get('<dynamic>')}"


def test_catalogs_are_complete_and_consistent():
    keys = i18n_check.collect_keys()
    problems = []
    for path in sorted((GUI_DIR / "locales").glob("*.json")):
        problems += i18n_check.check(path.stem, keys)
    assert not problems, "\n".join(problems[:20])
