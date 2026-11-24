"""Tab 'Query' (query): targeted read and write of an Elster signal via MQTT and history of results."""

import re
from datetime import datetime

from PySide6.QtCore import Qt, QStringListModel, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from elster import ElsterDb, decode_value, switch_label
from mqtt_client import FW_DEBUG, FW_PROD, FW_UNKNOWN, MqttBridge

try:
    from elster import build_write_request, encode_value, is_writable
except ImportError:  # write functions in elster.py still missing: writing stays locked
    _WRITE_MISSING = "Writing not available yet (elster write functions missing)."

    def encode_value(type_name, text):
        raise ValueError(_WRITE_MISSING)

    def build_write_request(member, index, value, fmt="auto"):
        raise ValueError(_WRITE_MISSING)

    def is_writable(type_name):
        return False

MAX_HISTORY = 1000
FW_LOCK_TEXT = {
    FW_UNKNOWN: "Waiting for firmware status...",
}
# Production firmware (raw/read): these options do not exist there, they stay disabled.
PROD_OFF_TIP = "Not available in production mode: the production firmware (raw/read) does not know {}."
# Same order as self._prod_off: sender, timeout, format
PROD_OFF_WHAT = ("no sender field", "no timeout", "no format short/long")
DEFAULT_TYPE = "et_default"
STATUS_TEXT = {
    "ok": "ok",
    "unsupported": "not supported",
    "timeout": "Timeout",
    "busy": "Firmware busy (scan running)",
}
REJECTED_TEXT = ("Write rejected (status rejected): firmware built without can_raw_write "
                 "or a lock rule of the firmware.")
PROD_CONFIRM_LINE = "Firmware: production (raw/write, result may be 'rejected')"
WRITE_ACK_TEXT = {2: "response", 3: "ack", 4: "write ack", 5: "write respond"}
HEADERS = ["Time", "Mode", "Member", "Index", "Switch", "Name", "Type", "Raw hex", "Raw dec",
           "Value", "Ack", "Readback value", "Status"]
_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]{1,4}$", re.IGNORECASE)
_MEMBER_LABEL_RE = re.compile(r"\((0x[0-9a-fA-F]{1,4})\)\s*$")

# Display -> parameter for send_read (INTERFACES 8.3)
FORMAT_CHOICES = (("auto", "auto"), ("short", "short"), ("long", "long"))
SWITCH_ALL = "alle"  # no firmware option: three normal queries in sequence
SWITCH_CHOICES = (("current", 0xFA), ("min", 0xF8), ("max", 0xF9), ("all", SWITCH_ALL))
DEFAULT_SWITCH = 0xFA
SEQ_SWITCHES = (0xFA, 0xF8, 0xF9)  # order for 'alle'
SEQ_TIMEOUT_MS = 3000  # wait time per switch, then continue to the next; shortened in tests
NO_ANSWER = "no answer"
SUMMARY_LABELS = (("current", 0xFA), ("min", 0xF8), ("max", 0xF9))
OWN_CAN_ID = 0x680  # own ID of the controller: never allowed as target
MODE_READ = "read"
MODE_WRITE = "write"
MODE_CHOICES = (("Read", MODE_READ), ("Write", MODE_WRITE))
STANDARD_TYPE_LABEL = "Default (type of the signal)"


def _to_int(value):
    """Converts a number or '0x..' string to int; None if not possible."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except ValueError:
        return None


def _parse_raw_hex(text: str) -> int:
    """Raw value in hex ('0x0208' or '0208'); ValueError if invalid."""
    t = text.strip()
    if t[:2].lower() == "0x":
        t = t[2:]
    try:
        raw = int(t, 16)
    except ValueError:
        raise ValueError(f"Raw value is not a hex value: '{text}'") from None
    if not 0 <= raw <= 0xFFFF:
        raise ValueError("Raw value must be between 0x0000 and 0xFFFF.")
    return raw


class QueryTab(QWidget):
    def __init__(self, db: ElsterDb, bridge: MqttBridge):
        super().__init__()
        self._db = db
        self._bridge = bridge
        # (member, index, switch) of the last query; only its response fills "Last result".
        self._pending: tuple[int, int, int] | None = None
        self._fw = FW_UNKNOWN
        # 'alle' sequence: member, index, fmt, queue (switches not yet sent), current.
        self._seq: dict | None = None
        # Result per switch for the line 'aktuell | min | max'; None = not yet received.
        self._alle_key: tuple[int, int] | None = None
        self._alle_results: dict[int, str | None] = {}
        self._seq_timer = QTimer(self)
        self._seq_timer.setSingleShot(True)
        self._seq_timer.timeout.connect(self._on_seq_timeout)
        # Writing: send request open (until write_result) and last read value per (member, index).
        self._write_pending = False
        self._last_read: dict[tuple[int, int], str] = {}
        # Sender and timeout of the running read query (also for the 'alle' sequence).
        self._read_src = OWN_CAN_ID
        self._read_timeout = 1000

        self._mode_combo = QComboBox()
        for label, value in MODE_CHOICES:
            self._mode_combo.addItem(label, value)

        self._member_combo = QComboBox()
        self._member_combo.setEditable(True)
        self._member_combo.setInsertPolicy(QComboBox.NoInsert)
        for m in db.members:
            can_id = _to_int(m.get("can_id"))  # Referenz liefert "0x180" als String
            if can_id is None:
                continue
            self._member_combo.addItem(f"{m.get('name', '')} (0x{can_id:03x})", can_id)
        self._member_combo.setEditText(self._member_combo.itemText(0))

        # Sender (read only): default own ID 0x680, otherwise any CAN ID (cmd/read "src").
        self._src_combo = QComboBox()
        self._src_combo.setEditable(True)
        self._src_combo.setInsertPolicy(QComboBox.NoInsert)
        self._src_combo.addItem(f"PC (0x{OWN_CAN_ID:03x})", OWN_CAN_ID)
        for m in db.members:
            can_id = _to_int(m.get("can_id"))
            if can_id is None or can_id == OWN_CAN_ID:
                continue
            self._src_combo.addItem(f"{m.get('name', '')} (0x{can_id:03x})", can_id)
        self._src_combo.setToolTip("Sender CAN ID of the request. An ID that itself sends on the bus "
                                   "can cause collisions; its responses also go to the real device.")
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(100, 10000)
        self._timeout_spin.setSingleStep(500)
        self._timeout_spin.setValue(1000)
        self._timeout_spin.setSuffix(" ms")
        self._timeout_spin.setToolTip("Response timeout of the firmware. FES (0x100) needs 0.5..1.5 s, "
                                      "with backlog up to ~4 s.")

        self._index_edit = QLineEdit()
        self._index_edit.setPlaceholderText("Hex (0x4ef0) or signal name")
        self._completer_model = QStringListModel(self._signal_labels(), self)
        completer = QCompleter(self._completer_model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._index_edit.setCompleter(completer)
        self._index_edit.returnPressed.connect(self._on_query)

        self._format_combo = QComboBox()
        for label, value in FORMAT_CHOICES:
            self._format_combo.addItem(label, value)
        self._switch_combo = QComboBox()
        for label, value in SWITCH_CHOICES:
            self._switch_combo.addItem(label, value)

        # Write inputs
        self._value_edit = QLineEdit()
        self._value_edit.setPlaceholderText("e.g. 52.0 (or hex for raw value)")
        self._type_combo = QComboBox()
        self._type_combo.addItem(STANDARD_TYPE_LABEL, None)
        for type_name in sorted({s.get("type") for s in db.signals if s.get("type")}
                                | {DEFAULT_TYPE}):
            self._type_combo.addItem(type_name, type_name)
        self._raw_check = QCheckBox("Raw value (hex)")
        self._arm_check = QCheckBox("Enable writing")
        self._preview_label = QLabel("")
        self._preview_label.setWordWrap(True)
        self._preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self._query_btn = QPushButton("Query")
        self._query_btn.clicked.connect(self._on_query)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.clicked.connect(self._on_clear)

        self._info_label = QLabel("–")
        self._info_label.setWordWrap(True)
        self._info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self._hint_label = QLabel("")
        self._hint_label.setWordWrap(True)
        self._warn_label = QLabel("")  # Write status 'rejected': warning color, otherwise empty
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet("color: #c00000; font-weight: bold;")

        self._fw_label = QLabel("")
        self._fw_label.setWordWrap(True)
        self._fw_label.setStyleSheet("font-weight: bold;")

        # Controls by mode: read-only and write-only (visibility).
        self._switch_lbl = QLabel("Switch:")
        self._src_lbl = QLabel("Sender:")
        self._timeout_lbl = QLabel("Timeout:")
        self._value_lbl = QLabel("Value:")
        self._type_lbl = QLabel("Type:")
        self._opts_lbl = QLabel("Options:")
        self._preview_lbl = QLabel("Preview:")
        self._read_only = (self._switch_lbl, self._switch_combo, self._query_btn,
                           self._src_lbl, self._src_combo, self._timeout_lbl, self._timeout_spin)
        self._write_only = (self._value_lbl, self._value_edit, self._type_lbl, self._type_combo,
                            self._opts_lbl, self._raw_check, self._arm_check,
                            self._preview_lbl, self._preview_label, self._send_btn)
        self._write_inputs = (self._value_edit, self._type_combo, self._raw_check, self._arm_check)
        # Firmware lock: read controls plus mode selection; write inputs individually in _update_write_ui.
        self._lockable = (self._mode_combo, self._member_combo, self._index_edit, self._format_combo,
                          self._switch_combo, self._query_btn, self._src_combo, self._timeout_spin)
        # Not available in production mode (disabled with tooltip); remember original tooltips.
        self._prod_off = (self._src_combo, self._timeout_spin, self._format_combo)
        self._default_tips = {w: w.toolTip() for w in self._prod_off}

        form = QFormLayout()
        form.addRow("Mode:", self._mode_combo)
        form.addRow("Recipient:", self._member_combo)
        form.addRow(self._src_lbl, self._src_combo)
        form.addRow(self._timeout_lbl, self._timeout_spin)
        form.addRow("Index:", self._index_edit)
        form.addRow("Format:", self._format_combo)
        form.addRow(self._switch_lbl, self._switch_combo)
        form.addRow(self._value_lbl, self._value_edit)
        form.addRow(self._type_lbl, self._type_combo)
        opts_row = QHBoxLayout()
        opts_row.addWidget(self._raw_check)
        opts_row.addWidget(self._arm_check)
        opts_row.addStretch(1)
        form.addRow(self._opts_lbl, opts_row)
        form.addRow(self._preview_lbl, self._preview_label)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._query_btn)
        btn_row.addWidget(self._send_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._clear_btn)
        form.addRow("", btn_row)

        query_box = QGroupBox("Query")
        query_box.setLayout(form)

        result_box = QGroupBox("Last result")
        result_layout = QVBoxLayout(result_box)
        result_layout.addWidget(self._info_label)
        result_layout.addWidget(self._hint_label)
        result_layout.addWidget(self._warn_label)

        self._table = QTableWidget(0, len(HEADERS))
        self._table.setHorizontalHeaderLabels(HEADERS)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(HEADERS.index("Name"), QHeaderView.Stretch)

        history_box = QGroupBox(f"History (newest on top, max. {MAX_HISTORY})")
        history_layout = QVBoxLayout(history_box)
        history_layout.addWidget(self._table)

        root = QVBoxLayout(self)
        root.addWidget(self._fw_label)
        root.addWidget(query_box)
        root.addWidget(result_box)
        root.addWidget(history_box, 1)

        # Connect signals only after all widgets are built (slots access all of them).
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._member_combo.currentTextChanged.connect(lambda _t: self._update_write_ui())
        self._index_edit.textChanged.connect(lambda _t: self._update_write_ui())
        self._format_combo.currentIndexChanged.connect(lambda _i: self._update_write_ui())
        self._value_edit.textChanged.connect(lambda _t: self._update_write_ui())
        self._type_combo.currentIndexChanged.connect(lambda _i: self._update_write_ui())
        self._raw_check.toggled.connect(lambda _c: self._update_write_ui())
        self._arm_check.toggled.connect(lambda _c: self._update_write_ui())

        self._apply_firmware(FW_UNKNOWN)
        bridge.read_result.connect(self._on_read_result)
        bridge.firmware_kind.connect(self._apply_firmware)
        if hasattr(bridge, "write_result"):  # older bridges without write function
            bridge.write_result.connect(self._on_write_result)
        if hasattr(bridge, "connected"):
            bridge.connected.connect(self._on_connected)

    # ----- Firmware lock ---------------------------------------------------

    def _apply_firmware(self, kind: str) -> None:
        """Query controls active for debug and production firmware; hint only for unknown."""
        self._fw = kind
        if kind not in (FW_DEBUG, FW_PROD) and self._seq is not None:
            self._seq_abort("Query 'all' aborted: firmware status unknown.")
        self._write_pending = False  # firmware change: response to open write request uncertain
        self._disarm()
        if kind == FW_PROD:
            self._reset_prod_options()
        self._apply_lock()

    def _fw_ok(self) -> bool:
        return self._fw in (FW_DEBUG, FW_PROD)

    def _reset_prod_options(self) -> None:
        """Production: unavailable options reset to defaults (src 0x680, format auto, timeout 1000)."""
        self._src_combo.setCurrentIndex(0)  # PC (0x680)
        self._format_combo.setCurrentIndex(self._format_combo.findData("auto"))
        self._timeout_spin.setValue(1000)

    def _on_connected(self, online: bool) -> None:
        if not online:
            self._disarm()

    def _apply_lock(self) -> None:
        allowed = self._fw_ok()
        prod = self._fw == FW_PROD
        for widget in self._lockable:
            widget.setEnabled(allowed and not (prod and widget in self._prod_off))
        for widget, what in zip(self._prod_off, PROD_OFF_WHAT):
            widget.setToolTip(PROD_OFF_TIP.format(what) if prod else self._default_tips[widget])
        if self._seq is not None:  # firmware answers only one request at a time
            self._query_btn.setEnabled(False)
            self._mode_combo.setEnabled(False)
        self._fw_label.setText(FW_LOCK_TEXT.get(self._fw, ""))
        self._fw_label.setVisible(not allowed)
        self._update_write_ui()

    def _disarm(self) -> None:
        self._arm_check.setChecked(False)

    # ----- Write mode -----------------------------------------------------

    def _mode(self) -> str:
        return self._mode_combo.currentData()

    def _on_mode_changed(self, _index: int) -> None:
        self._disarm()
        self._hint_label.setText("")
        self._warn_label.setText("")
        self._update_write_ui()

    def _update_write_ui(self) -> None:
        """Visibility, preview and send release from mode, firmware and inputs."""
        write = self._mode() == MODE_WRITE
        allowed = self._fw_ok()
        for widget in self._read_only:
            widget.setVisible(not write)
        for widget in self._write_only:
            widget.setVisible(write)
        for widget in self._write_inputs:
            widget.setEnabled(allowed)
        plan, error = self._write_plan() if write else (None, "")
        if not write:
            self._preview_label.setText("")
            self._preview_label.setStyleSheet("")
        elif plan is None:
            self._preview_label.setText(error)
            self._preview_label.setStyleSheet("color: #c00000; font-weight: bold;")
        else:
            self._preview_label.setText(
                f"Raw value: 0x{plan['raw']:04x} ({plan['raw']})   "
                f"Frame: 0x{plan['can_id']:03x}  {bytes(plan['frame']).hex(' ')}"
            )
            self._preview_label.setStyleSheet("")
        self._send_btn.setEnabled(
            write and allowed and plan is not None and self._arm_check.isChecked()
            and not self._write_pending
        )

    def _write_plan(self):
        """Checks all write inputs. Returns (plan, '') or (None, error text)."""
        member, member_error = self._parse_member(self._member_combo.currentText())
        if member is None:
            return None, member_error
        index, hint = self._resolve_index(self._index_edit.text())
        if index is None:
            return None, hint
        if not 0 <= index <= 0xFFFF:
            return None, "Index must be between 0x0000 and 0xFFFF."
        fmt = self._format_combo.currentData()
        type_name = self._effective_type(index)
        text = self._value_edit.text().strip()
        if not text:
            return None, "Please enter a value."
        try:
            if self._raw_check.isChecked():
                raw = _parse_raw_hex(text)
            else:
                if not is_writable(type_name):
                    return None, f"Type '{type_name}' is not writable."
                raw = encode_value(type_name, text)
                if not 0 <= raw <= 0xFFFF:
                    raise ValueError("Raw value outside 0x0000..0xFFFF.")
            can_id, frame = build_write_request(member, index, raw, fmt)
        except ValueError as exc:
            return None, f"Invalid value: {exc}"
        return {"member": member, "index": index, "type": type_name, "fmt": fmt,
                "raw": raw, "can_id": can_id, "frame": frame, "text": text}, ""

    def _effective_type(self, index: int) -> str:
        chosen = self._type_combo.currentData()
        if chosen:
            return chosen
        sig = self._db.signal_by_index(index)
        return (sig.get("type") if sig else None) or DEFAULT_TYPE

    def _confirm_text(self, plan: dict) -> str:
        member = plan["member"]
        index = plan["index"]
        sig = self._db.signal_by_index(index)
        name = sig.get("name", self._db.signal_name(index)) if sig else self._db.signal_name(index)
        lines = [
            "Send write command?",
            f"Member: {self._db.member_name(member)} (0x{member:03x})",
            f"Index: 0x{index:04x} {name}",
            f"Type: {plan['type']}",
            f"New value: {self._decode(plan['type'], plan['raw'], index)}",
            f"Raw value: 0x{plan['raw']:04x} ({plan['raw']})",
            f"Frame: 0x{plan['can_id']:03x}  {bytes(plan['frame']).hex(' ')}",
        ]
        if self._fw == FW_PROD:
            lines.insert(1, PROD_CONFIRM_LINE)
        last = self._last_read.get((member, index))
        if last is not None:
            lines.append(f"Last read value: {last}")
        if self._db.member_by_id(member) is None:
            lines.append("Warning: unknown member")
        if sig is None:
            lines.append("Warning: unknown signal")
        return "\n".join(lines)

    def _confirm_write(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Confirm write", text,
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _on_send(self):
        if not self._send_btn.isEnabled():
            return
        plan, error = self._write_plan()
        if plan is None:
            self._hint_label.setText(error)
            return
        if not self._confirm_write(self._confirm_text(plan)):
            return
        try:
            self._bridge.send_write(plan["member"], plan["index"], plan["raw"], plan["fmt"])
        except ValueError as exc:
            self._hint_label.setText(f"Send rejected: {exc}")
            return
        self._write_pending = True
        self._warn_label.setText("")
        self._info_label.setText(
            f"Write request sent: member 0x{plan['member']:03x}, index 0x{plan['index']:04x}, "
            f"raw value 0x{plan['raw']:04x} ..."
        )
        self._update_write_ui()

    def _on_write_result(self, payload: dict):
        member = _to_int(payload.get("member"))
        index = _to_int(payload.get("index"))
        value = _to_int(payload.get("value"))
        readback = _to_int(payload.get("readback"))
        ack = _to_int(payload.get("ack"))
        status = str(payload.get("status") or "-")
        if index is None:
            return
        sig = self._db.signal_by_index(index)
        name = sig.get("name", self._db.signal_name(index)) if sig else self._db.signal_name(index)
        type_name = (sig.get("type") if sig else None) or DEFAULT_TYPE
        sent_text = self._decode(type_name, value, index)
        readback_text = self._decode(type_name, readback, index) if readback is not None else "–"
        ack_text = WRITE_ACK_TEXT.get(ack, "-")
        value_hex = f"0x{value:04x}" if value is not None else "–"
        value_dec = str(value) if value is not None else "–"
        member_text = self._db.member_name(member) if member is not None else "–"

        if member is not None and readback is not None and status != "timeout":
            self._last_read[(member, index)] = f"{readback_text} (0x{readback:04x})"
        self._info_label.setText(
            f"Write: {name}\n"
            f"Member: {member_text}   Index: 0x{index:04x}   Type: {type_name}\n"
            f"Sent: {value_hex} ({value_dec})   Value: {sent_text}\n"
            f"Ack: {ack_text}   Readback value: {readback_text}\n"
            f"Status: {status}"
        )
        self._warn_label.setText(REJECTED_TEXT if status == "rejected" else "")
        self._add_history_row([
            datetime.now().strftime("%H:%M:%S"),
            "Write",
            member_text,
            f"0x{index:04x}",
            "",
            name,
            type_name,
            value_hex,
            value_dec,
            sent_text,
            ack_text,
            readback_text,
            status,
        ])
        self._write_pending = False
        self._disarm()  # locked again after each completed write
        self._update_write_ui()

    # ----- Helper functions -----------------------------------------------

    def _parse_member(self, text: str) -> tuple[int | None, str]:
        """Member from list entry 'NAME (0x180)', number, '0x..' or reference name.
        Returns (can_id, '') or (None, hint)."""
        text = text.strip()
        if not text:
            return None, "Please enter a member (name, 0x-hex or number)."
        value = None
        m = _MEMBER_LABEL_RE.search(text)
        if m:
            value = int(m.group(1), 16)
        else:
            value = _to_int(text)
            if value is None:
                for member in self._db.members:
                    if str(member.get("name", "")).lower() == text.lower():
                        value = _to_int(member.get("can_id"))
                        break
        if value is None:
            return None, (f"Member '{text}' unknown: enter reference name, 0x-hex (0x4ab) or number.")
        if value == OWN_CAN_ID:
            return None, "Member 0x680 is the own ID and not allowed."
        if not 0 <= value <= 0x7FF:
            return None, f"Member 0x{value:x} outside 0x000..0x7FF."
        return value, ""

    def _parse_src(self) -> tuple[int | None, str]:
        """Sender from 'NAME (0x700)', number or '0x..'. 0x680 allowed (default)."""
        text = self._src_combo.currentText().strip()
        m = _MEMBER_LABEL_RE.search(text)
        value = int(m.group(1), 16) if m else _to_int(text)
        if value is None:
            for member in self._db.members:
                if str(member.get("name", "")).lower() == text.lower():
                    value = _to_int(member.get("can_id"))
                    break
        if value is None or not 0 <= value <= 0x7FF:
            return None, f"Sender '{text}' invalid: 0x000..0x7FF as hex, number or reference name."
        return value, ""

    def _read_extra(self) -> dict:
        """Extra fields for send_read only when they differ from the default (older bridges without src/timeout_ms)."""
        extra = {}
        if self._read_src != OWN_CAN_ID:
            extra["src"] = self._read_src
        if self._read_timeout != 1000:
            extra["timeout_ms"] = self._read_timeout
        return extra

    def _current_member(self) -> int | None:
        return self._parse_member(self._member_combo.currentText())[0]

    def _signal_labels(self):
        labels = []
        for s in self._db.search_signals(""):  # empty substring matches all signals
            idx = _to_int(s.get("index_int"))
            if idx is not None:
                labels.append(f"{s.get('name', '')} (0x{idx:04x})")
        return labels

    def _resolve_index(self, text: str):
        """Hex index or name search. Returns (index, hint) or (None, error text)."""
        text = text.strip()
        if not text:
            return None, "Please enter an index or signal name."
        if _HEX_RE.match(text):
            return int(text, 16), ""
        # allow selection from completer format "NAME (0x....)"
        m = re.search(r"\((0x[0-9a-fA-F]{1,4})\)\s*$", text)
        if m:
            return int(m.group(1), 16), ""
        matches = self._db.search_signals(text)
        if not matches:
            return None, f"No signal found for '{text}'."
        exact = [s for s in matches if s.get("name", "").lower() == text.lower()]
        chosen = (exact or matches)[0]
        idx = _to_int(chosen.get("index_int"))
        if idx is None:
            return None, f"Signal '{chosen.get('name')}' has no valid index."
        hint = f"Name search: {chosen.get('name')} (0x{idx:04x})"
        if len(matches) > 1:
            hint += f" – {len(matches) - 1} more matches, please enter more precisely or choose a suggestion"
        return idx, hint

    def _decode(self, type_name: str, raw, index: int | None = None):
        if raw is None:
            return "–"
        try:
            if index is not None:
                return self._db.decode(index, raw, type_name)
            return decode_value(type_name, raw)
        except Exception as exc:  # decoder must not crash the GUI
            return f"Error: {exc}"

    # ----- Slots -----------------------------------------------------------

    def _on_query(self):
        if self._seq is not None:
            return  # 'alle' sequence already running
        if self._mode() == MODE_WRITE:
            return  # Enter in the index field never sends a write command
        member, member_error = self._parse_member(self._member_combo.currentText())
        if member is None:
            self._hint_label.setText(member_error)
            return
        index, hint = self._resolve_index(self._index_edit.text())
        if index is None:
            self._hint_label.setText(hint)
            return
        if not 0 <= index <= 0xFFFF:
            self._hint_label.setText("Index must be between 0x0000 and 0xFFFF.")
            return
        fmt = self._format_combo.currentData()
        switch = self._switch_combo.currentData()
        if fmt == "short" and index > 0xFF:
            self._hint_label.setText("Short format only for index up to 0xFF; please choose format auto or long.")
            return
        src, src_error = self._parse_src()
        if src is None:
            self._hint_label.setText(src_error)
            return
        if src == member:
            self._hint_label.setText("Sender and recipient must not be the same.")
            return
        self._read_src = src
        self._read_timeout = self._timeout_spin.value()
        self._hint_label.setText(hint)
        if switch == SWITCH_ALL:
            self._start_sequence(member, index, fmt)
            return
        self._alle_key = None  # single query: summary line of 'alle' no longer current
        self._info_label.setText(f"Request sent: recipient 0x{member:03x}, sender 0x{src:03x}, "
                                 f"index 0x{index:04x} ...")
        self._pending = (member, index, switch)
        self._bridge.send_read(member, index, fmt, switch, **self._read_extra())

    def _start_sequence(self, member: int, index: int, fmt: str) -> None:
        self._alle_key = (member, index)
        self._alle_results = {sw: None for _label, sw in SUMMARY_LABELS}
        self._seq = {"member": member, "index": index, "fmt": fmt,
                     "queue": list(SEQ_SWITCHES[1:]), "current": SEQ_SWITCHES[0]}
        self._info_label.setText(self._summary_text())
        self._apply_lock()
        self._send_step()

    def _send_step(self) -> None:
        seq = self._seq
        self._pending = (seq["member"], seq["index"], seq["current"])
        # Longer firmware timeout: GUI waits correspondingly longer.
        wait = SEQ_TIMEOUT_MS if self._read_timeout <= 1000 else max(SEQ_TIMEOUT_MS, self._read_timeout + 1000)
        self._seq_timer.start(wait)
        self._bridge.send_read(seq["member"], seq["index"], seq["fmt"], seq["current"], **self._read_extra())

    def _seq_advance(self) -> None:
        seq = self._seq
        if seq["queue"]:
            seq["current"] = seq["queue"].pop(0)
            self._send_step()
            return
        self._seq = None
        self._apply_lock()
        self._hint_label.setText("Query 'all' completed.")

    def _seq_abort(self, text: str) -> None:
        self._seq_timer.stop()
        self._seq = None
        self._hint_label.setText(text)
        self._apply_lock()

    def _on_seq_timeout(self) -> None:
        seq = self._seq
        if seq is None:
            return
        if self._alle_results.get(seq["current"]) is None:
            self._alle_results[seq["current"]] = NO_ANSWER
        self._info_label.setText(self._summary_text())
        self._seq_advance()

    def _summary_text(self) -> str:
        parts = []
        for label, sw in SUMMARY_LABELS:
            value = self._alle_results.get(sw)
            parts.append(f"{label}: {value if value is not None else '-'}")
        return " | ".join(parts)

    @staticmethod
    def _alle_text(status: str, value_text: str) -> str:
        if status == "ok":
            return value_text
        if status == "unsupported":
            return "not supported"
        if status == "timeout":
            return NO_ANSWER
        return STATUS_TEXT.get(status, status) or NO_ANSWER

    def _on_clear(self):
        self._table.setRowCount(0)

    def _on_read_result(self, payload: dict):
        member = _to_int(payload.get("member"))
        index = _to_int(payload.get("index"))
        raw = _to_int(payload.get("raw"))
        status = str(payload.get("status", ""))
        switch = _to_int(payload.get("switch"))
        if switch is None:
            switch = DEFAULT_SWITCH  # older firmware without field
        if index is None:
            return  # result without index cannot be assigned

        sig = self._db.signal_by_index(index)
        if sig is not None:
            name = sig.get("name", self._db.signal_name(index))
            type_name = sig.get("type") or DEFAULT_TYPE
        else:
            name = self._db.signal_name(index)
            type_name = DEFAULT_TYPE

        if status == "unsupported" and raw is None:
            raw = 0x8000
        if status == "busy":
            raw = None  # production firmware returns 0 without raw value; with busy show no value
        value_text = self._decode(type_name, raw, index) if status not in ("timeout", "busy") else "–"
        member_text = self._db.member_name(member) if member is not None else "–"
        status_text = STATUS_TEXT.get(status, status or "–")
        raw_hex = f"0x{raw:04x}" if raw is not None else "–"
        raw_dec = str(raw) if raw is not None else "–"
        sw_text = switch_label(switch)
        src = _to_int(payload.get("src"))
        src_text = f" from 0x{src:03x}" if src is not None and src != OWN_CAN_ID else ""
        if member is not None and status == "ok" and raw is not None:
            self._last_read[(member, index)] = f"{value_text} ({raw_hex})"

        # Response of an 'alle' sequence: update summary line, continue on current switch.
        if member is not None and (member, index) == self._alle_key and switch in self._alle_results:
            self._alle_results[switch] = self._alle_text(status, value_text)
            self._info_label.setText(self._summary_text())
            if self._seq is not None and switch == self._seq["current"]:
                self._seq_timer.stop()
                self._seq_advance()
        # Only the response to the current query (member, index, switch) fills the
        # "Last result" field; other switch responses only appear in the history.
        elif self._pending == (member, index, switch):
            self._info_label.setText(
                f"Name: {name}\n"
                f"Recipient: {member_text}{src_text}   Index: 0x{index:04x}   Switch: {sw_text or 'current'}\n"
                f"Type: {type_name}\n"
                f"Raw: {raw_hex} ({raw_dec})   Value: {value_text}\n"
                f"Status: {status_text}"
            )
        elif self._pending is not None:
            self._hint_label.setText(
                f"Response for index 0x{index:04x}, switch {sw_text or 'current'} only appears in the history "
                "(does not match the last query)."
            )

        self._add_history_row(
            [
                datetime.now().strftime("%H:%M:%S"),
                "Read" + src_text,
                member_text,
                f"0x{index:04x}",
                sw_text,
                name,
                type_name,
                raw_hex,
                raw_dec,
                value_text,
                "–",
                "–",
                status_text,
            ]
        )

    def _add_history_row(self, cells):
        self._table.insertRow(0)
        for col, text in enumerate(cells):
            self._table.setItem(0, col, QTableWidgetItem(str(text)))
        while self._table.rowCount() > MAX_HISTORY:
            self._table.removeRow(self._table.rowCount() - 1)
