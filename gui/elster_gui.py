"""Main window of the mk1-debug GUI (INTERFACES.md, section 5.3 elster_gui.py).

Start: .venv\\Scripts\\python.exe -I gui\\elster_gui.py
MQTT data (host, port, user, password) come from secrets.yaml in the repo root (or $STIEBEL_SECRETS); they are prefilled and editable in the connection dialog, are not saved and not logged.
"""

from __future__ import annotations

import os
import sys

# With -I the script directory is not in sys.path: add the modules from gui/ explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtGui import QAction
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
        self.setWindowTitle("Connect to MQTT broker")

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
        form.addRow("Broker host:", self._host)
        form.addRow("Port:", self._port)
        form.addRow("User:", self._user)
        form.addRow("Password:", self._password)

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

        self.setWindowTitle("Stiebel Bus Analysis GUI")
        self.resize(1200, 800)

        self.live_tab = LiveTab(db, bridge)
        self.query_tab = QueryTab(db, bridge)
        self.scanner_tab = ScannerTab(db, bridge)
        self.status_tab = StatusTab(bridge)

        self._tabs = QTabWidget()
        self._tabs.addTab(self.live_tab, "Live")
        self._tabs.addTab(self.query_tab, "Query")
        self._tabs.addTab(self.scanner_tab, "Scanner")
        self._tabs.addTab(self.status_tab, "Status")
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
        menu = self.menuBar().addMenu("&File")

        act_connect = QAction("Connect...", self)
        act_connect.triggered.connect(self._on_connect)
        menu.addAction(act_connect)

        act_disconnect = QAction("Disconnect", self)
        act_disconnect.triggered.connect(self._on_disconnect)
        menu.addAction(act_disconnect)
        menu.addSeparator()

        act_csv = QAction("Export capture (CSV)", self)
        act_csv.triggered.connect(self._on_export_csv)
        menu.addAction(act_csv)

        act_jsonl = QAction("Export capture (JSONL)", self)
        act_jsonl.triggered.connect(self._on_export_jsonl)
        menu.addAction(act_jsonl)

        act_load = QAction("Load offline log (JSONL)", self)
        act_load.triggered.connect(self._on_load_offline)
        menu.addAction(act_load)
        menu.addSeparator()

        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

    # ----- Connection ------------------------------------------------------

    def _on_connect(self) -> None:
        dlg = ConnectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            dlg.deleteLater()
            return
        host, port, user, password = dlg.values()
        dlg.deleteLater()  # discard password widget together with the dialog

        if not host:
            QMessageBox.warning(self, "Connect", "Please enter a broker host.")
            return
        self._bridge.connect_to(host, port, user, password)
        self.statusBar().showMessage(f"Connecting to {host}:{port} ...", 5000)

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
        self._lbl_conn.setText("MQTT: connected" if self._connected else "MQTT: disconnected")
        esp_text = f"ESP: {self._esp}"
        if self._fw in (FW_DEBUG, FW_PROD):
            esp_text += f" ({firmware_label(self._fw)})"
        self._lbl_esp.setText(esp_text)

    # ----- Export / offline log -------------------------------------------

    def _on_export_csv(self) -> None:
        rows = self.live_tab.rows()
        if not rows:
            QMessageBox.information(self, "Export", "No frames in capture.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export capture (CSV)", "mitschnitt.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            save_frames_csv(path, rows, self._db)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Export", f"Save failed:\n{exc}")
            return
        self.statusBar().showMessage(f"{len(rows)} frames exported: {path}", 5000)

    def _on_export_jsonl(self) -> None:
        rows = self.live_tab.rows()
        if not rows:
            QMessageBox.information(self, "Export", "No frames in capture.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export capture (JSONL)", "mitschnitt.jsonl", "JSONL (*.jsonl)"
        )
        if not path:
            return
        try:
            save_frames_jsonl(path, rows)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Export", f"Save failed:\n{exc}")
            return
        self.statusBar().showMessage(f"{len(rows)} frames exported: {path}", 5000)

    def _on_load_offline(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load offline log (JSONL)", "", "JSONL (*.jsonl);;All files (*)"
        )
        if not path:
            return
        self._load_offline_log(path)

    def _load_offline_log(self, path: str) -> int:
        """Reads a JSONL file and shows it in the live tab. Returns the number of frames."""
        try:
            rows = load_frames_jsonl(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Offline log", f"Load failed:\n{exc}")
            return 0
        self.live_tab.load_rows(rows)
        self._tabs.setCurrentWidget(self.live_tab)
        self.statusBar().showMessage(f"{len(rows)} frames loaded: {path}", 5000)
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
