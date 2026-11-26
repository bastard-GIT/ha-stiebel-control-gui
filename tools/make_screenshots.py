"""Screenshots of the main GUI for docs/screenshots/ (offscreen, no broker, no secrets).

Start from the repository root:
    .venv/Scripts/python.exe -I tools/make_screenshots.py          (English, docs/screenshots/)
    .venv/Scripts/python.exe -I tools/make_screenshots.py --lang de (German, docs/screenshots/de/)

No MQTT connection: MqttBridge.connect_to() is never called. The demo data goes through
bridge._on_message() (the same hook the tests use), so the same parsers and signals fill the tabs
as with a broker. The connected flag is set by hand for the status display only.
The Elster table is generated from the pinned upstream copy (wpla7) into a temp folder, without the
private overlay. Config and secrets are redirected to non-existing or relative paths.
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui"
LANG = sys.argv[sys.argv.index("--lang") + 1] if "--lang" in sys.argv else "en"
OUT = ROOT / "docs" / "screenshots" if LANG == "en" else ROOT / "docs" / "screenshots" / LANG

# Environment first: elster reads the table path at import time.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Offscreen has no font database of its own: without this, all text is drawn as boxes.
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "Fonts"))
os.environ["STIEBEL_GUI_LANG"] = LANG
os.environ["STIEBEL_GUI_CONFIG_DIR"] = "demo-config"      # relative: no user path in the dialog
os.environ["STIEBEL_SECRETS"] = "none-secrets.yaml"       # does not exist: secrets.yaml is never read
os.environ["ELSTER_PRIVATE_JSON"] = "none-private.json"   # does not exist: no private overlay

TMP = Path(tempfile.mkdtemp(prefix="stiebel-shots-"))
os.chdir(TMP)
sys.path.insert(0, str(GUI))

import i18n  # noqa: E402

if not i18n.set_language(LANG):
    sys.exit(f"unknown language: {LANG}")

import upstream_table  # noqa: E402

TABLE = upstream_table.write_table("wpla7", TMP / "ElsterTable.wpla7.json")
os.environ["ELSTER_REFERENCE_JSON"] = str(TABLE)

from PySide6.QtWidgets import QApplication  # noqa: E402

import elster  # noqa: E402
import elster_gui  # noqa: E402
from dialog_upstream_table import UpstreamTableDialog  # noqa: E402
from mqtt_client import MqttBridge  # noqa: E402

WIDTH, HEIGHT = 1280, 800
MANAGER, KESSEL, HEIZMODUL = 0x480, 0x180, 0x500
OWN_ID = 0x680
rnd = random.Random(20261126)


class FakeMsg:
    """Minimal stand-in for paho's MQTTMessage (topic and payload only)."""

    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


def feed(bridge: MqttBridge, topic: str, payload) -> None:
    """Deliver one message to the bridge as if it came from the broker."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    bridge._on_message(None, None, FakeMsg(topic, text.encode("utf-8")))


def elster_response(sender: int, receiver: int, index: int, value: int, switch: int = 0xFA) -> bytes:
    """7-byte response in the long layout (msg_type 2), inverse of elster.parse_frame."""
    d0 = ((receiver >> 3) & 0xF0) | 2
    d1 = receiver & 0x7F
    return bytes([d0, d1, switch, index >> 8, index & 0xFF, (value >> 8) & 0xFF, value & 0xFF])


def live_frames(t0: int, cycles: int) -> list[dict]:
    """Read requests from the PC (tx) and their responses (rx) for a few temperatures and the
    operating-status bits. Times in ms, no ESP timestamp (clock estimate on the PC side)."""
    # (member, index, mean value, spread) in degrees; the status word is a bit field.
    signals = [
        (HEIZMODUL, 0x000C, 4.0, 1.5),    # AUSSENTEMP
        (HEIZMODUL, 0x000F, 34.0, 1.2),   # VORLAUFISTTEMP
        (HEIZMODUL, 0x0016, 29.0, 0.8),   # RUECKLAUFISTTEMP
        (KESSEL, 0x000E, 47.0, 1.0),      # SPEICHERISTTEMP
    ]
    frames = []
    t = t0
    for _ in range(cycles):
        for member, index, mean, spread in signals:
            _, req = elster.build_read_request(member, index)
            frames.append({"t": t, "id": f"0x{OWN_ID:03x}", "d": req.hex(), "tx": True})
            t += 18
            value = round((mean + rnd.uniform(-spread, spread)) * 10) & 0xFFFF
            resp = elster_response(member, OWN_ID, index, value)
            frames.append({"t": t, "id": f"0x{member:03x}", "d": resp.hex(), "tx": False})
            t += 37
        # Operating status of the manager (bit field 0x4EEF, heating mode)
        _, req = elster.build_read_request(MANAGER, 0x4EEF)
        frames.append({"t": t, "id": f"0x{OWN_ID:03x}", "d": req.hex(), "tx": True})
        t += 15
        resp = elster_response(MANAGER, OWN_ID, 0x4EEF, 0x0051)
        frames.append({"t": t, "id": f"0x{MANAGER:03x}", "d": resp.hex(), "tx": False})
        t += 120
    return frames


def send_status(bridge: MqttBridge, rx: int, tx: int) -> None:
    feed(bridge, "stiebel-debug/status", {
        "fw": "prod", "rx": rx, "tx": tx, "rx_fill": 3, "rx_cap": 256, "overflow": 0,
        "twai_err": 0, "bus_off": False, "heap": 141320, "ntp": True,
    })


def run_query(win, member_label: str, member: int, index: int, raw_text, status: str) -> None:
    """Fill the query tab, send (no-op without broker), deliver the raw/result the firmware would send."""
    tab = win.query_tab
    tab._member_combo.setCurrentText(member_label)
    tab._index_edit.setText(f"0x{index:04X}")
    tab._on_query()
    feed(win._bridge, "heatingpump/raw/result", {
        "op": "read", "id": f"0x{member:03x}", "index": f"0x{index:04x}", "switch": "fa",
        "raw": raw_text, "status": status,
    })


def run_range_scan(win, db, member: int, lo: int, hi: int) -> None:
    """Range scan of one member (production topics). The tab starts the scan, the topics answer.
    Each hit is delivered once the progress has passed its index."""
    tab = win.scanner_tab
    tab._member.setCurrentIndex(tab._member.findData(member))
    tab._from.setText(f"0x{lo:04x}")
    tab._to.setText(f"0x{hi:04x}")
    tab._on_start()
    name = "manager"
    total = hi - lo + 1
    hits = [i for i in range(lo, hi + 1) if db.signal_by_index(i) is not None]
    if 0x4EEF in range(lo, hi + 1) and 0x4EEF not in hits:
        hits.append(0x4EEF)
    sent: set[int] = set()
    for done in range(0, total + 1, max(1, total // 8)):
        for idx in hits:
            if idx - lo < done and idx not in sent:
                raw = 0x0051 if idx == 0x4EEF else rnd.randint(0x0100, 0x01F0)
                feed(win._bridge, f"heatingpump/scan/{name}/0x{idx:04x}", {
                    "name": db.signal_name(idx), "raw": raw, "value": db.decode(idx, raw),
                })
                sent.add(idx)
        feed(win._bridge, "heatingpump/scan/status", {
            "state": "running", "mode": "range", "member": name,
            "done": min(done, total), "total": total, "hits": len(sent), "seconds": done // 4,
        })
    feed(win._bridge, "heatingpump/scan/status", {
        "state": "done", "mode": "range", "member": name,
        "done": total, "total": total, "hits": len(sent), "seconds": total // 4,
    })


def run_member_search(win, lo: int, hi: int) -> None:
    """Member search (production only): which CAN IDs answer on the bus."""
    tab = win.scanner_tab
    tab._mem_from.setText(f"0x{lo:03x}")
    tab._mem_to.setText(f"0x{hi:03x}")
    tab._mem_index.setText("0x000b")
    tab._on_member_start()
    total = hi - lo + 1
    found = [0x180, 0x201, 0x280, 0x300, 0x480, 0x500, 0x514, 0x580, 0x600, 0x601, 0x602]
    sent: set[int] = set()
    for done in range(0, total + 1, 128):
        for can_id in found:
            if can_id - lo < done and can_id not in sent:
                feed(win._bridge, f"heatingpump/scan/members/0x{can_id:03x}",
                     {"index": "0x000b", "raw": rnd.randint(1, 60000)})
                sent.add(can_id)
        feed(win._bridge, "heatingpump/scan/status", {
            "state": "running", "mode": "members", "member": "members",
            "done": min(done, total), "total": total, "hits": len(sent), "seconds": done // 16,
        })
    feed(win._bridge, "heatingpump/scan/status", {
        "state": "done", "mode": "members", "member": "members",
        "done": total, "total": total, "hits": len(sent), "seconds": total // 16,
    })


def feed_demo(win) -> None:
    bridge = win._bridge
    db = win._db
    # Firmware and availability first: query and scanner need the production firmware kind.
    send_status(bridge, 184213, 2290)
    time.sleep(1.5)
    send_status(bridge, 184213 + 240, 2290 + 4)
    feed(bridge, "heatingpump/status", "online")
    bridge.connected.emit(True)  # display only, no broker connection
    feed(bridge, "heatingpump/can_debug/state", "on")

    # Live: frames in batches, then one flush of the batch timer
    feed(bridge, "heatingpump/can_debug/frames", live_frames(t0=100000, cycles=10))
    win.live_tab._flush()

    # Query: one timeout and three reads; the last read stays in "Last result"
    run_query(win, "HEIZMODUL (0x500)", HEIZMODUL, 0x0004, None, "timeout")
    run_query(win, "KESSEL (0x180)", KESSEL, 0x000E, "0x01d6", "ok")
    run_query(win, "HEIZMODUL (0x500)", HEIZMODUL, 0x000F, "0x0151", "ok")
    run_query(win, "MANAGER (0x480)", MANAGER, 0x4EEF, "0x0051", "ok")

    # Scanner: range scan of the manager, then member search
    run_range_scan(win, db, MANAGER, 0x4EE0, 0x4EF7)
    run_member_search(win, 0x180, 0x5FF)


def grab(app, widget, name: str) -> None:
    app.processEvents()
    path = OUT / name
    widget.grab().save(str(path), "PNG")
    print(f"{path.relative_to(ROOT).as_posix()} {path.stat().st_size} bytes")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    OUT.mkdir(parents=True, exist_ok=True)

    db = elster.ElsterDb(str(TABLE), overlay=False)
    bridge = MqttBridge()
    win = elster_gui.MainWindow(db, bridge)
    win.setFixedSize(WIDTH, HEIGHT)
    win.show()
    feed_demo(win)

    for index, name in enumerate(("01-live.png", "02-query.png", "03-scanner.png", "04-status.png")):
        win._tabs.setCurrentIndex(index)
        grab(app, win, name)

    dlg = UpstreamTableDialog(win)
    dlg._model_combo.setCurrentText("wpla7")
    dlg.resize(720, 230)
    dlg.show()
    grab(app, dlg, "05-dialog-generate-table.png")
    dlg.close()

    # Startup without a table: status bar note, the same live frames shown raw (hex)
    db_missing = elster.ElsterDb(str(TMP / "ElsterTable.missing.json"), overlay=False)
    bridge2 = MqttBridge()
    win2 = elster_gui.MainWindow(db_missing, bridge2)
    win2.setFixedSize(WIDTH, HEIGHT)
    win2.show()
    send_status(bridge2, 184213, 2290)
    feed(bridge2, "heatingpump/status", "online")
    bridge2.connected.emit(True)  # display only, no broker connection
    feed(bridge2, "heatingpump/can_debug/state", "on")
    feed(bridge2, "heatingpump/can_debug/frames", live_frames(t0=100000, cycles=10))
    win2.live_tab._flush()
    win2._tabs.setCurrentIndex(0)
    grab(app, win2, "06-start-ohne-tabelle.png")
    win2.close()

    win.close()
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
