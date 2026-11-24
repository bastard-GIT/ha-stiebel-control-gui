"""Evaluation GUI: check measurement data index by index against the Elster reference (offline, no MQTT).

Start: .venv\\Scripts\\python.exe -I gui\\auswerte_gui.py [--state datei.json] [--csv datei.csv ...] [--ref ElsterTable.json]
The reference is edited as a working copy; save only under a new file name.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

# With -I the script directory is not in sys.path: add the modules from gui/ explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
)

from data_model import Measurements
from reference_model import ReferenceError, ReferenceModel
from tab_analyse import AnalysePanel
from tab_index_browser import IndexBrowser
from type_guess import load_mkgit_table

SETTINGS_ORG = "mk1-debug"
SETTINGS_APP = "auswerte_gui"


def _load_mkgit() -> dict:
    try:
        return load_mkgit_table()
    except OSError:
        return {}


class AuswerteWindow(QMainWindow):
    def __init__(self, ref_path: str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Stiebel Evaluation")
        self.resize(1500, 900)
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.meas = Measurements()
        self.ref = ReferenceModel(ref_path)
        self.mkgit = _load_mkgit()

        self.browser = IndexBrowser(self.meas, self.ref, self.mkgit)
        self.analyse = AnalysePanel(self.meas, self.ref, self.mkgit)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.browser)
        split.addWidget(self.analyse)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        self.setCentralWidget(split)

        self.browser.indexSelected.connect(self.analyse.set_index)
        self.analyse.referenceChanged.connect(self._on_reference_changed)

        self._build_menu()
        self._update_status()

    # ---- Menu ----
    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        for text, slot in (
            ("Load measurements…", self.open_measurements),
            ("Load reference…", self.open_reference),
        ):
            act = QAction(text, self)
            act.triggered.connect(slot)
            m.addAction(act)
        self._act_save = QAction("Save reference as…", self)
        self._act_save.triggered.connect(self.save_reference)
        m.addAction(self._act_save)
        m.addSeparator()
        act = QAction("Export index list as CSV…", self)
        act.triggered.connect(self.export_list)
        m.addAction(act)
        m.addSeparator()
        act = QAction("Quit", self)
        act.triggered.connect(self.close)
        m.addAction(act)

    def _last_dir(self) -> str:
        return str(self._settings.value("last_dir", "", type=str))

    def _remember(self, path: str) -> None:
        self._settings.setValue("last_dir", os.path.dirname(path))

    # ---- Actions ----
    def open_measurements(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Load measurements", self._last_dir(),
            "Measurements (*.json *.csv *.jsonl);;All files (*)")
        for p in paths:
            self.load_measurement(p)

    def load_measurement(self, path: str) -> None:
        try:
            n = self.meas.load_any(path)
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Load failed", f"{os.path.basename(path)}: {exc}")
            return
        self._remember(path)
        self.browser.reload()
        self.analyse.set_models(self.meas, self.ref, self.mkgit)
        self._update_status(f"{os.path.basename(path)}: {n} entries loaded")

    def open_reference(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load reference", self._last_dir(), "Reference (*.json);;All files (*)")
        if path:
            self.load_reference(path)

    def load_reference(self, path: str) -> None:
        try:
            self.ref = ReferenceModel(path)
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Load failed", f"{os.path.basename(path)}: {exc}")
            return
        self._remember(path)
        self.browser.set_models(self.meas, self.ref, self.mkgit)
        self.analyse.set_models(self.meas, self.ref, self.mkgit)
        self._update_status(f"Reference {os.path.basename(path)} loaded")

    def save_reference(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save reference as", self._last_dir(), "Reference (*.json)")
        if not path:
            return
        try:
            self.ref.save_as(path)
        except (OSError, ReferenceError) as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self._remember(path)
        self._update_status(f"Reference saved: {os.path.basename(path)}")

    def export_list(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export index list", self._last_dir(), "CSV (*.csv)")
        if not path:
            return
        header = ["index", "name_ref", "name_mkgit", "status", "member", "min_max", "typ_abweichung"]
        table = self.browser._table
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, delimiter=";", lineterminator="\r\n")
            w.writerow(header)
            for row in range(table.rowCount()):
                if table.isRowHidden(row):
                    continue
                w.writerow([table.item(row, c).text() if table.item(row, c) else ""
                            for c in range(len(header))])
        self._update_status(f"Index list exported: {os.path.basename(path)}")

    # ---- State ----
    def _on_reference_changed(self) -> None:
        self.browser.refresh_status()
        self._update_status("Reference changed (not saved)")

    def _confirm_discard(self) -> bool:
        if not self.ref.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes",
            "The reference has unsaved changes. Discard?")
        return answer == QMessageBox.StandardButton.Yes

    def _update_status(self, text: str = "") -> None:
        parts = []
        if text:
            parts.append(text)
        parts.append(f"Measurements: {', '.join(self.meas.sources) or '-'} ({len(self.meas.indices)} indices)")
        parts.append(f"Reference: {self.ref.path.name}" + (" *changed*" if self.ref.dirty else ""))
        self.statusBar().showMessage("  |  ".join(parts))
        self._act_save.setEnabled(True)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="mk1-debug evaluation GUI")
    ap.add_argument("--state", help="auto_*_state.json")
    ap.add_argument("--csv", nargs="*", default=[], help="*_bekannt.csv / *_unbekannt.csv")
    ap.add_argument("--rx", help="auto_*_rx.jsonl for response times")
    ap.add_argument("--ref", help="ElsterTable.json (default: reference in repo)")
    args = ap.parse_args(argv[1:])

    app = QApplication(argv[:1])
    win = AuswerteWindow(args.ref)
    for p in args.csv:
        win.load_measurement(p)
    if args.state:
        win.load_measurement(args.state)
    if args.rx:
        win.load_measurement(args.rx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
