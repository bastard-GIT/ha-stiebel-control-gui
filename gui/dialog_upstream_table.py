"""Dialog: generate the Elster table from the pinned upstream reference (see upstream_table.py).

On success the table is written, its path saved and activated (elster.activate_table); the main window
then tells the user to restart, because the tabs build their table-dependent lists once at start.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import elster
import table_config
import upstream_table
from i18n import _


def _signal_count(path: Path) -> int:
    """Number of entries in "signals" of a written table (0 if it cannot be read)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    signals = data.get("signals") if isinstance(data, dict) else None
    return len(signals) if isinstance(signals, list) else 0


class UpstreamTableDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Generate Elster table from upstream"))
        self.generated_path: Path | None = None
        self.model: str | None = None
        # Once the user edits the path (typed or browsed), the model choice no longer changes it.
        self._path_edited = False

        pin = upstream_table.pin_info()
        self._pin_label = QLabel(
            _("Repo: {repo}\nCommit: {commit}\nDate: {date}").format(
                repo=pin["repo"], commit=str(pin["commit"])[:7], date=pin["date"]
            )
        )

        self._model_combo = QComboBox()
        self._model_combo.addItems(upstream_table.available_models())

        self._path = QLineEdit()
        self._path.textEdited.connect(self._on_path_edited)
        self._browse = QPushButton(_("Browse…"))
        self._browse.clicked.connect(self._on_browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self._path, 1)
        path_row.addWidget(self._browse)

        self._overlay = QCheckBox(_("Add details from private table if found"))
        self._overlay.setChecked(table_config.overlay_enabled())

        form = QFormLayout()
        form.addRow(_("Pinned upstream:"), self._pin_label)
        form.addRow(_("Model:"), self._model_combo)
        form.addRow(_("Target file:"), path_row)
        form.addRow("", self._overlay)

        buttons = QDialogButtonBox()
        self._btn_generate = buttons.addButton(_("Generate"), QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_cancel = buttons.addButton(_("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._on_generate)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        self._set_suggested_path()

    # ----- Path handling ---------------------------------------------------

    def _suggested_path(self) -> str:
        return str(table_config.config_dir() / f"ElsterTable.{self._model_combo.currentText()}.json")

    def _set_suggested_path(self) -> None:
        self._path.setText(self._suggested_path())

    def _on_model_changed(self, _text: str) -> None:
        if not self._path_edited:
            self._set_suggested_path()

    def _on_path_edited(self, _text: str) -> None:
        self._path_edited = True

    def _on_browse(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, _("Save Elster table as"), self._path.text(), _("JSON (*.json);;All files (*)")
        )
        if path:
            self._path.setText(path)
            self._path_edited = True

    # ----- Generate --------------------------------------------------------

    def _on_generate(self) -> None:
        model = self._model_combo.currentText()
        path_text = self._path.text().strip()
        if not path_text:
            self._warn(_("Please choose a target file."))
            return
        try:
            target = Path(path_text)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = Path(upstream_table.write_table(model, target))
        except (upstream_table.UpstreamTableError, OSError) as exc:
            self._warn(_("Table generation failed:\n{error}").format(error=exc))
            return

        table_config.set_overlay_enabled(self._overlay.isChecked())
        elster.activate_table(written)
        self.generated_path = written
        self.model = model
        self._info(
            _("Elster table written: {count} signals.\n{path}").format(
                count=_signal_count(written), path=written
            )
        )
        self.accept()

    # ----- Messages (overridable, so tests do not block on a modal box) ----

    def _info(self, text: str) -> None:
        QMessageBox.information(self, _("Generate Elster table from upstream"), text)

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, _("Generate Elster table from upstream"), text)
