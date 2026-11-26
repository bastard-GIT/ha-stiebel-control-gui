"""Tests for the Query tab with several recipients (';' or ',' separated) and the 'all' sequence.

Fake bridge as in test_tab_query_prod.py (ProdFake, no MQTT); Qt runs offscreen.
Firmware 'prod' (raw/read): no lock for member/index/switch/query.

Run: .venv-paho2/Scripts/python.exe -I -m pytest -q -p no:cacheprovider tests/test_tab_query_multi.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

GUI_DIR = Path(__file__).resolve().parent.parent / "gui"
sys.path.insert(0, str(GUI_DIR))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import tab_query  # noqa: E402
from elster import ElsterDb  # noqa: E402

M1, M2 = 0x200, 0x480
INDEX = 0x0013  # WW_KOMFORT_TEMPERATUR
FA, F8, F9 = 0xFA, 0xF8, 0xF9
SEQ_ORDER = [FA, F8, F9]
RAW = 0x01F6


class ProdFake(QObject):
    """Fake-Bridge: zeichnet send_read/send_write samt Zusatzfeldern auf."""
    connected = Signal(bool)
    read_result = Signal(dict)
    write_result = Signal(dict)
    firmware_kind = Signal(str)

    def __init__(self):
        super().__init__()
        self.reads = []   # (member, index, fmt, switch, extra_kwargs)
        self.writes = []  # (member, index, value, fmt)

    def send_read(self, member, index, fmt=None, switch=None, **extra):
        self.reads.append((member, index, fmt, switch, extra))

    def send_write(self, member, index, value, fmt=None):
        self.writes.append((member, index, value, fmt))


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="session")
def db():
    return ElsterDb()


@pytest.fixture
def fake_elster(monkeypatch):
    """Simple fakes as in test_tab_query_write.py: value * 10 -> raw value."""
    monkeypatch.setattr(tab_query, "encode_value", lambda t, s: int(round(float(s) * 10)))
    monkeypatch.setattr(tab_query, "is_writable", lambda t: True)
    monkeypatch.setattr(tab_query, "build_write_request",
                        lambda m, i, v, f="auto": (m, bytes([i >> 8, i & 0xFF, v >> 8, v & 0xFF])))


def _tab(db, fake, fw="prod"):
    if fw is not None:
        fake.firmware_kind.emit(fw)
    tab = tab_query.QueryTab(db, fake)
    if fw is not None:
        fake.firmware_kind.emit(fw)
    return tab


def _set_read(tab, member="0x200;0x480", index="0x0013", switch=FA):
    tab._member_combo.setEditText(member)
    tab._index_edit.setText(index)
    tab._switch_combo.setCurrentIndex(tab._switch_combo.findData(switch))


def _answer(fake, member, switch, status="ok", raw=RAW):
    fake.read_result.emit({"member": member, "index": INDEX, "switch": switch, "raw": raw,
                           "status": status, "src": 0x680})


def _write_tab(db, fake):
    tab = _tab(db, fake, fw="prod")
    tab._mode_combo.setCurrentIndex(tab._mode_combo.findData(tab_query.MODE_WRITE))
    tab._index_edit.setText("0x0013")
    tab._value_edit.setText("52.0")
    tab._arm_check.setChecked(True)
    return tab


# ---------------------------------------------------------------- _parse_members

@pytest.mark.parametrize("text, expected", [
    ("0x200;0x480", [0x200, 0x480]),
    ("0x200,0x480", [0x200, 0x480]),
    ("0x200 ; 0x480 , 0x201", [0x200, 0x480, 0x201]),
    ("0x200;0x200;0x480", [0x200, 0x480]),
])
def test_parse_members_hex_lists(qapp, db, text, expected):
    tab = _tab(db, ProdFake())
    assert tab._parse_members(text) == (expected, "")


def test_parse_members_mixes_reference_name_and_hex(qapp, db):
    tab = _tab(db, ProdFake())
    name = db.members[0]["name"]  # KESSEL (0x180): SPEICHER not in the test table
    can_id = int(db.members[0]["can_id"], 0)
    assert tab._parse_members(f"{name};0x480") == ([can_id, M2], "")


def test_parse_members_error_entry_names_bad_text(qapp, db):
    tab = _tab(db, ProdFake())
    members, hint = tab._parse_members("0x200;FOO")
    assert members is None
    assert "FOO" in hint


@pytest.mark.parametrize("text", ["", "   ", ";", " ; , ;"])
def test_parse_members_empty_returns_none(qapp, db, text):
    tab = _tab(db, ProdFake())
    members, hint = tab._parse_members(text)
    assert members is None
    assert hint != ""


# ---------------------------------------------------------------- Single switch, 2 recipients

def test_single_switch_two_recipients_sends_first_only(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, switch=FA)
    tab._on_query()
    assert len(fake.reads) == 1
    assert fake.reads[0][0] == M1
    assert fake.reads[0][3] == FA
    assert tab._seq is not None


def test_single_switch_two_recipients_second_after_answer_and_end(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, switch=FA)
    tab._on_query()
    _answer(fake, M1, FA)
    assert [(r[0], r[3]) for r in fake.reads] == [(M1, FA), (M2, FA)]
    assert tab._seq is not None
    _answer(fake, M2, FA)
    assert [(r[0], r[3]) for r in fake.reads] == [(M1, FA), (M2, FA)]
    assert tab._seq is None
    assert tab._query_btn.isEnabled()


# ---------------------------------------------------------------- Timeout advances

def test_timeout_advances_to_next_recipient_and_shows_no_answer(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, switch=FA)
    tab._on_query()
    tab._on_seq_timeout()
    assert [r[0] for r in fake.reads] == [M1, M2]
    assert "no answer" in tab._info_label.text()
    assert tab._seq is not None


# ---------------------------------------------------------------- 'all' over 2 recipients

def test_all_two_recipients_order_and_single_open_request(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, switch=tab_query.SWITCH_ALL)
    tab._on_query()
    expected = [(M1, FA), (M1, F8), (M1, F9), (M2, FA), (M2, F8), (M2, F9)]
    assert len(fake.reads) == 1
    for step, (member, switch) in enumerate(expected[:-1], start=1):
        assert (fake.reads[-1][0], fake.reads[-1][3]) == (member, switch)
        _answer(fake, member, switch)
        assert len(fake.reads) == step + 1  # exactly one new request per answer
    assert [(r[0], r[3]) for r in fake.reads] == expected
    assert tab._seq is not None
    _answer(fake, M2, F9)
    assert len(fake.reads) == 6
    assert tab._seq is None


# ---------------------------------------------------------------- Last result display

def test_multi_display_one_line_per_recipient(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, switch=FA)
    tab._on_query()
    _answer(fake, M1, FA)
    lines = tab._info_label.text().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith(f"{db.member_name(M1)} (0x200): ")
    assert lines[1].startswith(f"{db.member_name(M2)} (0x480): ")
    assert lines[1].endswith(": -")  # not yet answered


def test_all_display_summary_per_recipient(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, switch=tab_query.SWITCH_ALL)
    tab._on_query()
    _answer(fake, M1, FA, status="timeout")
    _answer(fake, M1, F8)
    lines = tab._info_label.text().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith(f"{db.member_name(M1)} (0x200): current: no answer | min: ")
    assert " | max: -" in lines[0]
    assert lines[1] == f"{db.member_name(M2)} (0x480): current: - | min: - | max: -"


# ---------------------------------------------------------------- Input error clears display

@pytest.mark.parametrize("member", ["0x200;FOO", "FOO"])
def test_input_error_clears_last_result_and_sends_nothing(qapp, db, member):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, member="0x200", switch=FA)
    tab._on_query()
    _answer(fake, M1, FA)
    assert tab._info_label.text() != ""
    reads_before = len(fake.reads)
    _set_read(tab, member=member, switch=FA)
    tab._on_query()
    assert tab._info_label.text() == ""
    assert len(fake.reads) == reads_before


def test_input_error_clears_last_result_for_multi(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, member="0x200;0x480", switch=FA)
    tab._on_query()
    _answer(fake, M1, FA)
    _answer(fake, M2, FA)  # sequence must be finished, otherwise _on_query returns early
    assert tab._seq is None
    assert tab._info_label.text() != ""
    reads_before = len(fake.reads)
    _set_read(tab, member="0x200;FOO", switch=FA)
    tab._on_query()
    assert tab._info_label.text() == ""
    assert len(fake.reads) == reads_before
    assert tab._seq is None


# ---------------------------------------------------------------- One recipient, single switch

def test_single_recipient_single_switch_unchanged(qapp, db):
    fake = ProdFake()
    tab = _tab(db, fake)
    _set_read(tab, member="0x200", switch=FA)
    tab._on_query()
    assert fake.reads == [(M1, INDEX, "auto", FA, {})]
    assert tab._seq is None
    assert tab._multi_key is None
    _answer(fake, M1, FA)
    assert "Name:" in tab._info_label.text()
    assert "Status: ok" in tab._info_label.text()


# ---------------------------------------------------------------- Write mode with list

def test_write_mode_rejects_recipient_list(qapp, db, fake_elster):
    fake = ProdFake()
    tab = _write_tab(db, fake)
    tab._member_combo.setEditText("0x200;0x480")
    assert tab._write_plan() == (None, "Write mode accepts exactly one recipient.")
    assert not tab._send_btn.isEnabled()
    tab._on_send()
    assert fake.writes == []
