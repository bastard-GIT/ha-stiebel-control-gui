"""Main window of the mk1-debug GUI (INTERFACES.md, section 5.3 elster_gui.py).

Start: python -I main.py (in the repository root)
MQTT data (host, port, user, password) come from secrets.yaml in the repo root (or $STIEBEL_SECRETS); they are prefilled and editable in the connection dialog, are not saved and not logged.
"""

from __future__ import annotations

import os
import sys

# With -I the script directory is not in sys.path: add the modules from gui/ explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n
from i18n import _

# The language must be set before the tab modules are imported (module-level constants are translated at import).
# Only when started as a program: tests and imports always see English.
if __name__ == "__main__":
    i18n.init_from_settings()

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
)

from elster import ElsterDb
from export_io import load_frames_jsonl, save_frames_csv, save_frames_jsonl
from mqtt_secrets import load_mqtt_settings
from mqtt_client import FW_DEBUG, FW_PROD, FW_UNKNOWN, MqttBridge
from tab_live import LiveTab
from tab_query import QueryTab
from tab_scanner import ScannerTab
from tab_status import StatusTab, firmware_label

DEFAULT_HOST = os.environ.get("STIEBEL_MQTT_HOST", "localhost")
DEFAULT_PORT = 31883
DEFAULT_USER = os.environ.get("STIEBEL_MQTT_USER", "")


class ConnectDialog(QDialog):
    """Prompt for connection data. None of it is saved."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Connect to MQTT broker"))

        # Prefill from secrets.yaml in the repo root (or $STIEBEL_SECRETS), else fixed defaults
        host, port, user, password = load_mqtt_settings() or (DEFAULT_HOST, DEFAULT_PORT, DEFAULT_USER, "")
        self._host = QLineEdit(host)
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(port)
        self._user = QLineEdit(user)
        self._password = QLineEdit(password)
        self._password.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow(_("Broker host:"), self._host)
        form.addRow(_("Port:"), self._port)
        form.addRow(_("User:"), self._user)
        form.addRow(_("Password:"), self._password)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, int, str, str]:
        return (
            self._host.text().strip(),
            self._port.value(),
            self._user.text().strip(),
            self._password.text(),
        )


class MainWindow(QMainWindow):
    def __init__(self, db: ElsterDb, bridge: MqttBridge):
        super().__init__()
        self._db = db
        self._bridge = bridge
        self._connected = False
        self._esp = "unknown"
        self._fw = FW_UNKNOWN  # firmware kind from stiebel-debug/status: debug | prod | unknown

        self.setWindowTitle(_("Stiebel Bus Analysis GUI"))
        self.resize(1200, 800)

        self.live_tab = LiveTab(db, bridge)
        self.query_tab = QueryTab(db, bridge)
        self.scanner_tab = ScannerTab(db, bridge)
        self.status_tab = StatusTab(bridge)

        self._tabs = QTabWidget()
        self._tabs.addTab(self.live_tab, _("Live"))
        self._tabs.addTab(self.query_tab, _("Query"))
        self._tabs.addTab(self.scanner_tab, _("Scanner"))
        self._tabs.addTab(self.status_tab, _("Status"))
        self.setCentralWidget(self._tabs)

        self._build_menu()

        self._lbl_conn = QLabel()
        self._lbl_esp = QLabel()
        bar = QStatusBar()
        bar.addPermanentWidget(self._lbl_conn)
        bar.addPermanentWidget(self._lbl_esp)
        self.setStatusBar(bar)
        self._update_status_bar()

        bridge.connected.connect(self._on_connected)
        bridge.availability.connect(self._on_availability)
        bridge.firmware_kind.connect(self._on_firmware)

    # ----- Menu ------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu(_("&File"))

        act_connect = QAction(_("Connect..."), self)
        act_connect.triggered.connect(self._on_connect)
        menu.addAction(act_connect)

        act_disconnect = QAction(_("Disconnect"), self)
        act_disconnect.triggered.connect(self._on_disconnect)
        menu.addAction(act_disconnect)
        menu.addSeparator()

        act_csv = QAction(_("Export capture (CSV)"), self)
        act_csv.triggered.connect(self._on_export_csv)
        menu.addAction(act_csv)

        act_jsonl = QAction(_("Export capture (JSONL)"), self)
        act_jsonl.triggered.connect(self._on_export_jsonl)
        menu.addAction(act_jsonl)

        act_load = QAction(_("Load offline log (JSONL)"), self)
        act_load.triggered.connect(self._on_load_offline)
        menu.addAction(act_load)
        menu.addSeparator()

        act_quit = QAction(_("Quit"), self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

        lang_menu = self.menuBar().addMenu(_("&Language"))
        group = QActionGroup(self)
        group.setExclusive(True)
        for code in i18n.available_languages():
            act = QAction(i18n.LANGUAGE_NAMES.get(code, code), self, checkable=True)
            act.setChecked(code == i18n.current_language())
            act.triggered.connect(lambda _checked=False, c=code: self._on_language(c))
            group.addAction(act)
            lang_menu.addAction(act)

    def _on_language(self, code: str) -> None:
        """Save the choice; it applies at the next start (module constants are translated at import)."""
        if code == i18n.current_language():
            return
        i18n.save_language(code)
        QMessageBox.information(self, _("Language"), _("The language changes after restarting the application."))

    # ----- Connection ------------------------------------------------------

    def _on_connect(self) -> None:
        dlg = ConnectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            dlg.deleteLater()
            return
        host, port, user, password = dlg.values()
        dlg.deleteLater()  # discard password widget together with the dialog

        if not host:
            QMessageBox.warning(self, _("Connect"), _("Please enter a broker host."))
            return
        self._bridge.connect_to(host, port, user, password)
        self.statusBar().showMessage(_("Connecting to {host}:{port} ...").format(host=host, port=port), 5000)

    def _on_disconnect(self) -> None:
        self._bridge.disconnect_from()
        self._connected = False
        self._esp = "unknown"
        self._fw = FW_UNKNOWN
        self._update_status_bar()

    def _on_connected(self, ok: bool) -> None:
        self._connected = bool(ok)
        if not ok:
            self._esp = "unknown"
            self._fw = FW_UNKNOWN
        self._update_status_bar()

    def _on_availability(self, text: str) -> None:
        self._esp = text
        self._update_status_bar()

    def _on_firmware(self, kind: str) -> None:
        self._fw = kind
        self._update_status_bar()

    def _update_status_bar(self) -> None:
        self._lbl_conn.setText(_("MQTT: connected") if self._connected else _("MQTT: disconnected"))
        esp_text = _("ESP: {esp}").format(esp=self._esp)
        if self._fw in (FW_DEBUG, FW_PROD):
            esp_text += _(" ({label})").format(label=firmware_label(self._fw))
        self._lbl_esp.setText(esp_text)

    # ----- Export / offline log -------------------------------------------

    def _on_export_csv(self) -> None:
        rows = self.live_tab.rows()
        if not rows:
            QMessageBox.information(self, _("Export"), _("No frames in capture."))
            return
        path, _unused = QFileDialog.getSaveFileName(
            self, _("Export capture (CSV)"), "mitschnitt.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            save_frames_csv(path, rows, self._db)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, _("Export"), _("Save failed:\n{exc}").format(exc=exc))
            return
        self.statusBar().showMessage(_("{count} frames exported: {path}").format(count=len(rows), path=path), 5000)

    def _on_export_jsonl(self) -> None:
        rows = self.live_tab.rows()
        if not rows:
            QMessageBox.information(self, _("Export"), _("No frames in capture."))
            return
        path, _unused = QFileDialog.getSaveFileName(
            self, _("Export capture (JSONL)"), "mitschnitt.jsonl", "JSONL (*.jsonl)"
        )
        if not path:
            return
        try:
            save_frames_jsonl(path, rows)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, _("Export"), _("Save failed:\n{exc}").format(exc=exc))
            return
        self.statusBar().showMessage(_("{count} frames exported: {path}").format(count=len(rows), path=path), 5000)

    def _on_load_offline(self) -> None:
        path, _unused = QFileDialog.getOpenFileName(
            self, _("Load offline log (JSONL)"), "", _("JSONL (*.jsonl);;All files (*)")
        )
        if not path:
            return
        self._load_offline_log(path)

    def _load_offline_log(self, path: str) -> int:
        """Reads a JSONL file and shows it in the live tab. Returns the number of frames."""
        try:
            rows = load_frames_jsonl(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, _("Offline log"), _("Load failed:\n{exc}").format(exc=exc))
            return 0
        self.live_tab.load_rows(rows)
        self._tabs.setCurrentWidget(self.live_tab)
        self.statusBar().showMessage(_("{count} frames loaded: {path}").format(count=len(rows), path=path), 5000)
        return len(rows)

    # ----- Quit ------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._bridge.disconnect_from()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    db = ElsterDb()
    bridge = MqttBridge()
    win = MainWindow(db, bridge)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
