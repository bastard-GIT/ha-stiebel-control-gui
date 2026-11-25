"""Tests for the Elster table generated from the pinned upstream sources (gui/upstream_table.py).

Covers: the pin (upstream/PIN.json), generation for all models, the WPL-A branch, the duplicate rule,
the preprocessor subset, loading and the overlay of the private table, the table choice (table_config,
default_reference_path), the dialog and menu entry, and the CLI tools/gen_upstream_table.py.

The tests do not need the private repo ../elster_reference: ELSTER_PRIVATE_JSON points to a missing file
unless a test sets it. Everything is written to tmp_path. Qt runs offscreen.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GUI_DIR = ROOT / "gui"
TOOL = ROOT / "tools" / "gen_upstream_table.py"
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import elster  # noqa: E402
import elster_gui  # noqa: E402
import i18n  # noqa: E402
import table_config  # noqa: E402
import upstream_table as ut  # noqa: E402
from dialog_upstream_table import UpstreamTableDialog  # noqa: E402
from elster import ElsterDb  # noqa: E402
from upstream_table import UpstreamTableError  # noqa: E402

VALID_JSON = ROOT / "tests" / "data" / "ElsterTable.test.json"

# Signal counts of the generated tables at upstream commit 4ec5b3c (PIN.json). After a pin update,
# these numbers change: adjust them on purpose and say so in the commit message.
EXPECTED_SIGNALS = {
    "wpl9": 3613,
    "wpl10ac": 3733,
    "wpl13e": 3613,
    "wpl17": 3733,
    "wpl18e": 3613,
    "wpl23": 3733,
    "wpc7": 3733,
    "wpf10": 3613,
    "ttl33": 3613,
    "wpla7": 3733,
}
EXPECTED_MODELS = set(EXPECTED_SIGNALS)
EXPECTED_DUPLICATES = 7


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """User config, private table and the standard table are per test; caches are reset around it."""
    monkeypatch.setenv("STIEBEL_GUI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ELSTER_PRIVATE_JSON", str(tmp_path / "no-private.json"))
    monkeypatch.setattr(elster, "DEFAULT_JSON", elster.DEFAULT_JSON)
    elster._lookup_tables.cache_clear()
    elster._type_table.cache_clear()
    yield
    monkeypatch.undo()  # restores DEFAULT_JSON and the environment before the caches are cleared
    elster._lookup_tables.cache_clear()
    elster._type_table.cache_clear()


@pytest.fixture(scope="module")
def tables_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("upstream_tables")


@pytest.fixture(scope="module")
def wpla7_table(tables_dir):
    return ut.write_table("wpla7", tables_dir / "ElsterTable.wpla7.json")


@pytest.fixture(scope="module")
def wpl9_table(tables_dir):
    return ut.write_table("wpl9", tables_dir / "ElsterTable.wpl9.json")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def english():
    previous = i18n.current_language()
    i18n.set_language("en")
    yield
    i18n.set_language(previous)


def _signals_by_index(table: dict) -> dict:
    return {s["index"]: s for s in table["signals"]}


# ---------------------------------------------------------------- 1. pin

def test_pin_verifies():
    ut.verify_pin()


def test_pin_lists_the_three_required_files():
    pin = json.loads(ut.PIN_PATH.read_text(encoding="utf-8"))
    assert set(pin["files"]) == {"elster/ElsterTable.h", "model_profile.h", "can_members.h"}
    assert set(pin["files"]) == set(ut.REQUIRED_FILES)


def test_pin_sha256_matches_files():
    pin = json.loads(ut.PIN_PATH.read_text(encoding="utf-8"))
    for rel, expected in pin["files"].items():
        actual = hashlib.sha256((ut.SOURCE_DIR / rel).read_bytes()).hexdigest()
        assert actual == expected, rel


def test_pin_commit_is_40_hex():
    commit = ut.pin_info()["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit)


@pytest.fixture
def pin_copy(tmp_path, monkeypatch):
    """Copy of the three pinned files in tmp_path; ut.SOURCE_DIR points there."""
    dest = tmp_path / "pinned-src"
    for rel in ut.REQUIRED_FILES:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ut.SOURCE_DIR / rel, target)
    monkeypatch.setattr(ut, "SOURCE_DIR", dest)
    return dest


def test_pin_copy_verifies(pin_copy):
    ut.verify_pin()


def test_changed_file_fails_verification_and_names_it(pin_copy):
    target = pin_copy / "model_profile.h"
    target.write_bytes(target.read_bytes() + b"\n// changed\n")
    with pytest.raises(UpstreamTableError, match=r"model_profile\.h"):
        ut.verify_pin()


def test_changed_file_blocks_generation(pin_copy):
    target = pin_copy / "elster" / "ElsterTable.h"
    target.write_bytes(target.read_bytes() + b"\n// changed\n")
    with pytest.raises(UpstreamTableError, match=r"ElsterTable\.h"):
        ut.generate("wpla7")


# ---------------------------------------------------------------- 2. models

def test_available_models_are_the_ten_expected():
    models = ut.available_models()
    assert len(models) == 10
    assert set(models) == EXPECTED_MODELS
    assert "test_model" not in models and "unknown" not in models


@pytest.mark.parametrize("model", sorted(EXPECTED_MODELS))
def test_generate_model(model):
    pin = ut.pin_info()
    data = ut.generate(model)

    assert data["format_version"] == 2
    assert data["source"]["repo"] == pin["repo"]
    assert data["source"]["commit"] == pin["commit"]
    assert data["source"]["model"] == model

    signals = data["signals"]
    assert len(signals) == EXPECTED_SIGNALS[model]

    members = data["can_members"]
    assert len(members) == 21
    for member in members:
        assert len(member["read_id"]) == 2
        assert len(member["write_id"]) == 2
    assert data["frame_format"]["bus"]["own_can_id"] == "0x680"

    sentinels = [s for s in signals if s.get("sentinel")]
    assert len(sentinels) == 1
    assert signals[0].get("sentinel") is True
    assert signals[0]["index"] == "0x0000"

    indices = [s["index"] for s in signals]
    assert len(set(indices)) == len(indices)

    assert len(data["source"]["duplicates_dropped"]) == EXPECTED_DUPLICATES


# ---------------------------------------------------------------- 3. WPL-A branch

def test_wpla7_branch_signals(wpla7_table):
    sigs = _signals_by_index(json.loads(wpla7_table.read_text(encoding="utf-8")))
    # ElsterTable.h Z.811: #if defined(HA_CONTROLLER_WPLA) selects WP_WASSERVOLUMENSTROM (else WE_KONFIGURATION)
    assert sigs["0x02e2"]["name"] == "WP_WASSERVOLUMENSTROM"
    assert sigs["0x02e2"]["type"] == "et_dec_val"
    # ElsterTable.h Z.2597: FROSTSCHUTZTEMPERATUR (else EINGANG14)
    assert sigs["0x0a36"]["name"] == "FROSTSCHUTZTEMPERATUR"
    assert sigs["0x0a36"]["type"] == "et_dec_val"


@pytest.mark.parametrize("model", ["wpl13e", "wpl9"])
def test_non_wpla_branch_signals(tables_dir, model):
    path = ut.write_table(model, tables_dir / f"ElsterTable.{model}.json")
    sigs = _signals_by_index(json.loads(path.read_text(encoding="utf-8")))
    # Without HA_CONTROLLER_WPLA the else branches apply.
    assert sigs["0x02e2"]["name"] == "WE_KONFIGURATION"
    assert sigs["0x02e2"]["type"] == "et_default"
    assert sigs["0x0a36"]["name"] == "EINGANG14"
    assert sigs["0x0a36"]["type"] == "et_default"


def test_operating_mode_names_per_model(wpla7_table, tables_dir):
    wpla7 = json.loads(wpla7_table.read_text(encoding="utf-8"))
    names = {b["name"] for b in wpla7["betriebsart_list"]}
    assert "Programmierbetrieb" in names

    path = ut.write_table("wpl13e", tables_dir / "ElsterTable.wpl13e.json")
    wpl13e = json.loads(path.read_text(encoding="utf-8"))
    assert "Automatik" in {b["name"] for b in wpl13e["betriebsart_list"]}


# ---------------------------------------------------------------- 4. duplicate rule

def test_duplicates_first_row_wins_for_wpla7(wpla7_table):
    data = json.loads(wpla7_table.read_text(encoding="utf-8"))
    dups = {d["index"]: d for d in data["source"]["duplicates_dropped"]}
    assert len(dups) == EXPECTED_DUPLICATES

    sigs = _signals_by_index(data)
    # ElsterTable.h Z.1935 (TEST_OBJEKT_249) before Z.1936 (KALIBRIERWERT_1_1), both 0x073b.
    assert sigs["0x073b"]["name"] == "TEST_OBJEKT_249"
    assert dups["0x073b"]["kept"] == "TEST_OBJEKT_249"
    assert dups["0x073b"]["dropped"] == "KALIBRIERWERT_1_1"
    # ElsterTable.h Z.2888 (FEHLERNUMMER) before Z.2938 (REPEAT_MESSAGE_ALL_24H), both 0x1389.
    assert sigs["0x1389"]["name"] == "FEHLERNUMMER"
    assert dups["0x1389"]["kept"] == "FEHLERNUMMER"
    assert dups["0x1389"]["dropped"] == "REPEAT_MESSAGE_ALL_24H"


def test_first_text_match_in_header_is_the_kept_row():
    text = (ut.SOURCE_DIR / ut.TABLE_FILE).read_text(encoding="utf-8")
    match = re.search(r'\{\s*"([^"]+)"\s*,\s*0x073b\s*,', text)
    assert match is not None
    assert match.group(1) == "TEST_OBJEKT_249"


# ---------------------------------------------------------------- 5. preprocessor

def _active(lines, defines=(), values=None):
    return [text for _, text in ut.preprocess_lines(list(lines), set(defines), dict(values or {}))]


@pytest.mark.parametrize("expr, defines, values, expected", [
    ("defined(X)", {"X"}, {}, True),
    ("defined X", {"X"}, {}, True),
    ("defined(X)", set(), {}, False),
    ("!defined(X)", set(), {}, True),
    ("!defined X && defined(Y)", {"Y"}, {}, True),
    ("defined(X) || defined(Y)", {"Y"}, {}, True),
    ("defined(X) && defined(Y)", {"X"}, {}, False),
    ("(defined(X) || defined(Y)) && !defined(Z)", {"Y"}, {}, True),
    ("(defined(X) || defined(Y)) && !defined(Z)", {"Y", "Z"}, {}, False),
    ("defined(V)", set(), {"V": 0}, True),
    ("A == 3", set(), {"A": 3}, True),
    ("A != 3", set(), {"A": 3}, False),
    ("A == 0x10", set(), {"A": 16}, True),
    ("A == 16 || B != 1", set(), {"A": 3, "B": 1}, False),
    ("!A == 0", set(), {"A": 5}, True),
    ("defined(A) && A == 3", set(), {"A": 3}, True),
    ("1 == 1 && 2 != 3", set(), {}, True),
    ("0 || 1 && 0", set(), {}, False),  # && binds tighter than ||
])
def test_evaluate_condition(expr, defines, values, expected):
    assert ut.evaluate_condition(expr, defines, values) is expected


@pytest.mark.parametrize("expr", [
    "FOO > 3",        # unsupported operator
    "FOO",            # unknown identifier
    "1 +",            # unsupported operator at the end
    "",               # empty
    "defined(X",      # missing ')'
    "defined()",      # defined without name
    "(1",             # missing ')'
    "A == ",          # missing operand
])
def test_evaluate_condition_rejects(expr):
    with pytest.raises(UpstreamTableError):
        ut.evaluate_condition(expr, set(), {})


def test_macro_without_value_in_expression_is_rejected():
    with pytest.raises(UpstreamTableError, match="defined"):
        ut.evaluate_condition("HA_CONTROLLER_WPLA", {"HA_CONTROLLER_WPLA"}, {})


def test_ifdef_else():
    lines = ["#ifdef X", "a", "#else", "b", "#endif"]
    assert _active(lines, {"X"}) == ["a"]
    assert _active(lines) == ["b"]


def test_ifndef():
    lines = ["#ifndef X", "a", "#endif"]
    assert _active(lines, {"X"}) == []
    assert _active(lines) == ["a"]


def test_if_elif_else_chain():
    lines = ["#if A == 1", "one", "#elif A == 2", "two", "#elif A == 3", "three", "#else", "other", "#endif"]
    assert _active(lines, values={"A": 1}) == ["one"]
    assert _active(lines, values={"A": 2}) == ["two"]
    assert _active(lines, values={"A": 3}) == ["three"]
    assert _active(lines, values={"A": 9}) == ["other"]


def test_elif_after_taken_branch_is_not_evaluated():
    # FOO is unknown: evaluating the #elif would raise. It is skipped because #if was taken.
    lines = ["#if A == 1", "one", "#elif FOO > 3", "bad", "#endif"]
    assert _active(lines, values={"A": 1}) == ["one"]


def test_nested_blocks():
    lines = ["#if defined(X)", "#if defined(Y)", "xy", "#else", "x", "#endif", "#else", "none", "#endif"]
    assert _active(lines, {"X", "Y"}) == ["xy"]
    assert _active(lines, {"X"}) == ["x"]
    assert _active(lines) == ["none"]


def test_skipped_block_is_not_evaluated():
    # Nothing in the skipped block is evaluated, so the unknown expressions and directives are harmless.
    lines = ["#if 0", "#if FOO > 3", "#foo", "#elif FOO", "#else", "#endif", "#endif", "ok"]
    assert _active(lines) == ["ok"]


def test_line_continuation_in_if():
    lines = ["#if defined(X) && \\", "defined(Y)", "line", "#endif"]
    assert _active(lines, {"X", "Y"}) == ["line"]
    assert _active(lines, {"X"}) == []


def test_line_numbers_are_kept():
    yielded = list(ut.preprocess_lines(["#ifdef X", "a", "#endif", "b"], {"X"}))
    assert yielded == [(2, "a"), (4, "b")]


@pytest.mark.parametrize("lines, message", [
    (["#if FOO > 3", "a", "#endif"], "unsupported token"),
    (["#if 1 +", "a", "#endif"], "unsupported token"),
    (["#foo"], "unsupported directive"),
    (["a", "#if defined(X", "#endif"], "missing"),
    (["#if 1", "a"], "without #endif"),
    (["#endif"], "without #if"),
    (["#else"], "without #if"),
    (["#elif 1"], "without #if"),
    (["#if 1", "#else", "#else", "#endif"], "second #else"),
    (["#if 1", "#else", "#elif 1", "#endif"], "after #else"),
])
def test_preprocess_errors(lines, message):
    with pytest.raises(UpstreamTableError, match=re.escape(message)):
        _active(lines)


def test_error_names_source_and_line():
    with pytest.raises(UpstreamTableError, match=r"<input>:2:"):
        _active(["a", "#if FOO > 3", "#endif"])


def test_defines_for_wpla7():
    defines = ut.defines_for("wpla7")
    assert {"HA_CONTROLLER_WPLA", "HA_CONTROLLER_WPM3", "HA_ELSTER_WPL_FAMILY"} <= defines
    assert "HA_CONTROLLER_WPMW2" not in defines


def test_defines_for_wpl13e_and_ttl33():
    defines = ut.defines_for("wpl13e")
    assert "HA_CONTROLLER_WPMW2" in defines
    assert "HA_CONTROLLER_WPM3" not in defines
    assert "HA_CONTROLLER_WPLA" not in defines
    assert "HA_CONTROLLER_WPMW2" in ut.defines_for("ttl33")


def test_defines_for_unknown_model_raises():
    with pytest.raises(UpstreamTableError, match="unknown model"):
        ut.defines_for("foo")


# ---------------------------------------------------------------- 6. loading

def test_db_loads_generated_table(wpla7_table):
    db = ElsterDb(str(wpla7_table), overlay=False)
    assert db.table_loaded is True
    assert db.table_error is None
    assert db.signal_name(0x02E2) == "WP_WASSERVOLUMENSTROM"
    assert db.decode(0x02E2, 0x0064) == "10.0"


def test_db_uses_upstream_member_names(wpla7_table):
    db = ElsterDb(str(wpla7_table), overlay=False)
    assert db.member_name(0x201) == "KESSEL_CANB"
    assert db.own_can_id == 0x680


def test_db_decodes_hours_and_error_status_with_generated_lists(wpla7_table):
    db = ElsterDb(str(wpla7_table), overlay=False)
    assert db.decode(0x0041, 1234, "et_hours") == "1234"
    # et_err_status: fault number in the high byte, looked up in the generated ErrorList (0x0002)
    assert db.decode(0x0041, 0x0200, "et_err_status") == "Schuetz klebt"
    # ErrorList has an entry 0x0000 ("Keine Stoerung"), so the high byte 0 is that name, not "0"
    assert db.decode(0x0041, 0x0000, "et_err_status") == "Keine Stoerung"


def test_db_reload_switches_table_in_place(wpla7_table, wpl9_table):
    db = ElsterDb(str(wpla7_table), overlay=False)
    count_a = len(db.signals)
    db.reload(str(wpl9_table), overlay=False)
    assert db.table_path == wpl9_table
    assert len(db.signals) != count_a
    assert len(db.signals) == EXPECTED_SIGNALS["wpl9"]
    assert db.signal_name(0x02E2) == "WE_KONFIGURATION"


def test_activate_table_sets_default_and_saves_path(wpla7_table):
    elster.activate_table(wpla7_table)
    assert elster.DEFAULT_JSON == wpla7_table
    assert table_config.saved_table_path() == wpla7_table
    db = ElsterDb(overlay=False)
    assert db.table_path == wpla7_table
    assert db.table_loaded is True


def test_default_path_order_env_first(tmp_path, monkeypatch, wpla7_table, wpl9_table):
    monkeypatch.setenv("ELSTER_REFERENCE_JSON", str(wpla7_table))
    table_config.save_table_path(wpl9_table)
    assert elster.default_reference_path() == wpla7_table


def test_default_path_order_saved_before_private(tmp_path, monkeypatch, wpla7_table, wpl9_table):
    monkeypatch.delenv("ELSTER_REFERENCE_JSON", raising=False)
    table_config.save_table_path(wpl9_table)
    monkeypatch.setattr(elster, "_private_candidates",
                        lambda: (tmp_path / "private.json", tmp_path / "private2.json"))
    (tmp_path / "private.json").write_text("{}", encoding="utf-8")
    assert elster.default_reference_path() == wpl9_table


def test_default_path_order_private_candidates(tmp_path, monkeypatch):
    monkeypatch.delenv("ELSTER_REFERENCE_JSON", raising=False)
    first, second = tmp_path / "c1.json", tmp_path / "c2.json"
    monkeypatch.setattr(elster, "_private_candidates", lambda: (first, second))
    assert elster.default_reference_path() == first  # nothing exists: first candidate
    second.write_text("{}", encoding="utf-8")
    assert elster.default_reference_path() == second  # first existing candidate wins
    first.write_text("{}", encoding="utf-8")
    assert elster.default_reference_path() == first


def test_no_table_anywhere_starts_without_table(tmp_path, monkeypatch):
    monkeypatch.delenv("ELSTER_REFERENCE_JSON", raising=False)
    missing = tmp_path / "none1.json"
    monkeypatch.setattr(elster, "_private_candidates", lambda: (missing, tmp_path / "none2.json"))
    monkeypatch.setattr(elster, "DEFAULT_JSON", elster.default_reference_path())
    db = ElsterDb()
    assert db.table_path == missing
    assert db.table_loaded is False
    assert db.table_error == "file not found"
    assert db.signals == []


def test_private_reference_path_env(tmp_path, monkeypatch):
    private = tmp_path / "private.json"
    private.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ELSTER_PRIVATE_JSON", str(private))
    assert elster.private_reference_path() == private
    monkeypatch.setenv("ELSTER_PRIVATE_JSON", str(tmp_path / "gone.json"))
    assert elster.private_reference_path() is None


# ---------------------------------------------------------------- 7. overlay

def _private_table(path: Path) -> Path:
    """Synthetic private table (format v2): one upstream index with details, one private-only index."""
    data = {
        "format_version": 2,
        "signals": [
            {"name": "INDEX_NOT_FOUND", "index": "0x0000", "index_int": 0, "type": "et_default",
             "owner": None, "owner_name": None, "poll": None, "sentinel": True},
            {"name": "PRIVATER_NAME", "index": "0x02e2", "index_int": 738, "type": "et_default",
             "owner": "0x480", "owner_name": "WP", "poll": "pl_60s", "comment": "Vergleich",
             "bits": [{"bit": 0, "name": "BIT0", "confirmed": True}]},
            {"name": "PRIV_ONLY", "index": "0x4f13", "index_int": 20243, "type": "et_default",
             "owner": "0x480", "owner_name": "WP", "poll": None, "comment": None, "bits": None},
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_overlay_adds_details_and_keeps_upstream_names(tmp_path, wpla7_table):
    private = _private_table(tmp_path / "private.json")
    before_upstream = wpla7_table.read_bytes()
    before_private = private.read_bytes()

    db = ElsterDb(str(wpla7_table), overlay=True, private_json=str(private))

    assert db.overlay_applied is True
    assert db.overlay_path == private
    assert db.overlay_added == 1
    assert db.overlay_enriched >= 1

    sig = db.signal_by_index(0x02E2)
    assert sig["name"] == "WP_WASSERVOLUMENSTROM"
    assert sig["type"] == "et_dec_val"
    assert sig["owner"] == "0x480"
    assert sig["owner_name"] == "WP"
    assert sig["poll"] == "pl_60s"
    assert sig["comment"] == "Vergleich"
    assert db.bits_for(0x02E2)[0]["name"] == "BIT0"

    added = db.signal_by_index(0x4F13)
    assert added is not None
    assert db.signal_name(0x4F13) == "PRIV_ONLY"

    assert wpla7_table.read_bytes() == before_upstream
    assert private.read_bytes() == before_private


def test_overlay_off_leaves_signals_alone(tmp_path, wpla7_table):
    private = _private_table(tmp_path / "private.json")
    db = ElsterDb(str(wpla7_table), overlay=False, private_json=str(private))
    assert db.overlay_applied is False
    assert db.signal_by_index(0x02E2)["owner"] is None
    assert db.signal_by_index(0x4F13) is None


def test_overlay_without_private_file(tmp_path, wpla7_table):
    db = ElsterDb(str(wpla7_table), overlay=True, private_json=str(tmp_path / "missing.json"))
    assert db.overlay_applied is False


def test_overlay_needs_private_file_from_env_when_not_given(tmp_path, wpla7_table):
    # ELSTER_PRIVATE_JSON points to a missing file (autouse fixture): no overlay.
    db = ElsterDb(str(wpla7_table), overlay=True)
    assert db.overlay_applied is False


def test_overlay_skipped_for_table_without_source_repo(tmp_path):
    private = _private_table(tmp_path / "private.json")
    db = ElsterDb(str(VALID_JSON), overlay=True, private_json=str(private))
    assert db.table_loaded is True
    assert db.overlay_applied is False


def test_overlay_default_follows_table_config(tmp_path, wpla7_table):
    private = _private_table(tmp_path / "private.json")
    assert table_config.overlay_enabled() is True  # default
    table_config.set_overlay_enabled(False)
    db = ElsterDb(str(wpla7_table), private_json=str(private))
    assert db.overlay_applied is False


def test_overlay_config_roundtrip():
    assert table_config.overlay_enabled() is True
    assert table_config.set_overlay_enabled(False) is True
    assert table_config.overlay_enabled() is False
    assert table_config.set_overlay_enabled(True) is True
    assert table_config.overlay_enabled() is True


# ---------------------------------------------------------------- 8. dialog and menu

def _combo_select(combo, text):
    index = combo.findText(text)
    assert index >= 0, text
    combo.setCurrentIndex(index)


def test_dialog_suggests_path_per_model(qapp):
    dlg = UpstreamTableDialog()
    try:
        _combo_select(dlg._model_combo, "wpla7")
        expected = table_config.config_dir() / "ElsterTable.wpla7.json"
        assert dlg._path.text() == str(expected)
        _combo_select(dlg._model_combo, "wpl9")
        assert dlg._path.text() == str(table_config.config_dir() / "ElsterTable.wpl9.json")
    finally:
        dlg.deleteLater()


def test_dialog_generates_file_and_accepts(qapp, tmp_path, monkeypatch):
    dlg = UpstreamTableDialog()
    infos = []
    monkeypatch.setattr(dlg, "_info", lambda text: infos.append(text))
    try:
        _combo_select(dlg._model_combo, "wpla7")
        target = tmp_path / "ElsterTable.wpla7.json"
        dlg._path.setText(str(target))
        dlg._btn_generate.click()

        assert target.is_file()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["source"]["model"] == "wpla7"
        assert len(data["signals"]) == EXPECTED_SIGNALS["wpla7"]
        assert dlg.result() == QDialog.DialogCode.Accepted
        assert dlg.generated_path == target
        assert dlg.model == "wpla7"
        assert elster.DEFAULT_JSON == target
        assert table_config.saved_table_path() == target
        assert len(infos) == 1 and str(EXPECTED_SIGNALS["wpla7"]) in infos[0]
    finally:
        dlg.deleteLater()


def test_dialog_generation_error_warns_and_stays_open(qapp, tmp_path, monkeypatch):
    dlg = UpstreamTableDialog()
    warnings = []
    monkeypatch.setattr(dlg, "_warn", lambda text: warnings.append(text))

    def boom(model, path, indent=1):
        raise UpstreamTableError("boom: pinned file differs")

    monkeypatch.setattr(ut, "write_table", boom)
    target = tmp_path / "ElsterTable.wpla7.json"
    before = elster.DEFAULT_JSON
    try:
        _combo_select(dlg._model_combo, "wpla7")
        dlg._path.setText(str(target))
        dlg._btn_generate.click()

        assert len(warnings) == 1
        assert "boom: pinned file differs" in warnings[0]
        assert dlg.result() != QDialog.DialogCode.Accepted
        assert dlg.generated_path is None
        assert not target.exists()
        assert elster.DEFAULT_JSON == before
    finally:
        dlg.deleteLater()


class _MenuBridge(QObject):
    connected = Signal(bool)
    rx_frames = Signal(list)
    read_result = Signal(dict)
    scan_progress = Signal(dict)
    scan_result = Signal(dict)
    status = Signal(dict)
    availability = Signal(str)
    availability_source = Signal(str)
    firmware_kind = Signal(str)
    can_debug_state = Signal(bool)

    def send_read(self, member, index, fmt=None, switch=None):
        pass

    def send_scan(self, member, from_idx, to_idx, delay_ms, fmt=None, switch=None):
        pass

    def send_stop(self):
        pass

    def disconnect_from(self):
        pass


def _find_action(window, text):
    for top in window.menuBar().actions():
        menu = top.menu()
        if menu is None:
            continue
        for act in menu.actions():
            if act.text() == text:
                return act
    return None


def test_main_window_menu_generates_table(qapp, english, wpla7_table, monkeypatch):
    results = []

    class _FakeDialog:
        def __init__(self, parent=None):
            results.append("created")

        def exec(self):
            return QDialog.DialogCode.Accepted

        def deleteLater(self):
            pass

    monkeypatch.setattr(elster_gui, "UpstreamTableDialog", _FakeDialog)
    win = elster_gui.MainWindow(ElsterDb(str(wpla7_table), overlay=False), _MenuBridge())
    try:
        act = _find_action(win, i18n._("Generate Elster table from upstream…"))
        assert act is not None
        act.trigger()
        assert results == ["created"]
        assert "Restart the GUI" in win.statusBar().currentMessage()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_main_window_menu_cancel_keeps_status(qapp, english, wpla7_table, monkeypatch):
    class _Rejected:
        def __init__(self, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

        def deleteLater(self):
            pass

    monkeypatch.setattr(elster_gui, "UpstreamTableDialog", _Rejected)
    win = elster_gui.MainWindow(ElsterDb(str(wpla7_table), overlay=False), _MenuBridge())
    try:
        win.statusBar().clearMessage()
        _find_action(win, i18n._("Generate Elster table from upstream…")).trigger()
        assert "Restart the GUI" not in win.statusBar().currentMessage()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------- 9. CLI

def _run_tool(*args):
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), *args],
        capture_output=True, text=True, timeout=300,
    )


def test_cli_list_names_ten_models():
    result = _run_tool("--list")
    assert result.returncode == 0, result.stderr
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(names) == 10
    assert set(names) == EXPECTED_MODELS


def test_cli_pin_verifies():
    result = _run_tool("--pin")
    assert result.returncode == 0, result.stderr
    assert "verify: ok" in result.stdout


def test_cli_model_writes_file(tmp_path):
    out = tmp_path / "x.json"
    result = _run_tool("--model", "wpla7", "--out", str(out))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["source"]["model"] == "wpla7"


def test_cli_unknown_model_fails(tmp_path):
    result = _run_tool("--model", "foo", "--out", str(tmp_path / "x.json"))
    assert result.returncode == 2
    assert result.stderr.strip()
    assert "unknown model" in result.stderr
    assert not (tmp_path / "x.json").exists()


def test_cli_all_writes_ten_files(tmp_path):
    out_dir = tmp_path / "all"
    out_dir.mkdir()
    result = _run_tool("--all", "--out-dir", str(out_dir))
    assert result.returncode == 0, result.stderr
    files = sorted(out_dir.glob("ElsterTable.*.json"))
    assert len(files) == 10
