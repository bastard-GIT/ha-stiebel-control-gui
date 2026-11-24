"""MQTT bridge for the mk1 debug GUI (interface per INTERFACES.md, sections 5.2 and 8.3).

paho-mqtt 1.6.x: Client(...) without CallbackAPIVersion. paho runs via loop_start
in its own thread; all results go exclusively via Qt signals to the GUI.
The password is neither stored nor logged.

Production firmware (fw "prod"): commands and responses run via prod_api (topic root
heatingpump/). Debug firmware and unknown firmware stay with the prefix stiebel-debug/.
"""

import json
import time

import paho.mqtt.client as mqtt
from PySide6.QtCore import QObject, Signal

import prod_api

TOPIC_PREFIX = "stiebel-debug/"

# Topic root of the production firmware. All production topics derive from it.
PROD_ROOT = prod_api.PROD_ROOT

RX_TOPICS = (
    "rx",
    "read/result",
    "write/result",
    "scan/progress",
    "scan/result",
    "status",
    "availability",
)

# Availability: debug firmware (under the prefix) and production firmware (own topic).
PROD_AVAILABILITY_TOPIC = PROD_ROOT + "/status"
# Sniffer of the production firmware: frames (JSON array like stiebel-debug/rx, without field "u"),
# state (retained "on"/"off") and switch (payload "on"/"off", not JSON, no filter).
PROD_CAN_DEBUG_FRAMES = PROD_ROOT + "/can_debug/frames"
PROD_CAN_DEBUG_STATE = PROD_ROOT + "/can_debug/state"
PROD_CAN_DEBUG_SET = PROD_ROOT + "/can_debug/set"
AVAILABILITY_SOURCES = {
    "debug": TOPIC_PREFIX + "availability",
    "produktiv": PROD_AVAILABILITY_TOPIC,
}

# Firmware kind from stiebel-debug/status (field "fw"); if the field is missing, "prod" applies.
FW_DEBUG = "debug"
FW_PROD = "prod"
FW_UNKNOWN = "unknown"

SWITCH_DEFAULT = 0xFA            # current value (default)
CMD_SWITCH_VALUES = (0xFA, 0xF8, 0xF9)
CMD_FORMAT_VALUES = (None, "auto", "short", "long")


def _to_int(value):
    """Convert hex string ("0x180", "0X4E00", "4e00") or int to int. Otherwise ValueError/TypeError."""
    if isinstance(value, bool):
        raise TypeError("bool is not a hex value")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip(), 16)
    raise TypeError("unexpected type for hex value")


def _hex_id(value):
    return f"0x{value:03x}"


def _hex_index(value):
    return f"0x{value:04x}"


def _opt_int(value):
    """Like _to_int, but a missing field (None) stays None."""
    return None if value is None else _to_int(value)


def _prod_switch(switch):
    """Production: switch None counts as 0xFA; invalid values are checked by prod_api."""
    return prod_api.SWITCH_VALUE if switch is None else switch


class ClockSync:
    """Time estimate without ESP timestamp (fallback 'pc').

    For each frame: arrival (PC) - t/1000 = offset + latency. The smallest value since
    the last reset has the lowest latency and is used.
    Reset when a batch contains time values smaller than the largest t so far
    (ESP restart).
    """

    def __init__(self):
        self._offset: float | None = None
        self._last_t: int | None = None

    def update(self, arrival_unix_s: float, t_ms_list) -> None:
        if not t_ms_list:
            return
        t_min = min(t_ms_list)
        if self._last_t is not None and t_min < self._last_t:
            self._offset = None
            self._last_t = None
        candidate = min(arrival_unix_s - t / 1000 for t in t_ms_list)
        if self._offset is None or candidate < self._offset:
            self._offset = candidate
        self._last_t = max(t_ms_list) if self._last_t is None else max(self._last_t, max(t_ms_list))

    def estimate(self, t_ms: int) -> float | None:
        if self._offset is None:
            return None
        return t_ms / 1000 + self._offset


def _parse_switch(data: dict, default: int = SWITCH_DEFAULT) -> int:
    """Switch from payload; if the field is missing (older firmware), default (0xFA) applies."""
    raw = data.get("switch")
    if raw is None:
        return default
    value = _to_int(raw)
    if not 0 <= value <= 0xFF:
        raise ValueError(f"Switch outside 8 bit: {value}")
    return value


def _command_fields(fmt, switch) -> dict:
    """Optional payload fields format/switch for cmd/read and cmd/scan. Default values are omitted."""
    fields = {}
    if fmt not in CMD_FORMAT_VALUES:
        raise ValueError(f"unknown format: {fmt!r}")
    if fmt not in (None, "auto"):
        fields["format"] = fmt
    if switch is not None:
        if switch not in CMD_SWITCH_VALUES:
            raise ValueError(f"unknown switch: {switch!r}")
        if switch != SWITCH_DEFAULT:
            fields["switch"] = f"0x{switch:02x}"
    return fields


def _prod_check_format(fmt) -> None:
    """Production supports only format auto (default). short/long do not exist there."""
    if fmt in ("short", "long"):
        raise ValueError(f"format {fmt!r} not available in production mode")
    if fmt not in (None, "auto"):
        raise ValueError(f"unknown format: {fmt!r}")


class MqttBridge(QObject):
    connected = Signal(bool)
    rx_frames = Signal(list)         # list of dict {"t","id"(int),"data"(bytes),"tx"(bool)}
    read_result = Signal(dict)       # payload of read/result, member/index/raw/switch as int
    write_result = Signal(dict)      # write/result: member/index/value/ack/readback as int|None, status str|None
    scan_progress = Signal(dict)     # index/switch as int
    scan_result = Signal(dict)       # items with index/raw/switch as int
    status = Signal(dict)
    availability = Signal(str)       # combined: "offline" wins, otherwise "online"
    availability_source = Signal(str)  # "debug" or "produktiv", emitted immediately before availability
    firmware_kind = Signal(str)      # "debug" | "prod" | "unknown"; only on change
    can_debug_state = Signal(bool)   # production sniffer on/off (retained heatingpump/can_debug/state)
    member_result = Signal(dict)     # production member scan: {"id","index","raw"} as int

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client = None
        self._avail_by_source: dict[str, str | None] = {"debug": None, "produktiv": None}
        self._fw: str = FW_UNKNOWN
        self._clock = ClockSync()
        self._now = time.time  # arrival time; replaceable in tests
        # Production scan sent by this bridge (None = none). Retained leftovers from the broker
        # are discarded as long as no active scan exists.
        # Form: {"kind": "range"|"members", "name": str (lower case), "can_id": int|None,
        #        "switch": int, "running": bool}
        self._prod_scan: dict | None = None
        self.stale_ignored = 0           # discarded scan messages (counter)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect_to(self, host: str, port: int, user: str, password: str) -> None:
        self.disconnect_from()
        self._avail_by_source = {"debug": None, "produktiv": None}
        self._clock = ClockSync()
        self._prod_scan = None
        self._set_firmware_kind(FW_UNKNOWN)
        client = mqtt.Client(client_id="", clean_session=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        if user:
            client.username_pw_set(user, password)
        self._client = client
        try:
            client.connect(host, int(port), keepalive=30)
        except Exception:
            self._client = None
            self.connected.emit(False)
            return
        client.loop_start()

    def disconnect_from(self) -> None:
        self._prod_scan = None
        self._set_firmware_kind(FW_UNKNOWN)
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sending (qos 0)
    # ------------------------------------------------------------------
    def _publish(self, suffix: str, payload: dict) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.publish(TOPIC_PREFIX + suffix, json.dumps(payload), qos=0)
        except Exception:
            pass

    def _publish_raw(self, topic: str, text: str) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.publish(topic, text, qos=0)
        except Exception:
            pass

    def send_sniff(self, on: bool, ids: list[int], types: list[int]) -> None:
        """Production firmware (fw "prod"): heatingpump/can_debug/set "on"/"off". ids/types are omitted,
        the filter is applied client-side. Debug firmware: stiebel-debug/cmd/sniff.
        Firmware kind still unknown (no status received): send to both; each firmware ignores
        the other firmware's topic."""
        if self._fw in (FW_PROD, FW_UNKNOWN):
            self._publish_raw(PROD_CAN_DEBUG_SET, "on" if on else "off")
            if self._fw == FW_PROD:
                return
        self._publish("cmd/sniff", {
            "on": bool(on),
            "ids": [_hex_id(i) for i in ids],
            "types": [int(t) for t in types],
        })

    def send_read(self, member: int, index: int, fmt=None, switch=None, src=None, timeout_ms=None) -> None:
        """fmt: None|"auto"|"short"|"long"; switch: None|0xFA|0xF8|0xF9. Invalid values: ValueError.
        src: sender CAN ID (None/0x680 = default), timeout_ms: 100..10000 (None/1000 = default).

        Production: raw text on heatingpump/raw/read. fmt short/long and src (other than 0x680)
        do not exist there (ValueError). timeout_ms is omitted.
        """
        if self._fw == FW_PROD:
            _prod_check_format(fmt)
            if src is not None and src != prod_api.PC_CAN_ID:
                raise ValueError("sender (src) not available in production mode")
            self._publish_raw(prod_api.TOPIC_RAW_READ,
                              prod_api.raw_read_payload(member, index, _prod_switch(switch)))
            return
        payload = {
            "member": _hex_id(member),
            "index": _hex_index(index),
        }
        payload.update(_command_fields(fmt, switch))
        if src is not None and src != 0x680:
            if not 0 <= src <= 0x7FF or src == member:
                raise ValueError(f"invalid sender: {src!r}")
            payload["src"] = _hex_id(src)
        if timeout_ms is not None and timeout_ms != 1000:
            if not 100 <= timeout_ms <= 10000:
                raise ValueError(f"timeout outside 100..10000 ms: {timeout_ms!r}")
            payload["timeout_ms"] = int(timeout_ms)
        self._publish("cmd/read", payload)

    def send_write(self, member: int, index: int, value: int, fmt='auto') -> None:
        """Write command. Member 0..0x7FF (not 0x680), index/value 0..0xFFFF, otherwise ValueError.
        Production: raw text on heatingpump/raw/write; fmt must be 'auto'."""
        if not 0 <= member <= 0x7FF or member == 0x680:
            raise ValueError(f"invalid member: {member!r}")
        if not 0 <= index <= 0xFFFF:
            raise ValueError(f"index outside 16 bit: {index!r}")
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"value outside 16 bit: {value!r}")
        if self._fw == FW_PROD:
            if fmt != 'auto':
                raise ValueError(f"format {fmt!r} not available in production mode")
            self._publish_raw(prod_api.TOPIC_RAW_WRITE,
                              prod_api.raw_write_payload(member, index, value))
            return
        payload = {
            "member": _hex_id(member),
            "index": _hex_index(index),
            "value": _hex_index(value),
        }
        if fmt != 'auto':
            payload.update(_command_fields(fmt, None))
        self._publish("cmd/write", payload)

    def send_scan(self, member: int, from_idx: int, to_idx: int, delay_ms: int,
                  fmt=None, switch=None) -> None:
        """Like send_read; invalid fmt/switch values: ValueError.

        Production: scan/start with range; delay_ms is omitted. The scan is set active.
        """
        if self._fw == FW_PROD:
            _prod_check_format(fmt)
            sw = _prod_switch(switch)
            payload = prod_api.scan_start_payload(member, from_idx, to_idx, sw)
            self._prod_scan = {
                "kind": "range",
                "name": prod_api.member_topic_name(member).lower(),
                "can_id": member,
                "switch": sw,
                "running": False,
            }
            self._publish_raw(prod_api.TOPIC_SCAN_START, payload)
            return
        payload = {
            "member": _hex_id(member),
            "from": _hex_index(from_idx),
            "to": _hex_index(to_idx),
            "delay_ms": int(delay_ms),
        }
        payload.update(_command_fields(fmt, switch))
        self._publish("cmd/scan", payload)

    def send_member_scan(self, lo: int = 0, hi: int = 0x7FF, index: int = 0x000B) -> None:
        """Production member scan (scan/members). Only with production firmware, otherwise ValueError."""
        if self._fw != FW_PROD:
            raise ValueError("production firmware only")
        payload = prod_api.members_payload(lo, hi, index)
        self._prod_scan = {
            "kind": "members",
            "name": "members",
            "can_id": None,
            "switch": prod_api.SWITCH_VALUE,
            "running": False,
        }
        self._publish_raw(prod_api.TOPIC_SCAN_MEMBERS, payload)

    def send_stop(self) -> None:
        if self._fw == FW_PROD:
            self._publish_raw(prod_api.TOPIC_SCAN_STOP, "stop")
            return
        self._publish("cmd/stop", {})

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    def _combined_availability(self) -> str | None:
        """"offline" wins; "online" only if no source reports offline and at least one reports online.

        With debug firmware only stiebel-debug/availability counts (heatingpump/status is ignored).
        """
        if self._fw == FW_DEBUG:
            states = [self._avail_by_source["debug"]]
        else:
            states = list(self._avail_by_source.values())
        states = [s for s in states if s is not None]
        if "offline" in states:
            return "offline"
        if "online" in states:
            return "online"
        return None

    def _set_firmware_kind(self, kind: str) -> None:
        """Set firmware kind; signal only on change. On change to debug/prod, availability is re-evaluated.
        On change, a production scan is no longer valid and is cleared."""
        if kind == self._fw:
            return
        self._fw = kind
        self._prod_scan = None
        self.firmware_kind.emit(kind)
        if kind in (FW_DEBUG, FW_PROD):
            combined = self._combined_availability()
            if combined is not None:
                self.availability.emit(combined)

    def _handle_availability(self, source: str, text: str) -> None:
        value = text.strip()
        if value not in ("online", "offline"):
            return
        self._avail_by_source[source] = value  # also remember in case the firmware changes later
        if source == "produktiv" and self._fw == FW_DEBUG:
            return  # Debug firmware: heatingpump/status is ignored
        self.availability_source.emit(source)
        combined = self._combined_availability()
        if combined is not None:
            self.availability.emit(combined)

    # ------------------------------------------------------------------
    # paho callbacks (run in the paho thread)
    # ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            for topic in RX_TOPICS:
                try:
                    client.subscribe(TOPIC_PREFIX + topic, qos=0)
                except Exception:
                    pass
            for topic in (PROD_AVAILABILITY_TOPIC, PROD_CAN_DEBUG_FRAMES, PROD_CAN_DEBUG_STATE,
                          prod_api.TOPIC_RAW_RESULT, prod_api.TOPIC_SCAN_WILDCARD):
                try:
                    client.subscribe(topic, qos=0)
                except Exception:
                    pass
        self.connected.emit(rc == 0)

    def _on_disconnect(self, client, userdata, rc):
        self._prod_scan = None
        self._set_firmware_kind(FW_UNKNOWN)
        self.connected.emit(False)

    def _handle_can_debug_state(self, text: str) -> None:
        value = text.strip()
        if value in ("on", "off"):
            self.can_debug_state.emit(value == "on")

    def _handle_frames(self, data) -> None:
        """Frame batch (JSON array) from stiebel-debug/rx or heatingpump/can_debug/frames.

        If "u" is missing (production firmware), the time is estimated from t via ClockSync (ts_src "pc").
        """
        if not isinstance(data, list):
            return
        arrival = self._now()
        pending = []  # (item, frame) without time fields
        for item in data:
            try:
                pending.append((item, {
                    "t": int(item["t"]),
                    "id": _to_int(item["id"]),
                    "data": bytes.fromhex(item["d"]),
                    "tx": bool(item["tx"]),
                }))
            except Exception:
                continue
        # Fallback 'pc': only frames without ESP time feed the estimate.
        pc_t = [frame["t"] for item, frame in pending if item.get("u") is None]
        if pc_t:
            self._clock.update(arrival, pc_t)
        frames = []
        for item, frame in pending:
            try:
                if item.get("u") is not None:
                    frame["ts"] = round(float(item["u"]) / 1000, 3)
                    frame["ts_src"] = "esp"
                else:
                    frame["ts"] = round(self._clock.estimate(frame["t"]), 3)
                    frame["ts_src"] = "pc"
            except Exception:
                continue
            frames.append(frame)
        if frames:
            self.rx_frames.emit(frames)

    # ---- Production: scan state and responses --------------------------

    @staticmethod
    def _scan_matches(scan: dict, status: dict) -> bool:
        """Does the scan status (mode, member) match the sent scan?"""
        if scan["kind"] == "members":
            return status["mode"] == "members"
        return status["mode"] == "range" and status["member"].lower() == scan["name"]

    def _handle_raw_result(self, text: str) -> None:
        """heatingpump/raw/result: response to raw/read or raw/write. No active-scan check."""
        obj = prod_api.parse_raw_result(text)
        if obj is None:
            return
        if obj["op"] == "read":
            self.read_result.emit({
                "member": obj["id"],
                "index": obj["index"],
                "raw": obj["raw"] if obj["raw"] is not None else 0,
                "switch": obj["switch"],
                "src": prod_api.PC_CAN_ID,
                "status": obj["status"],
            })
        elif obj["op"] == "write":
            self.write_result.emit({
                "member": obj["id"],
                "index": obj["index"],
                "value": obj["value"],
                "status": obj["status"],
                "ack": obj["ack"],
                "readback": obj["readback"],
            })

    def _handle_scan_status(self, text: str) -> None:
        """heatingpump/scan/status. Without active scan or without matching running: discard."""
        status = prod_api.parse_scan_status(text)
        if status is None:
            return
        scan = self._prod_scan
        if scan is None or not self._scan_matches(scan, status):
            self.stale_ignored += 1
            return
        if not scan["running"]:
            if status["state"] != "running":
                self.stale_ignored += 1
                return
            scan["running"] = True
        out = dict(status)
        out["switch"] = scan["switch"]
        self.scan_progress.emit(out)
        if status["state"] != "running":
            self._prod_scan = None

    def _handle_scan_result(self, parsed, text: str) -> None:
        """heatingpump/scan/<name>/0x<idx>[/f8|/f9]: only for the running range scan."""
        name, index, switch = parsed
        result = prod_api.parse_scan_result(text)
        if result is None:
            return
        scan = self._prod_scan
        if (scan is None or not scan["running"] or scan["kind"] != "range"
                or name.lower() != scan["name"] or switch != scan["switch"]):
            self.stale_ignored += 1
            return
        self.scan_result.emit({
            "member": scan["can_id"],
            "switch": switch,
            "items": [{
                "index": index,
                "raw": result["raw"],
                "status": "ok",
                "switch": switch,
                "name": result["name"],
                "value": result["value"],
            }],
        })

    def _handle_member_result(self, can_id: int, text: str) -> None:
        """heatingpump/scan/members/0x<id>: only for the running member scan."""
        result = prod_api.parse_member_result(text)
        if result is None:
            return
        scan = self._prod_scan
        if scan is None or not scan["running"] or scan["kind"] != "members":
            self.stale_ignored += 1
            return
        self.member_result.emit({"id": can_id, "index": result["index"], "raw": result["raw"]})

    def _handle_prod_scan(self, topic: str, text: str) -> None:
        if topic == prod_api.TOPIC_SCAN_STATUS:
            self._handle_scan_status(text)
            return
        can_id = prod_api.parse_member_topic(topic)
        if can_id is not None:
            self._handle_member_result(can_id, text)
            return
        parsed = prod_api.parse_scan_result_topic(topic)
        if parsed is not None:
            self._handle_scan_result(parsed, text)

    def _on_message(self, client, userdata, msg):
        """Process message. Faulty messages are ignored.

        Testable without network: tests call this method directly with a fake object
        (attributes topic, payload).
        """
        try:
            topic = msg.topic
            if topic == PROD_AVAILABILITY_TOPIC:
                self._handle_availability("produktiv", msg.payload.decode("utf-8"))
                return
            if topic == PROD_CAN_DEBUG_STATE:
                self._handle_can_debug_state(msg.payload.decode("utf-8"))
                return
            if topic == PROD_CAN_DEBUG_FRAMES:
                self._handle_frames(json.loads(msg.payload.decode("utf-8")))
                return
            if topic == prod_api.TOPIC_RAW_RESULT:
                self._handle_raw_result(msg.payload.decode("utf-8"))
                return
            if self._fw == FW_PROD and topic.startswith(PROD_ROOT + "/scan/"):
                self._handle_prod_scan(topic, msg.payload.decode("utf-8"))
                return
            if not topic.startswith(TOPIC_PREFIX):
                return
            suffix = topic[len(TOPIC_PREFIX):]
            if suffix not in RX_TOPICS:
                return
            text = msg.payload.decode("utf-8")
            if suffix == "availability":
                self._handle_availability("debug", text)
                return

            data = json.loads(text)

            if suffix == "rx":
                self._handle_frames(data)

            elif suffix == "read/result":
                if not isinstance(data, dict):
                    return
                out = dict(data)
                out["member"] = _to_int(data["member"])
                out["index"] = _to_int(data["index"])
                out["raw"] = _to_int(data["raw"])
                out["switch"] = _parse_switch(data)
                out["src"] = _to_int(data.get("src", "0x680"))  # missing = 0x680
                self.read_result.emit(out)

            elif suffix == "write/result":
                if not isinstance(data, dict):
                    return
                self.write_result.emit({
                    "member": _opt_int(data.get("member")),
                    "index": _opt_int(data.get("index")),
                    "value": _opt_int(data.get("value")),
                    "status": data.get("status"),
                    "ack": _opt_int(data.get("ack")),
                    "readback": _opt_int(data.get("readback")),
                })

            elif suffix == "scan/progress":
                if not isinstance(data, dict):
                    return
                out = dict(data)
                out["index"] = _to_int(data["index"])
                out["switch"] = _parse_switch(data)
                self.scan_progress.emit(out)

            elif suffix == "scan/result":
                if not isinstance(data, dict):
                    return
                out = dict(data)
                out["member"] = _to_int(data["member"])
                top_switch = _parse_switch(data)
                out["switch"] = top_switch
                items = []
                for item in data.get("items", []):
                    try:
                        entry = dict(item)
                        entry["index"] = _to_int(item["index"])
                        entry["raw"] = _to_int(item["raw"])
                        entry["switch"] = _parse_switch(item, default=top_switch)
                        items.append(entry)
                    except Exception:
                        continue
                out["items"] = items
                self.scan_result.emit(out)

            elif suffix == "status":
                if not isinstance(data, dict):
                    return
                self._set_firmware_kind(FW_DEBUG if data.get("fw") == FW_DEBUG else FW_PROD)
                self.status.emit(data)
        except Exception:
            # Faulty message (not UTF-8, not JSON, wrong fields): ignore.
            return
