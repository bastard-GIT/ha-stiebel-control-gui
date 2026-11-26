"""Elster protocol for mk1-debug (no Qt, no paho).

Data source: the table ElsterTable.json generated from upstream bullitt186/ha-stiebel-control
(see default_reference_path; a saved path or ELSTER_REFERENCE_JSON can point elsewhere).
The private table (ELSTER_PRIVATE_JSON or ../elster_reference/ElsterTable.json) is only an
overlay for generated tables: it adds owner, names, poll, comment and bits (see ElsterDb).
Decoding rules as in KElsterTable.cpp (SetValueType, SetDoubleType).
"""
from __future__ import annotations

import json
import re
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import table_config
from elster_defaults import DEFAULT_CAN_MEMBERS, DEFAULT_OWN_CAN_ID, DEFAULT_TYPES

NOT_SUPPORTED_RAW = 0x8000  # "no value" -> "-255" (except et_double_val/et_triple_val)

SWITCH_LONG = (0xF7, 0xF8, 0xF9, 0xFA)   # data[2] in long layout (INTERFACES 8.1)
REQUEST_SWITCHES = (0xFA, 0xF8, 0xF9)    # valid switch values in request
SWITCH_LABELS = {0xF7: "f7", 0xF8: "min", 0xF9: "max"}

def _private_candidates() -> tuple[Path, Path]:
    """Private table locations next to this repo (checked in this order)."""
    root = Path(__file__).resolve().parents[1]
    return (root.parent / "elster_reference" / "ElsterTable.json",
            root.parent / "referenzen" / "elster_reference" / "ElsterTable.json")


def default_reference_path() -> Path:
    """Path of the table to start with. Order:
    1. environment variable ELSTER_REFERENCE_JSON
    2. saved path (table_config, only if the file exists)
    3. private table in ../elster_reference (two candidates, first existing wins)
    4. otherwise the first candidate (missing file: the GUI starts without a table)."""
    env = os.environ.get("ELSTER_REFERENCE_JSON")
    if env:
        return Path(env)
    saved = table_config.saved_table_path()
    if saved is not None:
        return saved
    candidates = _private_candidates()
    for cand in candidates:
        if cand.is_file():
            return cand
    return candidates[0]


def private_reference_path() -> Path | None:
    """Private overlay table: environment variable ELSTER_PRIVATE_JSON (then only that path),
    else the private candidates. None if no file exists."""
    env = os.environ.get("ELSTER_PRIVATE_JSON")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    for cand in _private_candidates():
        if cand.is_file():
            return cand
    return None


DEFAULT_JSON = default_reference_path()


def activate_table(path: str | Path) -> None:
    """Make path the standard table for this process and save it (table_config).

    Updates DEFAULT_JSON and clears the caches of decode_value, encode_value and is_writable.
    Already created ElsterDb instances keep their table: use ElsterDb.reload()."""
    global DEFAULT_JSON
    DEFAULT_JSON = Path(path)
    _lookup_tables.cache_clear()
    _type_table.cache_clear()
    table_config.save_table_path(DEFAULT_JSON)

# Bit fields: index -> [{"bit": n, "name": "...", "confirmed": bool}]. Built-in default (user knowledge
# from old ESPHome code); a signal of the reference JSON with field "bits" overrides it.
# BETRIEBS_STATUS (checked on display 2026-10-21): bit 0 compressor, bit 6 storage charge pump, bit 14 hot water (tap).
# Hot water 0x4041, heating 0x0041, emergency operation (auxiliary heater, compressor off) 0x1040. The heating symbol has no own bit here.
BUILTIN_BITS = {
    0x0176: [
        {"bit": 0, "name": "VERDICHTER", "confirmed": True},
        {"bit": 6, "name": "SPEICHERLADEPUMPE", "confirmed": True},
        {"bit": 12, "name": "ZUSATZHEIZUNG", "confirmed": True},
        {"bit": 14, "name": "WARMWASSER", "confirmed": True},
    ],
    # BETRIEBS_STATUS_WPLA (0x4eef, owner 0x480). Source kr0ner/OneESP32ToRuleThemAll yaml/wpl_a_base.yaml,
    # measured 0x0051 heating 2026-10-20. Bits 0, 4, 6 confirmed, others not yet checked on display.
    0x4EEF: [
        {"bit": 0, "name": "HEIZKREISPUMPE_1", "confirmed": True},
        {"bit": 1, "name": "HEIZKREISPUMPE_2", "confirmed": False},
        {"bit": 2, "name": "AUFHEIZPROGRAMM", "confirmed": False},
        {"bit": 3, "name": "NHZ_STUFEN_IN_BETRIEB", "confirmed": False},
        {"bit": 4, "name": "WP_IM_HEIZBETRIEB", "confirmed": True},
        {"bit": 5, "name": "WP_IM_WW_BETRIEB", "confirmed": False},
        {"bit": 6, "name": "VERDICHTER_IN_BETRIEB", "confirmed": True},
        {"bit": 7, "name": "SOMMERBETRIEB_AKTIV", "confirmed": False},
        {"bit": 8, "name": "KUEHLBETRIEB_AKTIV", "confirmed": False},
        {"bit": 9, "name": "ABTAUBETRIEB", "confirmed": False},
        {"bit": 10, "name": "SILENT_MODE_1", "confirmed": False},
        {"bit": 11, "name": "SILENT_MODE_2", "confirmed": False},
    ],
}

MSG_TYPE_NAMES = {
    0: "write",
    1: "read",
    2: "response",
    3: "ack",
    4: "write ack",
    5: "write respond",
    6: "system",
    7: "system respond",
}


def _hex(value) -> int:
    """'0x0112' / '0x180' / int -> int."""
    if isinstance(value, int):
        return value
    return int(str(value), 16)


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    """JSON object from path, or (None, short reason). Never raises for file or JSON errors."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found"
    except OSError as e:
        return None, f"cannot read: {e.strerror or type(e).__name__}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg}"
    except UnicodeDecodeError:
        return None, "invalid JSON: not UTF-8"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


OVERLAY_FIELDS = ("owner", "owner_name", "poll", "comment", "bits")


def _is_generated(table: dict) -> bool:
    """Generated upstream table: source is an object with 'repo'."""
    source = table.get("source")
    return isinstance(source, dict) and "repo" in source


class ElsterDb:
    """Signals, members, error and operating-mode lists of one table file.

    json_path None: default_reference_path(). Missing or broken file: empty table, see table_error.
    overlay None: table_config.overlay_enabled(). Overlay (see _apply_overlay) only for generated
    tables. Member, type and bit lookups in the module functions (is_writable, encode_value) still
    use the standard table DEFAULT_JSON; decode() uses this instance's own lists.
    """

    def __init__(self, json_path: str | None = None, *, overlay: bool | None = None,
                 private_json: str | Path | None = None):
        self._load(Path(json_path) if json_path else DEFAULT_JSON,
                   overlay=overlay, private_json=private_json)

    def reload(self, json_path: str | Path | None = None, *, overlay: bool | None = None,
               private_json: str | Path | None = None) -> None:
        """Load a table into this instance in place (other holders of the instance see the new table).
        json_path None: the standard table DEFAULT_JSON."""
        self._load(Path(json_path) if json_path else DEFAULT_JSON,
                   overlay=overlay, private_json=private_json)

    def _load(self, table_path: Path, *, overlay: bool | None, private_json) -> None:
        self.table_path: Path = table_path
        table, error = _read_json(self.table_path)
        if table is not None and "signals" not in table:
            table, error = None, "missing key 'signals'"
        self.table_error: str | None = error
        self.table_loaded: bool = error is None
        if table is None:
            table = {}  # empty table: no signals, built-in members and own ID

        self.members: list[dict] = list(table.get("can_members", DEFAULT_CAN_MEMBERS))
        self.error_list: list[dict] = table.get("error_list", [])
        self.betriebsart_list: list[dict] = table.get("betriebsart_list", [])
        try:
            self.own_can_id: int = _hex(table["frame_format"]["bus"]["own_can_id"])
        except (KeyError, TypeError, ValueError):
            self.own_can_id = DEFAULT_OWN_CAN_ID

        if overlay is None:
            overlay = table_config.overlay_enabled()
        self.overlay_applied: bool = False
        self.overlay_path: Path | None = None
        self.overlay_added: int = 0
        self.overlay_enriched: int = 0
        self.signals: list[dict] = table.get("signals", [])
        if table and overlay and _is_generated(table):
            self._apply_overlay(private_json)

        self._member_by_id: dict[int, dict] = {}
        for m in self.members:
            self._member_by_id.setdefault(_hex(m["can_id"]), m)

        self._signal_by_index: dict[int, dict] = {}
        self._bits: dict[int, list[dict]] = {k: list(v) for k, v in BUILTIN_BITS.items()}
        for s in self.signals:
            if s.get("sentinel"):  # INDEX_NOT_FOUND is not a real signal
                continue
            self._signal_by_index.setdefault(_hex(s["index"]), s)
            if s.get("bits"):
                self._bits[_hex(s["index"])] = list(s["bits"])

    def _apply_overlay(self, private_json) -> None:
        """Merge the private table into self.signals (copies, the upstream file is not changed).

        Upstream signals: missing (None) OVERLAY_FIELDS are filled from the private signal with the
        same index. Names and types stay upstream. Private-only signals are appended with their own
        name and type. can_members and types of the private table are not used. The private file
        is only read."""
        path = Path(private_json) if private_json is not None else private_reference_path()
        if path is None:
            return
        private, _ = _read_json(path)
        if private is None or not isinstance(private.get("signals"), list):
            return

        private_by_index: dict[int, dict] = {}
        for s in private["signals"]:
            if not isinstance(s, dict) or s.get("sentinel"):
                continue
            try:
                private_by_index.setdefault(_hex(s["index"]), s)
            except (KeyError, TypeError, ValueError):
                continue

        merged: list[dict] = []
        upstream_indices: set[int] = set()
        for s in self.signals:
            copy = dict(s)
            if not s.get("sentinel"):
                idx = _hex(s["index"])
                upstream_indices.add(idx)
                priv = private_by_index.get(idx)
                if priv is not None:
                    changed = False
                    for field in OVERLAY_FIELDS:
                        if priv.get(field) is not None and copy.get(field) is None:
                            copy[field] = priv[field]
                            changed = True
                    if changed:
                        self.overlay_enriched += 1
            merged.append(copy)

        for idx, priv in private_by_index.items():
            if idx not in upstream_indices:
                merged.append(dict(priv))
                self.overlay_added += 1

        self.signals = merged
        self.overlay_applied = True
        self.overlay_path = path

    def member_by_id(self, can_id: int) -> dict | None:
        return self._member_by_id.get(can_id)

    def member_name(self, can_id: int) -> str:
        m = self.member_by_id(can_id)
        return m["name"] if m else f"UNBEKANNT_0x{can_id:03x}"

    def signal_by_index(self, index: int) -> dict | None:
        return self._signal_by_index.get(index)

    def bits_for(self, index: int) -> list[dict] | None:
        """Bit definitions of the index (reference field "bits" or built-in default), else None."""
        return self._bits.get(index)

    def decode(self, index: int, raw: int, type_name: str | None = None) -> str:
        """Decode raw value: bit field text if the index has bits, else by type."""
        bits = self.bits_for(index)
        if bits:
            return decode_bits(raw, bits)
        if type_name is None:
            sig = self.signal_by_index(index)
            type_name = sig.get("type", "et_default") if sig else "et_default"
        return decode_value(type_name, raw, self.betriebsart_list, self.error_list)

    def signal_name(self, index: int) -> str:
        s = self.signal_by_index(index)
        return s["name"] if s else f"UNK_0x{index:04x}"

    def search_signals(self, text: str) -> list[dict]:
        needle = text.strip().lower()
        result = []
        for s in self.signals:
            if s.get("sentinel"):
                continue
            if needle in s["name"].lower() or needle in s["index"].lower():
                result.append(s)
        return result


def parse_frame(can_id: int, data: bytes) -> dict | None:
    """Split 7-byte response (section 2 / ElsterTable.README 'dekodieren')."""
    if len(data) < 7:
        return None
    d = bytes(data)
    msg_type = d[0] & 0x0F
    receiver = ((d[0] & 0xF0) << 3) | d[1]
    system = msg_type in (6, 7)  # system message: always short layout
    switch = None
    if not system and d[2] in SWITCH_LONG:
        switch = d[2]
        index = (d[3] << 8) | d[4]
        value = (d[5] << 8) | d[6]
    else:
        index = d[2]
        value = (d[3] << 8) | d[4]
    return {
        "sender": can_id,
        "receiver": receiver,
        "msg_type": msg_type,
        "msg_type_name": MSG_TYPE_NAMES[msg_type],
        "index": index,
        "value": value,
        "switch": switch,
        "system": system,
    }


def switch_label(switch: int | None) -> str:
    """Short display form of the switch byte: None/0xFA -> "", 0xF8 -> "min", 0xF9 -> "max", 0xF7 -> "f7"."""
    if switch is None or switch == 0xFA:
        return ""
    return SWITCH_LABELS.get(switch, f"0x{switch:02x}")


@lru_cache(maxsize=1)
def _lookup_tables() -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """betriebsart_list and error_list from the standard table (for decode_value).
    Table missing or unreadable: both empty (raw display)."""
    table, _ = _read_json(DEFAULT_JSON)
    if table is None:
        return (), ()
    return tuple(table.get("betriebsart_list", [])), tuple(table.get("error_list", []))


def decode_value(type_name: str, raw: int, betriebsart: list[dict] | None = None,
                 errors: list[dict] | None = None) -> str:
    """Raw value (uint16) as text per type rule (KElsterTable.cpp).

    betriebsart and errors: lists for et_betriebsart and et_err_nr / et_err_status. None: the
    standard table (DEFAULT_JSON). ElsterDb.decode passes its own lists."""
    if type_name == "et_double_val":
        return f"{raw:.3f}"
    if type_name == "et_triple_val":
        return f"{raw:.6f}"

    if raw == NOT_SUPPORTED_RAW:
        return "-255"

    raw &= 0xFFFF
    lo = raw & 0xFF
    hi = raw >> 8
    s16 = raw - 0x10000 if raw & 0x8000 else raw

    if type_name == "et_dec_val":
        return f"{s16 / 10.0:.1f}"
    if type_name == "et_cent_val":
        return f"{s16 / 100.0:.2f}"
    if type_name == "et_mil_val":
        return f"{s16 / 1000.0:.3f}"
    if type_name == "et_byte":
        return str(lo - 256 if lo & 0x80 else lo)
    if type_name in ("et_bool", "et_little_bool"):
        # Both byte orders count as on (like upstream 2.3.0/2.4.0 and production firmware)
        return {0x0001: "on", 0x0100: "on", 0x0000: "off"}.get(raw, "?")
    if type_name == "et_little_endian":
        return str(hi + 256 * lo)
    if type_name == "et_betriebsart":
        if betriebsart is None:
            betriebsart = _lookup_tables()[0]
        if lo == 0 and hi < len(betriebsart):
            return betriebsart[hi]["name"]
        return "?"
    if type_name == "et_hours":
        return str(raw)  # unsigned hours
    if type_name == "et_err_status":
        # live fault status: fault number (ErrorList) in the high byte, low byte unused
        if errors is None:
            errors = _lookup_tables()[1]
        for e in errors:
            if _hex(e["index"]) == hi:
                return e["name"]
        return "0" if hi == 0 else f"ERR {hi}"
    if type_name == "et_zeit":
        return f"{lo:02d}:{hi:02d}"
    if type_name == "et_datum":
        return f"{hi:02d}.{lo:02d}."
    if type_name == "et_time_domain":
        if raw & 0x8080:
            return "not used time domain"
        return (f"{hi // 4:02d}:{15 * (hi % 4):02d}-"
                f"{lo // 4:02d}:{15 * (lo % 4):02d}")
    if type_name == "et_dev_nr":
        return "--" if raw >= 0x80 else str(raw + 1)
    if type_name == "et_dev_id":
        return f"{hi}-{lo:02d}"
    if type_name == "et_err_nr":
        if errors is None:
            errors = _lookup_tables()[1]
        for e in errors:
            if _hex(e["index"]) == raw:
                return e["name"]
        return f"ERR {raw}"
    # et_default and unknown types
    return str(s16)


def set_bit_names(raw: int, bits: list[dict]) -> list[tuple[int, str]]:
    """Set bits as [(bit, name)], ascending. Bits without definition are named "b<n>"."""
    names = {int(b["bit"]): b["name"] for b in bits}
    raw &= 0xFFFF
    return [(n, names.get(n, f"b{n}")) for n in range(16) if raw & (1 << n)]


def decode_bits(raw: int, bits: list[dict]) -> str:
    """Bit field as text: "0x0041: VERDICHTER, SPEICHERLADEPUMPE". 0x8000 = not supported."""
    if raw == NOT_SUPPORTED_RAW:
        return "-255"
    on = ", ".join(name for _, name in set_bit_names(raw, bits))
    return f"0x{raw & 0xFFFF:04X}: {on}" if on else f"0x{raw & 0xFFFF:04X}: -"


OWN_CAN_ID = 0x680  # own ID (ESPCLIENT), forbidden as read target


def build_read_request(member_can_id: int, index: int,
                       db: ElsterDb | None = None, fmt: str = "auto",
                       switch: int = 0xFA) -> tuple[int, bytes]:
    """Read request per INTERFACES.md 7.1/8.1: (CAN ID 0x680, 7 bytes).

    d0 = ((id >> 3) & 0xF0) | 0x01, d1 = id & 0x7F (generic formula for any ID).
    fmt: "auto" (short if index <= 0xFF, not in SWITCH_LONG, switch == 0xFA),
    "short" (only index <= 0xFF; indices in SWITCH_LONG and switch != 0xFA are sent long),
    "long" (always [d0, d1, switch, hi, lo, 0, 0]).
    switch: 0xFA (default), 0xF8 (Min), 0xF9 (Max); otherwise ValueError.
    db is only present for compatibility and is ignored.
    """
    if not 0 <= member_can_id <= 0x7FF:
        raise ValueError(f"CAN ID outside 11 bit: 0x{member_can_id:x}")
    if member_can_id == OWN_CAN_ID:
        raise ValueError("Target 0x680 (own ID) is not allowed")
    if not 0 <= index <= 0xFFFF:
        raise ValueError(f"Index outside 16 bit: {index}")
    if fmt not in ("auto", "short", "long"):
        raise ValueError(f"unknown format: {fmt!r}")
    if fmt == "short" and index > 0xFF:
        raise ValueError(f"short format only for index <= 0xFF, was 0x{index:04x}")
    if switch not in REQUEST_SWITCHES:
        raise ValueError(f"invalid switch: {switch!r} (allowed: 0xFA, 0xF8, 0xF9)")

    d0 = ((member_can_id >> 3) & 0xF0) | 0x01
    d1 = member_can_id & 0x7F
    use_short = (fmt != "long" and index <= 0xFF and switch == 0xFA
                 and index not in SWITCH_LONG)
    if use_short:
        tail = [index, 0x00, 0x00, 0x00, 0x00]
    else:
        tail = [switch, index >> 8, index & 0xFF, 0x00, 0x00]
    return OWN_CAN_ID, bytes([d0, d1, *tail])


@lru_cache(maxsize=1)
def _type_table() -> dict[str, dict]:
    """types from the standard table, name -> entry. Table missing or without types: DEFAULT_TYPES."""
    table, _ = _read_json(DEFAULT_JSON)
    types = table.get("types") if table is not None else None
    if not types:
        types = DEFAULT_TYPES
    return {t["name"]: t for t in types if isinstance(t, dict) and "name" in t}


def is_writable(type_name: str) -> bool:
    """types[].writable of the standard reference; unknown type -> False."""
    t = _type_table().get(type_name)
    return bool(t and t.get("writable"))


def _parse_int(s: str) -> int:
    if not re.fullmatch(r"[+-]?\d+", s):
        raise ValueError(f"not an integer: {s!r}")
    return int(s)


def _parse_scaled(s: str, factor: int) -> int:
    """Decimal number (comma or point) * factor, truncated (toward zero), as uint16."""
    if not re.fullmatch(r"[+-]?(\d+([.,]\d*)?|[.,]\d+)", s):
        raise ValueError(f"not a decimal number: {s!r}")
    scaled = int(Decimal(s.replace(",", ".")) * factor)
    if not -32767 <= scaled <= 32767:
        raise ValueError(f"value outside +-32767 after scaling: {s!r}")
    return scaled & 0xFFFF


def _parse_onoff(s: str, on_raw: int) -> int:
    low = s.lower()
    if low == "on":
        return on_raw
    if low == "off":
        return 0
    raise ValueError(f"expected 'on' or 'off': {s!r}")


def _parse_hm(h: str, m: str) -> tuple[int, int]:
    hour, minute = int(h), int(m)
    if not (hour < 24 and minute < 60):
        raise ValueError(f"time outside 0-23:0-59: {h}:{m}")
    return hour, minute


def _encode_text(type_name: str, s: str) -> int:
    """Text of a writable type to uint16 (inverse of decode_value)."""
    if type_name == "et_default":
        n = _parse_int(s)
        if not -32767 <= n <= 65535:
            raise ValueError(f"outside -32767..65535: {s!r}")
        return n & 0xFFFF
    if type_name == "et_dec_val":
        return _parse_scaled(s, 10)
    if type_name == "et_cent_val":
        return _parse_scaled(s, 100)
    if type_name == "et_mil_val":
        return _parse_scaled(s, 1000)
    if type_name == "et_byte":
        n = _parse_int(s)
        if not 0 <= n <= 255:
            raise ValueError(f"outside 0..255: {s!r}")
        return n
    if type_name == "et_bool":
        return _parse_onoff(s, 0x0001)
    if type_name == "et_little_bool":
        return _parse_onoff(s, 0x0100)
    if type_name == "et_little_endian":
        n = _parse_int(s)
        if not 0 <= n <= 0xFFFF:
            raise ValueError(f"outside 0..65535: {s!r}")
        return ((n & 0xFF) << 8) | (n >> 8)  # swap bytes
    if type_name == "et_betriebsart":
        if not s:
            raise ValueError("empty operating-mode name")
        needle = s.lower()
        betriebsart, _ = _lookup_tables()
        for idx in range(len(betriebsart) - 1, -1, -1):  # from back to front
            if betriebsart[idx]["name"].lower().startswith(needle):
                return idx << 8
        raise ValueError(f"unknown operating mode: {s!r}")
    if type_name == "et_zeit":
        m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s)
        if not m:
            raise ValueError(f"expected 'H:M': {s!r}")
        hour, minute = _parse_hm(m.group(1), m.group(2))
        return (minute << 8) + hour
    if type_name == "et_datum":
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", s)
        if not m:
            raise ValueError(f"expected 'D.M.': {s!r}")
        tag, monat = int(m.group(1)), int(m.group(2))
        if not (1 <= tag <= 31 and 1 <= monat <= 12):
            raise ValueError(f"date outside day 1-31 / month 1-12: {s!r}")
        return (tag << 8) + monat
    if type_name == "et_time_domain":
        if not s:
            return 0x8080  # "not used time domain"
        m = re.fullmatch(r"(\d{1,2}):(\d{1,2})-(\d{1,2}):(\d{1,2})", s)
        if not m:
            raise ValueError(f"expected 'H:M-H:M': {s!r}")
        h1, m1 = _parse_hm(m.group(1), m.group(2))
        h2, m2 = _parse_hm(m.group(3), m.group(4))
        if h1 * 60 + m1 >= h2 * 60 + m2:
            raise ValueError(f"start must be before end: {s!r}")
        return ((4 * h1 + m1 // 15) << 8) + (4 * h2 + m2 // 15)
    raise ValueError(f"type not writable: {type_name}")


def encode_value(type_name: str, text: str) -> int:
    """Text to uint16 per type rule (KElsterTable.cpp / types[].encode).

    Text '0x....' is the raw value for every type (0..0xFFFF). Result 0x8000 is ValueError.
    """
    if type_name not in _type_table():
        raise ValueError(f"unknown type: {type_name!r}")
    s = text.strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", s):
        raw = int(s, 16)
    else:
        if not is_writable(type_name):
            raise ValueError(f"type not writable: {type_name}")
        raw = _encode_text(type_name, s)
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"raw value outside 16 bit: {text!r}")
    if raw == NOT_SUPPORTED_RAW:
        raise ValueError("raw value 0x8000 ('no value') is not writable")
    return raw


def build_write_request(member_can_id: int, index: int, value: int,
                        fmt: str = "auto") -> tuple[int, bytes]:
    """Write request: (CAN ID 0x680, 7 bytes). Counterpart to build_read_request.

    d0 = ((id >> 3) & 0xF0) | 0x00, d1 = id & 0x7F.
    Short (fmt "auto"/"short", index <= 0xFF, not 0xF7..0xFA): [d0, d1, idx, vh, vl, 0, 0].
    Long (fmt "long", index > 0xFF or 0xF7..0xFA): [d0, d1, 0xFA, ih, il, vh, vl].
    """
    if not 0 <= member_can_id <= 0x7FF:
        raise ValueError(f"CAN ID outside 11 bit: 0x{member_can_id:x}")
    if member_can_id == OWN_CAN_ID:
        raise ValueError("Target 0x680 (own ID) is not allowed")
    if not 0 <= index <= 0xFFFF:
        raise ValueError(f"Index outside 16 bit: {index}")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"value outside 16 bit: {value}")
    if fmt not in ("auto", "short", "long"):
        raise ValueError(f"unknown format: {fmt!r}")
    if fmt == "short" and index > 0xFF:
        raise ValueError(f"short format only for index <= 0xFF, was 0x{index:04x}")

    d0 = ((member_can_id >> 3) & 0xF0) | 0x00
    d1 = member_can_id & 0x7F
    use_short = fmt != "long" and index <= 0xFF and index not in SWITCH_LONG
    if use_short:
        data = [d0, d1, index, value >> 8, value & 0xFF, 0x00, 0x00]
    else:
        data = [d0, d1, 0xFA, index >> 8, index & 0xFF, value >> 8, value & 0xFF]
    return OWN_CAN_ID, bytes(data)
