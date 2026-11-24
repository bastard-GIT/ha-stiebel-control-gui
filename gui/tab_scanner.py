"""Scanner tab: query the index range of a CAN member via MQTT.

Debug firmware: range scan with format, switch and pause (cmd/scan).
Production firmware: range scan with switch (heatingpump/scan/start, fixed pace 150 ms)
and member search (heatingpump/scan/members).
"""

import csv

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import prod_api
from elster import ElsterDb, decode_value, switch_label
from export_io import save_scan_csv
from i18n import _
from mqtt_client import FW_DEBUG, FW_PROD, FW_UNKNOWN, MqttBridge

# Display texts are translated at import (the language is set before the tabs load).
FW_LOCK_TEXT = {
    FW_UNKNOWN: _("Waiting for firmware status..."),
}
MAX_RANGE = 4096
MIN_DELAY_MS = 100
DEFAULT_DELAY_MS = 200
COLUMNS = [_("Index"), _("Switch"), _("Name"), _("Raw value"), _("Status"), _("Decoded")]
RIGHT_ALIGNED_COLS = (0, 3)  # Index, raw value
DEFAULT_SWITCH = 0xFA

# Production: fixed pace, no pause (send_scan ignores delay_ms)
PROD_MS_PER_ID = 150
PROD_FORMAT_TEXT = _("Not available in production mode: format short/long exists only with "
                     "debug firmware, selection is auto.")
PROD_DELAY_TEXT = _("Not available in production mode: the pace is fixed at 150 ms, "
                    "no pause.")
PROD_MEMBER_TEXT = _("Only with production firmware (scan/members)")

# Member search (production)
MEMBER_ID_MAX = 0x7FF
MEMBER_DEFAULT_FROM = "0x000"
MEMBER_DEFAULT_TO = "0x7ff"
MEMBER_DEFAULT_INDEX = "0x000b"
MEMBER_COLUMNS = [_("ID"), _("Name"), _("Index"), _("Raw hex"), _("Raw dec")]
MEMBER_RIGHT_ALIGNED_COLS = (3, 4)
MEMBER_CSV_HEADER = ["id", "name", "index", "raw"]
RAW_NOT_SUPPORTED = 0x8000  # response "not supported"

# Display -> parameter for send_scan (INTERFACES 8.3)
FORMAT_CHOICES = ((_("auto"), "auto"), (_("short"), "short"), (_("long"), "long"))
SWITCH_ALL = "alle"  # three scans in sequence, no firmware option
SWITCH_CHOICES = ((_("current"), 0xFA), (_("min"), 0xF8), (_("max"), 0xF9), (_("all"), SWITCH_ALL))
SEQ_SWITCHES = (0xFA, 0xF8, 0xF9)  # order for 'alle' (all)


def parse_hex(text: str) -> int | None:
    """Hex ("0x..."), decimal or None for invalid input; only 0..0xFFFF."""
    s = text.strip()
    if not s:
        return None
    try:
        value = int(s, 16) if s.lower().startswith("0x") else int(s, 10)
    except ValueError:
        return None
    if not 0 <= value <= 0xFFFF:
        return None
    return value


def parse_member_id(text: str) -> int | None:
    """CAN ID (hex/decimal) for scan start: 0..0x7FF without PC ID 0x680, else None."""
    value = parse_hex(text)
    if value is None or value > MEMBER_ID_MAX or value == prod_api.PC_CAN_ID:
        return None
    return value


def save_member_csv(path: str, rows: list[list]) -> None:
    """Member search as CSV: semicolon, UTF-8 with BOM. Columns id, name, index, raw (decimal)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", lineterminator="\r\n")
        writer.writerow(MEMBER_CSV_HEADER)
        writer.writerows(rows)


class ScannerTab(QWidget):
    def __init__(self, db: ElsterDb, bridge: MqttBridge):
        super().__init__()
        self._db = db
        self._bridge = bridge
        # List of hits per (index, switch); index/raw/switch as int, status as str.
        self._items: dict[tuple[int, int], dict] = {}
        self._scan_fmt = "auto"  # format of the running scan, fallback for hits without format
        self._scanning = False
        # Sequence 'alle' (all): member, start, end, delay, fmt, count, queue (switches not yet started),
        # current, completed (finished scans). None for a single scan.
        self._seq: dict | None = None
        self._fw = FW_UNKNOWN
        # Member search (production): running, found responses {"id","index","raw"} as int
        self._member_scanning = False
        self._member_items: list[dict] = []

        self._build_ui()
        self._populate_members()
        self._update_estimate()
        self._update_member_estimate()
        self._apply_enabled()

        bridge.scan_progress.connect(self._on_progress)
        bridge.scan_result.connect(self._on_result)
        bridge.firmware_kind.connect(self._on_firmware)
        # Bridge fakes of older tests have no member_result signal; MqttBridge always has it.
        member_signal = getattr(bridge, "member_result", None)
        if member_signal is not None:
            member_signal.connect(self._on_member_result)

    # ---------- UI ----------

    def _build_ui(self) -> None:
        self._member = QComboBox()
        self._member_hex = QLineEdit()
        self._member_hex.setPlaceholderText(_("e.g. 0x7ff"))
        self._member_hex.setToolTip(_("Optional: hex ID instead of the selection above (0x000-0x7FF, without 0x680)."))
        self._from = QLineEdit("0x4e00")
        self._to = QLineEdit("0x4fff")
        self._format = QComboBox()
        for label, value in FORMAT_CHOICES:
            self._format.addItem(label, value)
        self._switch = QComboBox()
        for label, value in SWITCH_CHOICES:
            self._switch.addItem(label, value)
        self._delay = QSpinBox()
        self._delay.setRange(MIN_DELAY_MS, 10000)
        self._delay.setValue(DEFAULT_DELAY_MS)
        self._delay.setSuffix(" ms")

        form = QFormLayout()
        form.addRow(_("Member:"), self._member)
        form.addRow(_("or hex ID:"), self._member_hex)
        form.addRow(_("From (hex):"), self._from)
        form.addRow(_("To (hex):"), self._to)
        form.addRow(_("Format:"), self._format)
        form.addRow(_("Switch:"), self._switch)
        form.addRow(_("Delay:"), self._delay)

        self._estimate = QLabel()
        for edit in (self._from, self._to):
            edit.textChanged.connect(self._update_estimate)
        self._delay.valueChanged.connect(self._update_estimate)
        self._switch.currentIndexChanged.connect(self._update_estimate)

        self._start_btn = QPushButton(_("Start"))
        self._stop_btn = QPushButton(_("Stop"))
        self._stop_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)

        self._progress = QProgressBar()
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m")

        self._only_ok = QCheckBox(_("responses only (ok)"))
        self._only_ok.toggled.connect(self._rebuild_table)

        self._export_btn = QPushButton(_("Export CSV"))
        self._export_btn.clicked.connect(self._on_export)

        buttons = QHBoxLayout()
        buttons.addWidget(self._start_btn)
        buttons.addWidget(self._stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(self._only_ok)
        buttons.addWidget(self._export_btn)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        settings = QGroupBox(_("Scan parameters"))
        settings.setLayout(form)

        self._fw_label = QLabel("")
        self._fw_label.setWordWrap(True)
        self._fw_label.setStyleSheet("font-weight: bold;")
        self._lockable = (self._member, self._member_hex, self._from, self._to, self._format,
                          self._switch, self._delay, self._start_btn)

        # Member search (production)
        self._mem_from = QLineEdit(MEMBER_DEFAULT_FROM)
        self._mem_to = QLineEdit(MEMBER_DEFAULT_TO)
        self._mem_index = QLineEdit(MEMBER_DEFAULT_INDEX)
        for edit in (self._mem_from, self._mem_to, self._mem_index):
            edit.textChanged.connect(self._update_member_estimate)
        mem_form = QFormLayout()
        mem_form.addRow(_("ID from (hex):"), self._mem_from)
        mem_form.addRow(_("ID to (hex):"), self._mem_to)
        mem_form.addRow(_("Index (hex):"), self._mem_index)

        self._mem_estimate = QLabel()
        self._mem_estimate.setWordWrap(True)
        self._mem_start_btn = QPushButton(_("Search"))
        self._mem_stop_btn = QPushButton(_("Stop"))
        self._mem_stop_btn.setEnabled(False)
        self._mem_start_btn.clicked.connect(self._on_member_start)
        self._mem_stop_btn.clicked.connect(self._on_member_stop)

        self._mem_progress = QProgressBar()
        self._mem_progress.setValue(0)
        self._mem_progress.setFormat("%v / %m")

        self._mem_export_btn = QPushButton(_("Export as CSV"))
        self._mem_export_btn.clicked.connect(self._on_member_export)

        mem_buttons = QHBoxLayout()
        mem_buttons.addWidget(self._mem_start_btn)
        mem_buttons.addWidget(self._mem_stop_btn)
        mem_buttons.addStretch(1)
        mem_buttons.addWidget(self._mem_export_btn)

        self._mem_table = QTableWidget(0, len(MEMBER_COLUMNS))
        self._mem_table.setHorizontalHeaderLabels(MEMBER_COLUMNS)
        self._mem_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._mem_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._mem_table.verticalHeader().setVisible(False)
        self._mem_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._mem_table.itemDoubleClicked.connect(self._on_member_double_click)

        member_box = QGroupBox(_("Member search (production)"))
        member_layout = QVBoxLayout(member_box)
        member_layout.addLayout(mem_form)
        member_layout.addWidget(self._mem_estimate)
        member_layout.addLayout(mem_buttons)
        member_layout.addWidget(self._mem_progress)
        member_layout.addWidget(self._mem_table, 1)
        self._mem_inputs = (self._mem_from, self._mem_to, self._mem_index)

        layout = QVBoxLayout(self)
        layout.addWidget(self._fw_label)
        layout.addWidget(settings)
        layout.addWidget(self._estimate)
        layout.addLayout(buttons)
        layout.addWidget(self._progress)
        layout.addWidget(self._table, 1)
        layout.addWidget(member_box, 1)

    def _populate_members(self) -> None:
        for member in self._db.members:
            raw_id = member.get("can_id")
            if raw_id is None:
                continue
            # ElsterTable.json provides can_id as hex string ("0x180")
            can_id = int(raw_id, 0) if isinstance(raw_id, str) else int(raw_id)
            label = f"{member.get('name', '?')} (0x{can_id:03x})"
            self._member.addItem(label, can_id)

    # ---------- Helpers ----------

    def _range(self) -> tuple[int, int] | None:
        start = parse_hex(self._from.text())
        end = parse_hex(self._to.text())
        if start is None or end is None or start > end:
            return None
        return start, end

    def _member_range(self) -> tuple[int, int, int] | None:
        """(lo, hi, index) of the member search, or None for invalid input."""
        lo = parse_hex(self._mem_from.text())
        hi = parse_hex(self._mem_to.text())
        index = parse_hex(self._mem_index.text())
        if lo is None or hi is None or index is None:
            return None
        if lo > hi or hi > MEMBER_ID_MAX:
            return None
        return lo, hi, index

    def _ms_per_id(self) -> int:
        """Pace per index: production fixed 150 ms, debug the configured pause."""
        return PROD_MS_PER_ID if self._fw == FW_PROD else self._delay.value()

    def _scan_count(self) -> int:
        """Number of scans: 3 for 'alle' (all), else 1."""
        return len(SEQ_SWITCHES) if self._switch.currentData() == SWITCH_ALL else 1

    def _update_estimate(self, *_args) -> None:
        rng = self._range()
        if rng is None:
            self._estimate.setText(_("Range invalid (hex 0000-FFFF, from <= to)."))
            return
        per_scan = rng[1] - rng[0] + 1
        scans = self._scan_count()
        total = per_scan * scans
        ms = self._ms_per_id()
        seconds = total * ms / 1000.0
        prefix = (_("{scans} scans x {per_scan} indices = ").format(scans=scans, per_scan=per_scan)
                  if scans > 1 else "")
        self._estimate.setText(
            _("{prefix}{total} indices x {ms} ms = approx. {seconds:.1f} s").format(
                prefix=prefix, total=total, ms=ms, seconds=seconds))

    def _update_member_estimate(self, *_args) -> None:
        rng = self._member_range()
        if rng is None:
            self._mem_estimate.setText(
                _("Range invalid (ID hex 000-7FF, from <= to; index hex 0000-FFFF)."))
            return
        count = rng[1] - rng[0] + 1
        seconds = count * PROD_MS_PER_ID / 1000.0
        self._mem_estimate.setText(
            _("Duration: approx. {ms} ms per ID; {count} IDs = approx. {seconds:.0f} s.").format(
                ms=PROD_MS_PER_ID, count=count, seconds=seconds))

    def _set_scanning(self, active: bool) -> None:
        self._scanning = active
        self._apply_enabled()

    def _set_member_scanning(self, active: bool) -> None:
        self._member_scanning = active
        self._apply_enabled()

    def _on_firmware(self, kind: str) -> None:
        self._fw = kind
        if kind == FW_PROD:
            self._format.setCurrentIndex(self._format.findData("auto"))
        if kind not in (FW_DEBUG, FW_PROD):
            self._seq = None  # sequence aborts; Stop stays usable as for a single scan
        self._apply_enabled()
        self._update_estimate()

    def _apply_enabled(self) -> None:
        """Start and inputs with debug or production firmware, without a running scan.

        Stop stays usable while a scan runs. Range scan and member search block each other.
        """
        prod = self._fw == FW_PROD
        busy = self._scanning or self._member_scanning
        open_inputs = self._fw in (FW_DEBUG, FW_PROD) and not busy
        for widget in self._lockable:
            widget.setEnabled(open_inputs)
        # Production: format and pause not available, tooltip explains why
        self._format.setEnabled(open_inputs and not prod)
        self._delay.setEnabled(open_inputs and not prod)
        self._format.setToolTip(PROD_FORMAT_TEXT if prod else "")
        self._delay.setToolTip(PROD_DELAY_TEXT if prod else "")
        self._stop_btn.setEnabled(self._scanning)
        self._fw_label.setText(FW_LOCK_TEXT.get(self._fw, ""))
        self._fw_label.setVisible(self._fw == FW_UNKNOWN)
        # Member search: production only
        member_open = prod and not busy
        self._mem_start_btn.setEnabled(member_open)
        self._mem_start_btn.setToolTip("" if prod else PROD_MEMBER_TEXT)
        for edit in self._mem_inputs:
            edit.setEnabled(member_open)
        self._mem_stop_btn.setEnabled(self._member_scanning)

    def _row_visible(self, item: dict) -> bool:
        return not self._only_ok.isChecked() or item["status"] == "ok"

    def _table_row(self, item: dict) -> list[str]:
        index = item["index"]
        raw = item["raw"]
        status = item["status"]
        switch = item["switch"]
        name = self._db.signal_name(index)
        decoded = "-"
        signal = self._db.signal_by_index(index)
        if status == "ok" and (signal is not None or self._db.bits_for(index)):
            decoded = self._db.decode(index, raw, signal.get("type", "") if signal else None)
        return [
            f"0x{index:04x}",
            switch_label(switch),
            name,
            f"0x{raw:04x}",
            status,
            decoded,
        ]

    def _append_row(self, item: dict) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, text in enumerate(self._table_row(item)):
            cell = QTableWidgetItem(text)
            if col in RIGHT_ALIGNED_COLS:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(row, col, cell)

    def _rebuild_table(self, *_args) -> None:
        self._table.setRowCount(0)
        for item in self._items.values():
            if self._row_visible(item):
                self._append_row(item)

    def _member_name(self, can_id: int) -> str:
        """Name from the production table, else from the database, else "-"."""
        name = prod_api.member_name(can_id)
        if name is None:
            known = self._db.member_by_id(can_id)
            name = known["name"] if known else "-"
        return name

    def _member_display_name(self, item: dict) -> str:
        name = self._member_name(item["id"])
        if item["raw"] == RAW_NOT_SUPPORTED:
            name += _(" (not supported)")
        return name

    def _member_row(self, item: dict) -> list[str]:
        return [
            f"0x{item['id']:03x}",
            self._member_display_name(item),
            f"0x{item['index']:04x}",
            f"0x{item['raw']:04x}",
            str(item["raw"]),
        ]

    def _append_member_row(self, item: dict) -> None:
        row = self._mem_table.rowCount()
        self._mem_table.insertRow(row)
        for col, text in enumerate(self._member_row(item)):
            cell = QTableWidgetItem(text)
            if col in MEMBER_RIGHT_ALIGNED_COLS:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._mem_table.setItem(row, col, cell)

    def _selected_member(self) -> int | None:
        """Member from a valid hex ID, else from the selection. None on error (with notice)."""
        text = self._member_hex.text()
        if text.strip():
            member = parse_member_id(text)
            if member is None:
                QMessageBox.warning(self, _("Input"),
                                    _("Invalid hex ID: 0x000-0x7FF, without 0x680 (PC)."))
            return member
        member = self._member.currentData()
        if member is None:
            QMessageBox.warning(self, _("Input"), _("No member selected."))
        return member

    # ---------- Actions: range scan ----------

    def _on_start(self) -> None:
        rng = self._range()
        if rng is None:
            QMessageBox.warning(self, _("Input"), _("Range invalid (hex 0000-FFFF, from <= to)."))
            return
        member = self._selected_member()
        if member is None:
            return
        start, end = rng
        fmt = self._format.currentData()
        if self._fw == FW_PROD:
            fmt = "auto"  # production knows only auto
        switch = self._switch.currentData()
        if fmt == "short" and end > 0xFF:
            QMessageBox.warning(
                self,
                _("Input"),
                _("Short format only for indices up to 0xFF. Please select format auto or long, "
                  "or limit the range."),
            )
            return
        count = end - start + 1
        scans = self._scan_count()
        total = count * scans
        if total > MAX_RANGE:
            if scans > 1:
                text = _("The range covers {total} indices (limit {limit}) over {scans} scans. "
                         "The scan may take a very long time.\n\nStart anyway?").format(
                             total=total, limit=MAX_RANGE, scans=scans)
            else:
                text = _("The range covers {total} indices (limit {limit}). "
                         "The scan may take a very long time.\n\nStart anyway?").format(
                             total=total, limit=MAX_RANGE)
            answer = QMessageBox.warning(
                self,
                _("Large range"),
                text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self._items.clear()
        self._table.setRowCount(0)
        self._progress.setRange(0, total)
        self._progress.setValue(0)
        self._scan_fmt = fmt
        if switch == SWITCH_ALL:
            self._seq = {
                "member": member, "start": start, "end": end, "delay": self._delay.value(),
                "fmt": fmt, "count": count, "queue": list(SEQ_SWITCHES[1:]),
                "current": SEQ_SWITCHES[0], "completed": 0,
            }
            self._set_scanning(True)
            self._bridge.send_scan(member, start, end, self._delay.value(), fmt, SEQ_SWITCHES[0])
            return
        self._set_scanning(True)
        self._bridge.send_scan(member, start, end, self._delay.value(), fmt, switch)

    def _on_stop(self) -> None:
        self._seq = None  # no further scans of the sequence
        self._bridge.send_stop()
        self._set_scanning(False)

    def _on_progress(self, data: dict) -> None:
        if data.get("mode") == "members":  # member search, not this bar
            self._on_member_progress(data)
            return
        if self._seq is not None:
            self._on_seq_progress(data)
            return
        total = int(data.get("total", 0))
        done = int(data.get("done", 0))
        if total > 0 and self._progress.maximum() != total:
            self._progress.setRange(0, total)
        self._progress.setValue(done)
        if total > 0 and done >= total:
            self._set_scanning(False)

    def _on_seq_progress(self, data: dict) -> None:
        """Sequence 'alle' (all): overall progress; when the current scan reports 'done', start the next."""
        seq = self._seq
        state = data.get("state")
        if state == "stopped":
            self._seq = None
            self._set_scanning(False)
            return
        switch = int(data.get("switch", DEFAULT_SWITCH))
        if switch != seq["current"]:
            return  # progress of another scan in the sequence
        done = int(data.get("done", 0))
        count = seq["count"]
        if state != "done":
            self._progress.setValue(seq["completed"] * count + done)
            return
        seq["completed"] += 1
        self._progress.setValue(seq["completed"] * count)
        if seq["queue"]:
            seq["current"] = seq["queue"].pop(0)
            self._bridge.send_scan(seq["member"], seq["start"], seq["end"], seq["delay"],
                                   seq["fmt"], seq["current"])
            return
        self._seq = None
        self._set_scanning(False)

    def _on_result(self, data: dict) -> None:
        payload_fmt = str(data.get("format", self._scan_fmt))
        for raw_item in data.get("items", []):
            item = {
                "index": int(raw_item["index"]),
                "raw": int(raw_item["raw"]),
                "status": str(raw_item["status"]),
                "switch": int(raw_item.get("switch", DEFAULT_SWITCH)),
                "format": str(raw_item.get("format", payload_fmt)),
            }
            key = (item["index"], item["switch"])
            replaced = key in self._items
            self._items[key] = item  # same (index, switch) replaces the hit
            if replaced:
                self._rebuild_table()
            elif self._row_visible(item):
                self._append_row(item)

    def _on_export(self) -> None:
        if not self._items:
            QMessageBox.information(self, _("Export"), _("No results to export."))
            return
        path, _unused = QFileDialog.getSaveFileName(
            self, _("Export CSV"), "scan.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            save_scan_csv(path, list(self._items.values()), self._db)
        except OSError as exc:
            QMessageBox.critical(self, _("Export"), _("Save failed:\n{exc}").format(exc=exc))

    # ---------- Actions: member search (production) ----------

    def _on_member_start(self) -> None:
        rng = self._member_range()
        if rng is None:
            QMessageBox.warning(self, _("Member search"),
                                _("Range invalid (ID hex 000-7FF, from <= to; "
                                  "index hex 0000-FFFF)."))
            return
        lo, hi, index = rng
        self._member_items.clear()
        self._mem_table.setRowCount(0)
        self._mem_progress.setRange(0, hi - lo + 1)
        self._mem_progress.setValue(0)
        self._set_member_scanning(True)
        try:
            self._bridge.send_member_scan(lo, hi, index)
        except ValueError as exc:
            self._set_member_scanning(False)
            QMessageBox.warning(self, _("Member search"), str(exc))

    def _on_member_stop(self) -> None:
        self._bridge.send_stop()
        self._set_member_scanning(False)

    def _on_member_progress(self, data: dict) -> None:
        state = data.get("state")
        if state == "running" and not self._member_scanning:
            self._set_member_scanning(True)
        total = int(data.get("total", 0))
        done = int(data.get("done", 0))
        if total > 0 and self._mem_progress.maximum() != total:
            self._mem_progress.setRange(0, total)
        self._mem_progress.setValue(done)
        if state in ("done", "stopped"):
            self._set_member_scanning(False)

    def _on_member_result(self, data: dict) -> None:
        item = {"id": int(data["id"]), "index": int(data["index"]), "raw": int(data["raw"])}
        self._member_items.append(item)
        self._append_member_row(item)

    def _on_member_double_click(self, cell: QTableWidgetItem) -> None:
        """Double-click copies the row's ID into the scanner's hex ID field."""
        id_text = self._mem_table.item(cell.row(), 0).text()
        self._member_hex.setText(id_text)

    def _on_member_export(self) -> None:
        if not self._member_items:
            QMessageBox.information(self, _("Export"), _("No results to export."))
            return
        path, _unused = QFileDialog.getSaveFileName(
            self, _("Export CSV"), "members.csv", "CSV (*.csv)"
        )
        if not path:
            return
        rows = [
            [f"0x{item['id']:03x}", self._member_display_name(item),
             f"0x{item['index']:04x}", item["raw"]]
            for item in self._member_items
        ]
        try:
            save_member_csv(path, rows)
        except OSError as exc:
            QMessageBox.critical(self, _("Export"), _("Save failed:\n{exc}").format(exc=exc))
