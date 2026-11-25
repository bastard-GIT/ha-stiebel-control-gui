"""Small user configuration of the GUI (no Qt).

The file config.json lives in the user config folder (see config_dir). Keys:
  table_path     saved path of the Elster table to start with
  table_overlay  bool, add details from the private table to a generated table (default True)

Why not QSettings (used by i18n): this core module stays free of Qt, and the tests stay
deterministic: STIEBEL_GUI_CONFIG_DIR points them to a temporary folder.

All functions swallow file and JSON errors. A missing or broken config reads as empty.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

APP_DIR_NAME = "ha-stiebel-control-gui"
CONFIG_FILE_NAME = "config.json"


def config_dir() -> Path:
    """Folder of config.json: $STIEBEL_GUI_CONFIG_DIR, else %APPDATA% (Windows),
    else $XDG_CONFIG_HOME or ~/.config, each plus 'ha-stiebel-control-gui'."""
    env = os.environ.get("STIEBEL_GUI_CONFIG_DIR")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA")
    if sys.platform.startswith("win") and appdata:
        base = Path(appdata)
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_DIR_NAME


def _config_file() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def load() -> dict:
    """Config as dict. Missing, unreadable or non-object file: {}. Never raises."""
    try:
        data = json.loads(_config_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save(updates: dict) -> bool:
    """Merge updates into config.json (atomic replace, folder created). False on any error."""
    try:
        data = load()
        data.update(updates)
        folder = config_dir()
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _config_file())
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def saved_table_path() -> Path | None:
    """Saved table path, only if the file exists; else None."""
    value = load().get("table_path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def save_table_path(path: str | Path) -> bool:
    return save({"table_path": str(path)})


def overlay_enabled() -> bool:
    """table_overlay, default True."""
    value = load().get("table_overlay", True)
    return value if isinstance(value, bool) else True


def set_overlay_enabled(enabled: bool) -> bool:
    return save({"table_overlay": bool(enabled)})
