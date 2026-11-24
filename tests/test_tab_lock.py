"""Tests for the firmware lock of the Query/Scanner tabs, the status tab and the status bar.

Fake bridge with the same signals as MqttBridge; Qt runs offscreen.

Run: C:\\temp\\stiebel-Remote\\.venv\\Scripts\\python.exe -I -B -m pytest C:\\temp\\stiebel-Remote\\mk1-debug\\tests\\test_tab_lock.py -q
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

import elster_gui  # noqa: E402
import tab_query  # noqa: E402
import tab_scanner  # noqa: E402
import tab_status  # noqa: E402
from elster import ElsterDb  # noqa: E402

PROD_TEXT = "Only possible with debug firmware: the production firmware does not answer queries."
UNKNOWN_TEXT = "Waiting for firmware status..."


class FakeBridge(QObject):
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

    def __init__(self):
        super().__init__()
        self.reads = []
        self.scans = []

    def send_read(self, member, index, fmt=None, switch=None):
        self.reads.append((member, index, fmt, switch))

    def send_scan(self, member, from_idx, to_idx, delay_ms, fmt=None, switch=None):
        self.scans.append((member, from_idx, to_idx, delay_ms, fmt, switch))

    def send_stop(self):
        pass


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="session")
def db():
    return ElsterDb()


def _query_controls(tab):
    return [tab._member_combo, tab._index_edit, tab._format_combo, tab._switch_combo, tab._query_btn]


def _scanner_controls(tab):
    return [tab._member, tab._from, tab._to, tab._format, tab._switch, tab._delay, tab._start_btn]


def _assert_query_locked(tab, text):
    assert all(not w.isEnabled() for w in _query_controls(tab))
    assert not tab._fw_label.isHidden()
    assert tab._fw_label.text() == text


def _assert_query_open(tab):
    assert all(w.isEnabled() for w in _query_controls(tab))
    assert tab._fw_label.isHidden()


def _assert_query_open_prod(tab):
    # Production: format stays locked (raw/read has no short/long), rest open
    assert all(w.isEnabled() for w in _query_controls(tab) if w is not tab._format_combo)
    assert not tab._format_combo.isEnabled()
    assert tab._fw_label.isHidden()


def _assert_scanner_locked(tab, text):
    assert all(not w.isEnabled() for w in _scanner_controls(tab))
    assert not tab._fw_label.isHidden()
    assert tab._fw_label.text() == text


def _assert_scanner_open(tab):
    assert all(w.isEnabled() for w in _scanner_controls(tab))
    assert tab._fw_label.isHidden()


# ---------------------------------------------------------------- Query tab

def test_query_locked_when_unknown(qapp, db):
    fake = FakeBridge()
    tab = tab_query.QueryTab(db, fake)
    _assert_query_locked(tab, UNKNOWN_TEXT)


def test_query_open_when_prod(qapp, db):
    fake = FakeBridge()
    tab = tab_query.QueryTab(db, fake)
    fake.firmware_kind.emit("prod")
    _assert_query_open_prod(tab)


def test_query_open_when_debug(qapp, db):
    fake = FakeBridge()
    tab = tab_query.QueryTab(db, fake)
    fake.firmware_kind.emit("debug")
    _assert_query_open(tab)


def test_query_stays_open_from_debug_to_prod_and_locks_on_unknown(qapp, db):
    fake = FakeBridge()
    tab = tab_query.QueryTab(db, fake)
    fake.firmware_kind.emit("debug")
    fake.firmware_kind.emit("prod")
    _assert_query_open_prod(tab)
    fake.firmware_kind.emit("unknown")
    _assert_query_locked(tab, UNKNOWN_TEXT)


# ---------------------------------------------------------------- Scanner tab

def test_scanner_locked_when_unknown(qapp, db):
    fake = FakeBridge()
    tab = tab_scanner.ScannerTab(db, fake)
    _assert_scanner_locked(tab, UNKNOWN_TEXT)


def test_scanner_open_when_prod_without_format_and_delay(qapp, db):
    fake = FakeBridge()
    tab = tab_scanner.ScannerTab(db, fake)
    fake.firmware_kind.emit("prod")
    # Production: start and inputs open, format and pause locked (tooltip explains it)
    assert all(w.isEnabled() for w in _scanner_controls(tab) if w not in (tab._format, tab._delay))
    assert not tab._format.isEnabled()
    assert not tab._delay.isEnabled()
    assert tab._fw_label.isHidden()
    assert tab._fw_label.text() == ""


def test_scanner_open_when_debug(qapp, db):
    fake = FakeBridge()
    tab = tab_scanner.ScannerTab(db, fake)
    fake.firmware_kind.emit("debug")
    _assert_scanner_open(tab)
    fake.firmware_kind.emit("unknown")
    _assert_scanner_locked(tab, UNKNOWN_TEXT)


def test_scanner_stop_stays_usable_while_scanning(qapp, db):
    fake = FakeBridge()
    tab = tab_scanner.ScannerTab(db, fake)
    fake.firmware_kind.emit("debug")
    tab._on_start()
    assert not tab._start_btn.isEnabled()
    assert tab._stop_btn.isEnabled()
    fake.firmware_kind.emit("prod")
    assert not tab._start_btn.isEnabled()
    assert tab._stop_btn.isEnabled()
    tab._on_stop()
    assert tab._start_btn.isEnabled()  # prod: start open again
    assert not tab._format.isEnabled()  # prod: format stays locked
    assert not tab._stop_btn.isEnabled()


def test_scanner_reopens_after_scan_when_debug(qapp, db):
    fake = FakeBridge()
    tab = tab_scanner.ScannerTab(db, fake)
    fake.firmware_kind.emit("debug")
    tab._on_start()
    fake.scan_progress.emit({"index": 1, "done": 1, "total": 1, "switch": 0xFA})  # Scan fertig
    _assert_scanner_open(tab)
    assert not tab._stop_btn.isEnabled()


# ---------------------------------------------------------------- Status tab

@pytest.mark.parametrize("kind, text", [
    ("debug", "Debug"),
    ("prod", "Production"),
    ("unknown", "unknown"),
])
def test_status_tab_firmware_label(qapp, kind, text):
    fake = FakeBridge()
    tab = tab_status.StatusTab(fake)
    fake.firmware_kind.emit(kind)
    assert tab._lbl_fw.text() == text


# ---------------------------------------------------------------- Status bar

def test_status_bar_shows_firmware(qapp, db):
    fake = FakeBridge()
    win = elster_gui.MainWindow(db, fake)
    fake.availability.emit("online")
    assert win._lbl_esp.text() == "ESP: online"
    fake.firmware_kind.emit("debug")
    assert win._lbl_esp.text() == "ESP: online (Debug)"
    fake.firmware_kind.emit("prod")
    assert win._lbl_esp.text() == "ESP: online (Production)"
    fake.firmware_kind.emit("unknown")
    assert win._lbl_esp.text() == "ESP: online"
