"""Tests for the start without a usable Elster table (missing, broken or incomplete JSON).

The GUI must start and show raw values; the status bar names the file and the reason.
Built-in defaults (members, types) must match the checked-in test table. All files live in tmp_path.
Qt runs offscreen; the GUI tests use the English catalog.
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GUI_DIR = Path(__file__).resolve().parent.parent / "gui"
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))

import pytest  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

import elster  # noqa: E402
import i18n  # noqa: E402
import tab_live  # noqa: E402
from elster import ElsterDb, decode_value, encode_value, is_writable  # noqa: E402
from elster_defaults import DEFAULT_CAN_MEMBERS, DEFAULT_TYPES  # noqa: E402

VALID_JSON = Path(__file__).resolve().parent / "data" / "ElsterTable.test.json"

# Frame of a response from 0x180, index 0x0112, switch 0xFA, value 0x0200 (see test_elster.py)
FRAME = {"t": 1000, "id": 0x180, "data": bytes.fromhex("d2 00 fa 01 12 02 00"), "tx": False}

BROKEN_TEXT = '{ kaputt'


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _missing(tmp_path):
    return tmp_path / "ElsterTable.missing.json"


def _broken(tmp_path):
    return _write(tmp_path, "ElsterTable.broken.json", BROKEN_TEXT)


def _empty_object(tmp_path):
    return _write(tmp_path, "ElsterTable.empty.json", "{}")


def _json_list(tmp_path):
    return _write(tmp_path, "ElsterTable.list.json", json.dumps([1, 2, 3]))


BAD_TABLES = {
    "missing": _missing,
    "broken": _broken,
    "empty_object": _empty_object,
    "json_list": _json_list,
}


def _norm_hex_list(values):
    return [int(v, 16) for v in values]


# ---------------------------------------------------------------- ElsterDb without table

@pytest.mark.parametrize("kind", list(BAD_TABLES))
def test_db_without_table_uses_empty_table_and_defaults(tmp_path, kind):
    db = ElsterDb(str(BAD_TABLES[kind](tmp_path)))
    assert db.table_loaded is False
    assert db.table_error
    assert db.signals == []
    assert db.error_list == []
    assert db.betriebsart_list == []
    assert len(db.members) == 21
    assert db.own_can_id == 0x680
    assert db.member_name(0x680) == "ESPCLIENT"
    assert db.signal_by_index(0x0041) is None


@pytest.mark.parametrize("kind", list(BAD_TABLES))
def test_db_without_table_decodes_without_exception(tmp_path, kind):
    db = ElsterDb(str(BAD_TABLES[kind](tmp_path)))
    text = db.decode(0x0041, 0x0041)
    assert isinstance(text, str)
    assert db.signal_name(0x0041) == "UNK_0x0041"


def test_db_with_valid_table_is_loaded(tmp_path):
    db = ElsterDb(str(VALID_JSON))
    assert db.table_loaded is True
    assert db.table_error is None


def test_db_error_text_names_reason(tmp_path):
    assert "not found" in ElsterDb(str(_missing(tmp_path))).table_error
    assert "invalid JSON" in ElsterDb(str(_broken(tmp_path))).table_error
    assert "signals" in ElsterDb(str(_empty_object(tmp_path))).table_error
    assert "not a JSON object" in ElsterDb(str(_json_list(tmp_path))).table_error


# ---------------------------------------------------------------- built-in defaults vs test table

def test_default_members_match_test_table():
    table = json.loads(VALID_JSON.read_text(encoding="utf-8"))
    expected = {m["name"]: m for m in table["can_members"]}
    actual = {m["name"]: m for m in DEFAULT_CAN_MEMBERS}
    assert set(actual) == set(expected)
    assert len(DEFAULT_CAN_MEMBERS) == len(table["can_members"])
    for name, exp in expected.items():
        act = actual[name]
        assert int(act["can_id"], 16) == int(exp["can_id"], 16), name
        assert _norm_hex_list(act["read_id"]) == _norm_hex_list(exp["read_id"]), name
        assert _norm_hex_list(act["write_id"]) == _norm_hex_list(exp["write_id"]), name


def test_default_types_match_test_table():
    table = json.loads(VALID_JSON.read_text(encoding="utf-8"))
    expected = {t["name"]: bool(t["writable"]) for t in table["types"]}
    actual = {t["name"]: bool(t["writable"]) for t in DEFAULT_TYPES}
    assert actual == expected


# ---------------------------------------------------------------- lru_cache fallback

@pytest.fixture(params=["missing", "broken"])
def no_default_table(request, tmp_path, monkeypatch):
    """Point elster.DEFAULT_JSON at a missing or broken file; caches cleared before and after."""
    path = _missing(tmp_path) if request.param == "missing" else _broken(tmp_path)
    elster._lookup_tables.cache_clear()
    elster._type_table.cache_clear()
    monkeypatch.setattr(elster, "DEFAULT_JSON", path)
    yield path
    monkeypatch.undo()
    elster._lookup_tables.cache_clear()
    elster._type_table.cache_clear()


def test_decode_betriebsart_without_table_is_question_mark(no_default_table):
    assert decode_value("et_betriebsart", 0x0100) == "?"


def test_decode_err_nr_without_table_shows_raw(no_default_table):
    text = decode_value("et_err_nr", 0x1234)
    assert text == "ERR 4660"


def test_is_writable_without_table_matches_default_types(no_default_table):
    for t in DEFAULT_TYPES:
        assert is_writable(t["name"]) == t["writable"], t["name"]
    assert is_writable("et_dec_val") is True
    assert is_writable("et_double_val") is False


def test_encode_unknown_type_without_table_raises(no_default_table):
    with pytest.raises(ValueError):
        encode_value("no_such_type", "1")


# ---------------------------------------------------------------- frame without table

def test_frame_without_table_decodes_to_raw_columns(tmp_path):
    db = ElsterDb(str(_missing(tmp_path)))
    rec = tab_live._decode(db, 1, FRAME)
    assert rec.index == 0x0112
    assert rec.raw == 0x0200
    assert rec.name == "UNK_0x0112"
    assert rec.value == "512"  # et_default without table: signed decimal

    model = tab_live.FrameTableModel(db)
    model.load_rows([FRAME])
    raw_col = tab_live.COLUMNS.index("Raw value")
    assert model.data(model.index(0, raw_col)) == "0x0200"
    index_col = tab_live.COLUMNS.index("Index")
    assert model.data(model.index(0, index_col)) == "0x0112"


# ---------------------------------------------------------------- GUI

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


@pytest.fixture
def windows(qapp):
    """Collects windows and closes them after the test (no leftovers in later tests)."""
    created = []
    yield created.append
    for w in created:
        w.close()
        w.deleteLater()
    qapp.processEvents()


@pytest.fixture
def main_window_cls():
    import elster_gui
    return elster_gui.MainWindow


@pytest.fixture
def mqtt_bridge_cls():
    from mqtt_client import MqttBridge
    return MqttBridge


@pytest.mark.parametrize("kind", ["missing", "broken"])
def test_main_window_starts_without_table(tmp_path, english, windows, main_window_cls,
                                          mqtt_bridge_cls, kind):
    path = _missing(tmp_path) if kind == "missing" else _broken(tmp_path)
    db = ElsterDb(str(path))
    win = main_window_cls(db, mqtt_bridge_cls())
    windows(win)

    assert win._tabs.count() == 4
    assert win.live_tab is not None and win.query_tab is not None
    assert win.scanner_tab is not None and win.status_tab is not None

    msg = win.statusBar().currentMessage()
    assert "No Elster table found" in msg
    assert path.name in msg


def test_main_window_message_empty_with_valid_table(english, windows, main_window_cls,
                                                    mqtt_bridge_cls):
    db = ElsterDb(str(VALID_JSON))
    win = main_window_cls(db, mqtt_bridge_cls())
    windows(win)
    assert win.statusBar().currentMessage() == ""


def test_main_window_central_calls_without_table(tmp_path, english, windows, main_window_cls,
                                                 mqtt_bridge_cls):
    db = ElsterDb(str(_missing(tmp_path)))
    win = main_window_cls(db, mqtt_bridge_cls())
    windows(win)

    # Live: frame goes through the decoder and the model
    win.live_tab._on_rx_frames([FRAME])
    # Query: decoding without table (raw and by type), clearing
    assert win.query_tab._decode("et_default", 0x0200, 0x0112) == "512"
    win.query_tab._on_clear()
    # Scanner: member list is built from the built-in members
    win.scanner_tab._populate_members()
    # Status: availability text without bridge data
    win.status_tab._on_availability("online")


def test_auswerte_window_without_table_starts(tmp_path, english, windows):
    from auswerte_gui import AuswerteWindow

    for path in (_missing(tmp_path), _broken(tmp_path)):
        win = AuswerteWindow(str(path))
        windows(win)
        assert win.ref.load_error
        assert win.ref.types  # built-in types, not empty
        assert path.name in win.ref.path.name
        assert "Reference not loaded" in win.statusBar().currentMessage()


def test_auswerte_window_index_browser_and_analyse_without_table(tmp_path, english, windows):
    from auswerte_gui import AuswerteWindow

    win = AuswerteWindow(str(_missing(tmp_path)))
    windows(win)
    win.browser.reload()
    win.browser.refresh_status()
    win.analyse._refresh()
    win.analyse._refresh_tables()
