"""Production MQTT API for bus analysis: bus scan, member scan, raw read and raw write.

Qt-free, standard library only. Mirrors the firmware side from ha-stiebel-control
(bus_scan.h, can_raw.h). Topic-Wurzel ist PROD_ROOT ("heatingpump").

The module builds payloads and parses responses. It connects to nobody and
sends nothing. The member table is a copy of can_members.h from the fork
bastard-GIT/ha-stiebel-control (state mk1-2026.11.22, WPL-A names), without the PC entry.

All hex values in payloads and topics are lowercase. Invalid inputs raise
ValueError. Invalid responses return None.
"""

import json
import re
from typing import Any, Dict, Optional, Tuple

PROD_ROOT = "heatingpump"
PC_CAN_ID = 0x680
DEFAULT_MEMBER_INDEX = 0x000B

# (name, CAN ID) of all members of the fork table, PC excluded. Order as in the fork.
PROD_MEMBERS: Tuple[Tuple[str, int], ...] = (
    ("KESSEL", 0x180),
    ("ATEZ", 0x280),
    ("BEDIENMODUL_1", 0x300),
    ("BEDIENMODUL_2", 0x301),
    ("BEDIENMODUL_3", 0x302),
    ("BEDIENMODUL_4", 0x303),
    ("RAUMFERNFUEHLER", 0x400),
    ("RAUMFERNFUEHLER_2", 0x401),
    ("MANAGER", 0x480),
    ("HEIZMODUL", 0x500),
    ("BUSKOPPLER", 0x580),
    ("MISCHERMODUL_1", 0x600),
    ("MISCHERMODUL_3", 0x602),
    ("MISCHERMODUL_4", 0x603),
    ("FREMDGERAET", 0x700),
    ("DCF_MODUL", 0x780),
    ("WPUMPE", 0x514),  # WPL-A Heizmodul (HA_NAME_HEIZMODUL_WPL)
    ("SPEICHER", 0x201),  # WPL-A Speicher (HA_NAME_KESSEL_CANB)
    ("MISCHERMODUL_2", 0x601),
    ("FES", 0x100),
    ("OTHER", 0x000),
)

SWITCH_VALUE = 0xFA
SWITCH_MIN = 0xF8
SWITCH_MAX = 0xF9

TOPIC_SCAN_START = PROD_ROOT + "/scan/start"
TOPIC_SCAN_STOP = PROD_ROOT + "/scan/stop"
TOPIC_SCAN_STATUS = PROD_ROOT + "/scan/status"
TOPIC_SCAN_MEMBERS = PROD_ROOT + "/scan/members"
TOPIC_SCAN_WILDCARD = PROD_ROOT + "/scan/#"
TOPIC_RAW_READ = PROD_ROOT + "/raw/read"
TOPIC_RAW_WRITE = PROD_ROOT + "/raw/write"
TOPIC_RAW_RESULT = PROD_ROOT + "/raw/result"
TOPIC_CAN_DEBUG_SET = PROD_ROOT + "/can_debug/set"
TOPIC_CAN_DEBUG_STATE = PROD_ROOT + "/can_debug/state"
TOPIC_CAN_DEBUG_FRAMES = PROD_ROOT + "/can_debug/frames"

# Names that can never be a scan target (topics under scan/)
_RESERVED_SCAN_NAMES = ("status", "start", "stop", "members")

_NAME_BY_ID: Dict[int, str] = {can_id: name for name, can_id in PROD_MEMBERS}
_SWITCH_NAMES: Dict[int, str] = {SWITCH_VALUE: "fa", SWITCH_MIN: "f8", SWITCH_MAX: "f9"}
_SWITCH_BY_NAME: Dict[str, int] = {name: sw for sw, name in _SWITCH_NAMES.items()}
_SWITCH_SUFFIX: Dict[int, str] = {SWITCH_VALUE: "", SWITCH_MIN: "/f8", SWITCH_MAX: "/f9"}

_SCAN_RESULT_RE = re.compile(PROD_ROOT + r"/scan/([^/]+)/0x([0-9a-f]{4})(/f8|/f9)?")
_MEMBER_TOPIC_RE = re.compile(PROD_ROOT + r"/scan/members/0x([0-9a-f]{3})")
_HEX_RE = re.compile(r"0x([0-9a-f]+)")


# ---------------------------------------------------------------- Checks

def _is_int(value: Any) -> bool:
    """True for real integers. bool does not count."""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_int(value: Any, maximum: int, what: str, minimum: int = 0) -> None:
    """ValueError if value is not an integer in the range minimum..maximum."""
    if not _is_int(value) or not minimum <= value <= maximum:
        raise ValueError("%s out of range %d..%d: %r" % (what, minimum, maximum, value))


def _check_can_id(can_id: Any) -> None:
    """ValueError for the PC ID (0x680) and IDs outside 0x000..0x7FF."""
    if _is_int(can_id) and can_id == PC_CAN_ID:
        raise ValueError("PC ID 0x680 is not scannable and not readable")
    _check_int(can_id, 0x7FF, "CAN-ID")


def _check_switch(switch: Any) -> None:
    """ValueError for switch bytes other than 0xFA, 0xF8 and 0xF9."""
    if not _is_int(switch) or switch not in _SWITCH_NAMES:
        raise ValueError("Invalid switch byte: %r" % (switch,))


# ---------------------------------------------------------------- Names and switch

def member_name(can_id: int) -> Optional[str]:
    """Name from the member table, otherwise None (also for the PC ID)."""
    if not _is_int(can_id):
        return None
    return _NAME_BY_ID.get(can_id)


def member_topic_name(can_id: int) -> str:
    """Name for topics and scan start: table name, otherwise "0x%03x" (lowercase).

    ValueError for the PC ID and IDs outside 0x000..0x7FF.
    """
    _check_can_id(can_id)
    name = member_name(can_id)
    if name is not None:
        return name
    return "0x%03x" % can_id


def switch_name(switch: int) -> str:
    """"fa", "f8" or "f9". ValueError for other values."""
    _check_switch(switch)
    return _SWITCH_NAMES[switch]


def switch_suffix(switch: int) -> str:
    """Topic suffix: "" for fa, "/f8" and "/f9" for the limits. ValueError otherwise."""
    _check_switch(switch)
    return _SWITCH_SUFFIX[switch]


# ---------------------------------------------------------------- Payloads

def scan_start_payload(can_id: int, lo: Optional[int] = None, hi: Optional[int] = None,
                       switch: int = SWITCH_VALUE) -> str:
    """Payload for <root>/scan/start, e.g. "KESSEL 0x0000 0x00ff f8".

    First token is member_topic_name(can_id). lo and hi only together or not at all
    (without a range the firmware scans the whole table). Switch fa is omitted.
    """
    name = member_topic_name(can_id)
    _check_switch(switch)
    if (lo is None) != (hi is None):
        raise ValueError("specify lo and hi only together")
    parts = [name]
    if lo is not None:
        _check_int(lo, 0xFFFF, "lo")
        _check_int(hi, 0xFFFF, "hi")
        if lo > hi:
            raise ValueError("lo %d greater than hi %d" % (lo, hi))
        parts.append("0x%04x" % lo)
        parts.append("0x%04x" % hi)
    if switch != SWITCH_VALUE:
        parts.append(switch_name(switch))
    return " ".join(parts)


def members_payload(lo: int = 0, hi: int = 0x7FF, index: int = DEFAULT_MEMBER_INDEX) -> str:
    """Payload for <root>/scan/members, e.g. "0x000 0x7ff 0x000b"."""
    _check_int(lo, 0x7FF, "lo")
    _check_int(hi, 0x7FF, "hi")
    if lo > hi:
        raise ValueError("lo %d greater than hi %d" % (lo, hi))
    _check_int(index, 0xFFFF, "index")
    return "0x%03x 0x%03x 0x%04x" % (lo, hi, index)


def raw_read_payload(can_id: int, index: int, switch: int = SWITCH_VALUE) -> str:
    """Payload for <root>/raw/read, e.g. "0x180 0x0013" oder "0x180 0x0013 f8"."""
    _check_can_id(can_id)
    _check_int(index, 0xFFFF, "index")
    _check_switch(switch)
    payload = "0x%03x 0x%04x" % (can_id, index)
    if switch != SWITCH_VALUE:
        payload += " " + switch_name(switch)
    return payload


def raw_write_payload(can_id: int, index: int, value: int) -> str:
    """Payload for <root>/raw/write, e.g. "0x180 0x0013 0x0208"."""
    _check_can_id(can_id)
    _check_int(index, 0xFFFF, "index")
    _check_int(value, 0xFFFF, "value")
    return "0x%03x 0x%04x 0x%04x" % (can_id, index, value)


# ---------------------------------------------------------------- Parser

def _load_object(text: Any) -> Optional[Dict[str, Any]]:
    """JSON object from text, otherwise None (not a str, broken JSON, list, number)."""
    if not isinstance(text, str):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _hex_field(value: Any, maximum: int) -> Optional[int]:
    """Hex string "0x..." (lowercase) to int. None for wrong type, format or overflow."""
    if not isinstance(value, str):
        return None
    match = _HEX_RE.fullmatch(value)
    if match is None:
        return None
    number = int(match.group(1), 16)
    return number if number <= maximum else None


def parse_scan_status(text: Any) -> Optional[Dict[str, Any]]:
    """<root>/scan/status: JSON with state, mode, member, done, total, hits, seconds."""
    obj = _load_object(text)
    if obj is None:
        return None
    if obj.get("state") not in ("running", "done", "stopped"):
        return None
    if obj.get("mode") not in ("table", "range", "members"):
        return None
    member = obj.get("member")
    if not isinstance(member, str):
        return None
    result: Dict[str, Any] = {"state": obj["state"], "mode": obj["mode"], "member": member}
    for key in ("done", "total", "hits", "seconds"):
        if not _is_int(obj.get(key)):
            return None
        result[key] = obj[key]
    return result


def parse_scan_result_topic(topic: Any) -> Optional[Tuple[str, int, int]]:
    """<root>/scan/<name>/0x<idx>[/f8|/f9] -> (name, index, switch). Otherwise None.

    scan/status, start, stop, members and scan/members/0x<id> yield None.
    """
    if not isinstance(topic, str):
        return None
    match = _SCAN_RESULT_RE.fullmatch(topic)
    if match is None:
        return None
    name, index_hex, suffix = match.group(1), match.group(2), match.group(3) or ""
    if name in _RESERVED_SCAN_NAMES:
        return None
    return name, int(index_hex, 16), _switch_for_suffix(suffix)


def _switch_for_suffix(suffix: str) -> int:
    """Switch byte for topic suffix "", "/f8" or "/f9"."""
    for sw, text in _SWITCH_SUFFIX.items():
        if text == suffix:
            return sw
    raise ValueError("unknown suffix %r" % suffix)


def parse_scan_result(text: Any) -> Optional[Dict[str, Any]]:
    """<root>/scan/<name>/0x<idx>: {"name", "raw" (int), "value" (str)} or None."""
    obj = _load_object(text)
    if obj is None:
        return None
    name = obj.get("name")
    raw = obj.get("raw")
    value = obj.get("value")
    if not isinstance(name, str) or not _is_int(raw) or not isinstance(value, str):
        return None
    return {"name": name, "raw": raw, "value": value}


def parse_member_topic(topic: Any) -> Optional[int]:
    """<root>/scan/members/0x<id> -> CAN ID (int, at most 0x7FF). Otherwise None."""
    if not isinstance(topic, str):
        return None
    match = _MEMBER_TOPIC_RE.fullmatch(topic)
    if match is None:
        return None
    can_id = int(match.group(1), 16)
    return can_id if can_id <= 0x7FF else None


def parse_member_result(text: Any) -> Optional[Dict[str, int]]:
    """<root>/scan/members/0x<id>: {"index": "0x000b", "raw": <decimal>} -> {"index", "raw"}."""
    obj = _load_object(text)
    if obj is None:
        return None
    index = _hex_field(obj.get("index"), 0xFFFF)
    raw = obj.get("raw")
    if index is None or not _is_int(raw):
        return None
    return {"index": index, "raw": raw}


def _switch_field(value: Any) -> Optional[int]:
    """"fa", "f8" or "f9" to the switch byte. None otherwise."""
    if not isinstance(value, str):
        return None
    return _SWITCH_BY_NAME.get(value)


def parse_raw_result(text: Any) -> Optional[Dict[str, Any]]:
    """<root>/raw/result: response of a read or write operation, otherwise None.

    read:  {"op", "id", "index", "switch", "raw" (int oder None), "status"}
    write: {"op", "id", "index", "value", "status", "ack" (int oder None), "readback" (int oder None)}
    """
    obj = _load_object(text)
    if obj is None:
        return None
    op = obj.get("op")
    status = obj.get("status")
    if not isinstance(status, str):
        return None
    can_id = _hex_field(obj.get("id"), 0x7FF)
    index = _hex_field(obj.get("index"), 0xFFFF)
    if can_id is None or index is None:
        return None

    if op == "read":
        switch = _switch_field(obj.get("switch"))
        if switch is None:
            return None
        raw = None
        if obj.get("raw") is not None:
            raw = _hex_field(obj.get("raw"), 0xFFFF)
            if raw is None:
                return None
        return {"op": "read", "id": can_id, "index": index, "switch": switch, "raw": raw,
                "status": status}

    if op == "write":
        value = _hex_field(obj.get("value"), 0xFFFF)
        if value is None:
            return None
        ack = obj.get("ack")
        if ack is not None and not _is_int(ack):
            return None
        readback = None
        if obj.get("readback") is not None:
            readback = _hex_field(obj.get("readback"), 0xFFFF)
            if readback is None:
                return None
        return {"op": "write", "id": can_id, "index": index, "value": value, "status": status,
                "ack": ack, "readback": readback}

    return None
