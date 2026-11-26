# mk1-debug – Interfaces (binding for all subtasks)

Each subtask writes ONLY the files assigned to it. Keep the names, signatures and JSON formats below exactly. Do not write credentials to files. Plan: `../plan/mk1-debug-plan.md` (in the original stiebel-debug repo, not part of this repo).

Python: `<project folder>\.venv\Scripts\python.exe` (always start with `-I` when reading data from foreign sources). Reference table: `<project folder>\referenzen\elster_reference\ElsterTable.json` (reading aid: `ElsterTable.README.md` in the same folder).

## 1. MQTT topics (prefix `stiebel-debug/`)

This table applies to the debug firmware (prefix `stiebel-debug/`). The production firmware has different topics, see section 13.

| Topic | Direction | Payload (JSON) |
|---|---|---|
| `cmd/sniff` | GUI→ESP | `{"on":true,"ids":["0x180"],"types":[2]}` – empty lists = all |
| `cmd/read` | GUI→ESP | `{"member":"0x180","index":"0x4ef0"}`, optional `format`, `switch`, `src`, `timeout_ms` (sections 7.2, 8.3, 12) |
| `cmd/write` | GUI→ESP | `{"member":"0x201","index":"0x0013","value":"0x0208","format":"auto"}` – debug firmware only (section 10) |
| `cmd/scan` | GUI→ESP | `{"member":"0x500","from":"0x4e00","to":"0x4fff","delay_ms":200}`, optional `format`, `switch` |
| `cmd/probe` | GUI→ESP | `{"index":"0x000b","from_id":"0x000","to_id":"0x7ff","delay_ms":100}` (section 7.2) |
| `cmd/stop` | GUI→ESP | `{}` – aborts scan, probe, read and write |
| `rx` | ESP→GUI | `[{"t":123456,"id":"0x180","d":"d200fa01120200","tx":false}]`, optional `"u"` (Unix ms, section 11) |
| `read/result` | ESP→GUI | `{"member":"0x180","index":"0x4ef0","switch":"0xfa","raw":"0x03e8","status":"ok"}`, with `src` ≠ 0x680 additionally `"src"` |
| `write/result` | ESP→GUI | `{"member":"0x201","index":"0x0013","value":"0x0208","status":"verified","ack":4,"readback":"0x0208"}` (section 10.5) |
| `scan/progress` | ESP→GUI | `{"member":"0x500","format":"auto","index":"0x4e40","switch":"0xfa","done":64,"total":512,"state":"running"}` |
| `scan/result` | ESP→GUI | `{"member":"0x500","format":"auto","switch":"0xfa","items":[{"index":"0x4e40","raw":"0x0123","status":"ok"}]}` |
| `probe/progress` | ESP→GUI | `{"index":"0x000b","id":"0x1a0","done":416,"total":2047,"state":"running"}` |
| `probe/result` | ESP→GUI | `{"index":"0x000b","items":[{"id":"0x180","raw":"0x8000","status":"unsupported"}]}` |
| `status` | ESP→GUI | `{"fw":"debug","rx":0,"tx":0,"rx_fill":0,"rx_cap":4096,"overflow":0,"twai_err":0,"bus_off":false,"heap":0,"ntp":false}` |
| `availability` | ESP→GUI | `online` / `offline` (retained, LWT) |

Rules:
- `t` = milliseconds since boot (uint32). `id` = CAN ID as `"0x%03x"` (lowercase). `d` = 7 or 8 data bytes as a hex string without spaces, lowercase (2 characters per byte). `tx` = true for frames sent by the ESP.
- `status` values: `ok` | `unsupported` (raw value 0x8000) | `timeout`.
- `raw` and `index` are strings `"0x%04x"`, `member` is `"0x%03x"` (CAN ID of the member).
- Minimum interval between two sent requests: 100 ms. `delay_ms` below 100 is raised to 100.
- Read requests and (debug firmware only) write requests via cmd/write, see section 10.

## 2. Elster frame format (from `ElsterTable.json`, `frame_format`)

- Own CAN ID (sender of requests): `0x680`. Standard ID (11 bit). DLC 7 when sending.
- Read request, index ≤ 0xFF: `[read_id0, read_id1, idx_lo, 0, 0, 0, 0]`
- Read request, index ≥ 0x100: `[read_id0, read_id1, 0xFA, idx_hi, idx_lo, 0, 0]`
- Response: CAN ID = member ID, `data[0] & 0x0F == 2`. `data[2]==0xFA` → `index=(d3<<8)|d4`, `value=(d5<<8)|d6`; otherwise `index=d2`, `value=(d3<<8)|d4`.
- Message type = `data[0] & 0x0F`. Receiver = `((data[0]&0xF0)<<3)|data[1]`.
- Value `0x8000` = not supported.

## 3. C++ headers of the firmware (all in `firmware/`, namespace `mkdbg`, C++11, only `<stdint.h>`, `<stddef.h>`, `<string.h>`, `<stdio.h>`, `<stdlib.h>`; no ESPHome include in 3.1–3.3, so they can be tested on the PC with `g++ -std=c++11`)

### 3.1 `ring_buffer.h`
```cpp
namespace mkdbg {
struct Frame { uint32_t t; uint16_t id; uint8_t dlc; uint8_t data[8]; bool tx; };
template <size_t N> class RingBuffer {      // N Zweierpotenz (static_assert)
 public:
  bool push(const Frame &f);                 // false und overflow()++ wenn voll (neuer Frame verworfen)
  bool pop(Frame &out);                      // false wenn leer
  size_t size() const;  size_t capacity() const;  // capacity() == N
  uint32_t overflow() const;  void clear();
};
}
```
Memory: `Frame buf_[N]` as a member; for large N optionally constructor parameter `Frame *external` (PSRAM). Single-producer/single-consumer, `volatile`/`std::atomic` for head/tail, without mutex.

### 3.2 `elster_frame.h`
```cpp
namespace mkdbg {
struct Member { const char *name; uint16_t can_id; uint8_t read_id[2]; uint8_t write_id[2]; };
extern const Member MEMBERS[];  extern const size_t MEMBER_COUNT;   // Werte 1:1 aus can_members der ElsterTable.json
const Member *find_member(uint16_t can_id);                         // nullptr wenn unbekannt
size_t build_read_request(const Member &m, uint16_t index, uint8_t out[7]);  // Format Abschnitt 2, gibt 7 zurück
struct Parsed { uint8_t msg_type; uint16_t receiver; uint16_t index; uint16_t value; };
bool parse_frame(const uint8_t *data, size_t len, Parsed &out);     // false wenn len < 7
bool is_response_for(const Parsed &p, uint16_t sender_id, uint16_t member_id, uint16_t index); // msg_type==2 && sender==member && index gleich
}
```
Define as `inline`/`static` in the header file (header-only; `MEMBERS` as `static const`).

### 3.3 `debug_json.h`
Hand-written minimal JSON code without a library (strings in `char` buffers, `snprintf`, `strstr`).
```cpp
namespace mkdbg {
struct SniffCmd { bool on; uint16_t ids[16]; uint8_t id_count; uint8_t types[8]; uint8_t type_count; };
struct ReadCmd  { uint16_t member; uint16_t index; };
struct ScanCmd  { uint16_t member; uint16_t from; uint16_t to; uint32_t delay_ms; };   // delay_ms bereits auf >=100 geklemmt
bool parse_sniff(const char *json, SniffCmd &out);
bool parse_read(const char *json, ReadCmd &out);
bool parse_scan(const char *json, ScanCmd &out);       // false bei from>to oder fehlenden Feldern
size_t format_rx_batch(const Frame *frames, size_t count, char *out, size_t cap);   // JSON-Array nach Abschnitt 1; schreibt nie über cap; gibt Länge zurück (0 bei cap zu klein)
size_t format_read_result(uint16_t member, uint16_t index, uint16_t raw, const char *status, char *out, size_t cap);
size_t format_scan_progress(uint16_t index, uint32_t done, uint32_t total, char *out, size_t cap);
size_t format_status(uint32_t rx, uint32_t tx, size_t rx_fill, size_t rx_cap, uint32_t overflow, uint32_t twai_err, bool bus_off, uint32_t heap, char *out, size_t cap);
}
```
`debug_json.h` includes `ring_buffer.h` (for `Frame`). Hex numbers in inputs are strings `"0x…"` or `"0X…"` or decimal.

### 3.4 `can_debug.h` (glue, later wave)
Includes 3.1–3.3 and ESPHome. Provides (globally, callable from YAML lambdas):
```cpp
void dbg_on_frame(uint32_t can_id, const std::vector<uint8_t> &x);   // aus canbus on_frame
void dbg_on_command(const std::string &topic_suffix, const std::string &payload);  // sniff|read|write|scan|probe|stop
void dbg_loop();                                                      // aus interval: 20ms – RX-Ringpuffer leeren, Batch publizieren, Scanner/Read-State-Machine, TX-Takt
void dbg_publish_status();                                            // aus interval: 5s
```
MQTT publishing via `id(mqtt_client).publish(topic, payload, len, qos, retain)`.

## 4. YAML (wave 1: scaffold)
- `mk1-debug.yaml`: substitutions as Mk1 (`can_tx_pin: GPIO18`, `can_rx_pin: GPIO16`, `can_id_pc: "0x680"`, `can_bitrate: 50kbps`, Ethernet pins GPIO13/11/12/14/10/9), device name `mk1-debug`, packages `firmware/board_esp32s3_espidf.yaml`, `firmware/network_ethernet_w5500.yaml`, `firmware/can_debug.yaml`. `api:` with `encryption: key: !secret debug_api_key`, `ota: platform: esphome`, `mqtt:` with `broker/port/username/password` from `!secret mqtt_server|mqtt_port|mqtt_username|mqtt_password`, `id: mqtt_client`, `discovery: false`, `topic_prefix: stiebel-debug`, birth/will on `stiebel-debug/availability` (`online`/`offline`, retained), `on_message` for `stiebel-debug/cmd/sniff|read|write|scan|probe|stop` (mk1-debug.yaml), each calling `dbg_on_command(<suffix>, x)`. Unknown suffixes only log a warning. `includes:` the headers from section 3.
- `firmware/can_debug.yaml`: `canbus: esp32_can`, `id: my_can`, pins/ID/bitrate from substitutions, `use_extended_id: false`, `rx_queue_len: 512`, `tx_queue_len: 64`, `on_frame` with `can_id: 0`, `can_id_mask: 0` → `dbg_on_frame(can_id, x);`; `interval:` 20 ms → `dbg_loop();` and 5 s → `dbg_publish_status();`.

## 5. Python modules of the GUI (all in `gui/`)

### 5.1 `elster.py` (no Qt, no paho)
```python
class ElsterDb:
    def __init__(self, json_path: str | None = None): ...      # Standard: ../referenzen/elster_reference/ElsterTable.json relativ zu elster.py
    members: list[dict]                                         # Einträge aus can_members (name, can_id, read_id, write_id, ...)
    def member_by_id(self, can_id: int) -> dict | None
    def member_name(self, can_id: int) -> str                   # "UNBEKANNT_0x%03x" falls nicht gefunden
    def signal_by_index(self, index: int) -> dict | None
    def signal_name(self, index: int) -> str                    # "UNK_0x%04x" falls nicht gefunden
    def search_signals(self, text: str) -> list[dict]           # Teilstring, case-insensitiv, auf name und index
def parse_frame(can_id: int, data: bytes) -> dict | None        # None wenn len(data) < 7; sonst {"sender","receiver","msg_type","msg_type_name","index","value"}
def decode_value(type_name: str, raw: int) -> str               # Regeln aus ElsterTable.json types / KElsterTable.cpp SetValueType; raw 0x8000 -> "-255"
def build_read_request(member_can_id: int, index: int, db: ElsterDb) -> tuple[int, bytes]   # (0x680, 7 Bytes) nach Abschnitt 2
```

### 5.2 `mqtt_client.py` (PySide6.QtCore, paho-mqtt 2.x API)
```python
class MqttBridge(QObject):
    connected = Signal(bool)
    rx_frames = Signal(list)         # Liste von dict {"t","id"(int),"data"(bytes),"tx"(bool)}
    read_result = Signal(dict)       # Payload von read/result, member/index/raw als int
    scan_progress = Signal(dict)
    scan_result = Signal(dict)       # items mit index/raw als int
    status = Signal(dict)
    availability = Signal(str)
    def connect_to(self, host: str, port: int, user: str, password: str) -> None
    def disconnect_from(self) -> None
    def send_sniff(self, on: bool, ids: list[int], types: list[int]) -> None
    def send_read(self, member: int, index: int) -> None
    def send_scan(self, member: int, from_idx: int, to_idx: int, delay_ms: int) -> None
    def send_stop(self) -> None
```
paho loop in its own thread (`loop_start`); signals only via Qt signals (thread-safe), no direct widget access. The password is never stored or logged.

### 5.3 Tabs (each a `QWidget`, each in its own file)
- `tab_live.py`: `class LiveTab(QWidget)`; `__init__(self, db: ElsterDb, bridge: MqttBridge)`; model `FrameTableModel(QAbstractTableModel)`.
- `tab_query.py`: `class QueryTab(QWidget)`; same signature.
- `tab_scanner.py`: `class ScannerTab(QWidget)`; same signature.
- `tab_status.py`: `class StatusTab(QWidget)`; `__init__(self, bridge: MqttBridge)`.
- `export_io.py` (no Qt): `save_frames_csv(path, rows)`, `save_frames_jsonl(path, rows)`, `load_frames_jsonl(path) -> list[dict]`, `save_scan_csv(path, items, db)`. A `row` is `{"t":int,"id":int,"data":bytes,"tx":bool}`.
- `elster_gui.py` (main window, later wave): `QMainWindow` with the four tabs, connection dialog (password is asked for, not stored), menu File→Export/load offline log.

## 6. Tests
- Firmware headers: there is NO host C++ compiler on this PC (no g++/clang++/cl). No C++ tests are run; the headers must be checked carefully by hand. The overall check is done later by `esphome compile`. Whoever writes a header additionally creates `tests/test_<name>.cpp` (`main()` with `assert`) as source code, but does not run it.
- Python: `tests/test_elster.py`, `tests/test_export_io.py` with `pytest` or `unittest`.
- Examples for frame tests (from `frame_format.examples`): reading `PROGRAMMSCHALTER 0x0112` from KESSEL → CAN `0x680`, data `31 00 fa 01 12 00 00`; reading `0x0003` from SPEICHER → `41 01 03 00 00 00 00`; response KESSEL→ESP `d2 00 fa 01 12 02 00`.

## 7. Extension: automatic mode (plan: `../plan/mk1-debug-auto-plan.md`, in the original stiebel-debug repo, not part of this repo)

### 7.1 Generic addressing (firmware and `elster.py`)
- Read ID from every CAN ID 0x000–0x7FF: `d0 = ((can_id >> 3) & 0xF0) | 0x01`, `d1 = can_id & 0x7F`. Own ID `0x680` is forbidden as target.
- Format: `0` = auto, `1` = short, `2` = long. Auto: short format only if `index <= 0xFF` AND `index != 0xFA`, otherwise long. Short: only for `index <= 0xFF`; `0xFA` is still sent long (collision with the long-format identifier). Long: always `[d0, d1, 0xFA, hi, lo, 0, 0]`, also for indices ≤ 0xFF.
- Firmware `elster_frame.h`: new function `static inline size_t build_read_request_id(uint16_t can_id, uint16_t index, uint8_t fmt, uint8_t out[7]);`. The old `build_read_request(const Member&, ...)` remains and internally calls `build_read_request_id(m.can_id, index, 0, out)`. Response detection remains `is_response_for` (format of the response irrelevant, only the decoded index counts).
- `elster.py`: `build_read_request(member_can_id, index, db=None, fmt="auto") -> (0x680, bytes)`, `fmt` in `"auto"|"short"|"long"`, calculated by the formula above (no longer via the member table); `ValueError` for target 0x680 or ID > 0x7FF.

### 7.2 MQTT extensions (all prefix `stiebel-debug/`)
| Topic | Direction | Payload |
|---|---|---|
| `cmd/read` | in | as before, additionally optional `"format":"auto"|"short"|"long"`; `member` = any CAN ID |
| `cmd/scan` | in | as before, additionally optional `"format"` (default `auto`); `member` = any CAN ID ≠ 0x680 |
| `cmd/probe` | in | `{"index":"0x000b","from_id":"0x000","to_id":"0x7ff","delay_ms":100}` – queries one index at every CAN ID in the range (0x680 is skipped), timeout = delay_ms |
| `scan/progress` | out | `{"member":"0x180","format":"auto","index":"0x4e40","done":64,"total":512,"state":"running"}` – `state` ∈ `running|done|stopped`; always sent on `done`/`stopped` |
| `scan/result` | out | `{"member":"0x180","format":"auto","items":[{"index":"0x4e40","raw":"0x0123","status":"ok"}]}` |
| `probe/progress` | out | `{"index":"0x000b","id":"0x1a0","done":416,"total":2047,"state":"running"}` |
| `probe/result` | out | `{"index":"0x000b","items":[{"id":"0x180","raw":"0x8000","status":"unsupported"}]}` – only IDs with a response, chunks ≤ 32 |
| `cmd/stop` | in | aborts scan AND probe |

Scan result items: only `ok` and `unsupported` (timeouts are only counted). New fields are additive; existing GUI tabs ignore unknown fields.

### 7.3 `gui/auto_runner.py` (headless, no Qt, paho-mqtt 2.x)
CLI: `python -I auto_runner.py --out <dir> [--secrets ../secrets.yaml] [--delay 100] [--passive 60] [--resume <state.json>] [--smoke]`
- Reads `mqtt_server`, `mqtt_port`, `mqtt_username`, `mqtt_password` from `secrets.yaml` (simple line parser `key: "value"`/`key: value`); never outputs credentials.
- Files in `--out`, prefix `auto_<YYYYmmdd_HHMMSS>`: `_rx.jsonl` (every rx frame, format like recordings), `_status.jsonl` (every status message with PC time), `_state.json` (progress + all results, written atomically after each chunk: tmp + replace), `_runner.log`.
- Sequence: wait for `availability` = `online` (max 5 min) → `cmd/sniff` on, all → Step 1: collect passive senders for `--passive` s, then `probe` 0x000–0x7ff with index `0x000b` → members = probe responses (sorted); passive senders without a probe response are noted as `silent` → Step 2 stage by stage over ALL members: A1 `0x0000–0x00ff` short, A2 `0x0000–0x00ff` long, B `0x0100–0x0fff`, C `0x4e00–0x4fff`, `0xc000–0xc3ff`, `0xfff0–0xffff`, D rest (`0x1000–0x4dff`, `0x5000–0xbfff`, `0xc400–0xffef`); each range in partial scans of max. 0x1000 indices (continuation granularity) → Step 3: all hits (status ok) 3 passes via `cmd/read`, interval between passes ≥ 60 s.
- A partial scan is finished when `scan/progress.state` = `done`. If progress stays absent for 60 s, or `availability` goes `offline`: wait until `online`, then repeat the entire partial scan.
- Safety: `status.bus_off` true or `twai_err` rises by more than 100 between two status messages → `cmd/stop`, save state, exit code 3.
- `--smoke`: only probe `0x170–0x210`, member scan `0x0000–0x001f` auto and `0x00f8–0x0102` auto, 1 read pass; for quick testing.
- Exit codes: 0 ok, 2 ESP not online, 3 bus problem, 4 other error. Ctrl+C → `cmd/stop`, save state.

### 7.4 `gui/type_guess.py` (no Qt)
- `load_mkgit_table(path) -> dict[int, (name, type)]` from mk-git `ElsterTable.h` (`<project folder>\ha-stiebel-control\esphome\ha-stiebel-control\elster\ElsterTable.h`, line format `{ "NAME", 0x1234, et_xxx|0, ...}`).
- `guess_types(raws: list[int]) -> list[tuple[str, float, str]]` – type candidates with score 0..1 and decoded example value, per the rules from plan step 3.
- `analyse(state_json_path, out_prefix)` → `<prefix>_analyse.csv` (semicolon, UTF-8 BOM: member;member_name;index;name_ref;type_ref;name_mkgit;type_mkgit;raws;constant;candidates;conflict) and `<prefix>_analyse.md` (summary: members, hit counts per stage, short/long comparison for 0x00–0xFF, type conflicts, unknown indices with candidates).
- CLI: `python -I type_guess.py <state.json>`.

### 7.5 Format of `_state.json` (written by auto_runner, read by type_guess)
```json
{
  "version": 1,
  "started": "2026-10-20T20:00:00",
  "finished": null,
  "delay_ms": 100,
  "passive_ids": ["0x100", "0x180"],
  "probe": {"index": "0x000b", "items": [{"id": "0x180", "raw": "0x8000", "status": "unsupported"}]},
  "members": ["0x180", "0x201"],
  "silent": ["0x100"],
  "tasks": [
    {"stage": "A1", "member": "0x180", "format": "short", "from": "0x0000", "to": "0x00ff", "done": true,
     "items": [{"index": "0x000c", "raw": "0x0058", "status": "ok"}]}
  ],
  "rereads": [{"member": "0x180", "index": "0x000c", "pass": 1, "t": "2026-10-20T23:00:00", "raw": "0x0059", "status": "ok"}]
}
```
`tasks` contains all planned partial scans in execution order (`done` false until finished). When resuming, tasks with `done: true` are skipped.

## 8. Switch bytes and production streaming (plan: `<project folder>\.claude\plans\1-kannst-du-bitte-unified-zephyr.md`)

### 8.1 Frame rules (measured 20/21 Oct 2026)
- `data[2]` is a switch byte. Long layout (`d3/d4` = index, `d5/d6` = value) only for `0xF7`, `0xF8` (minimum), `0xF9` (maximum), `0xFA` (current value, default). All other `data[2]` values are short layout (`d2` = index, `d3/d4` = value), including `0xF0`–`0xF6` and `0xFB`–`0xFF`.
- `0xFE` with message type 6/7 is a system message (no index). System messages are read like short layout (`index = d2`, `value = d3/d4`) and marked with `system = true`.
- Request format: the request determines the layout of the response. Long format is mandatory for `switch != 0xFA` and for the indices `0xF7`, `0xF8`, `0xF9`, `0xFA` (otherwise `d2` is read as the switch). Otherwise: `auto` = short for `index <= 0xFF`, `short` = only for `index <= 0xFF` (index `0xF7`–`0xFA` is still sent long), `long` = always long.
- Switch values in the request: `0xFA` (default), `0xF8` (min), `0xF9` (max). Other values: error.

### 8.2 `gui/elster.py`
- `SWITCH_LONG = (0xF7, 0xF8, 0xF9, 0xFA)`.
- `parse_frame(can_id, data)` additionally returns `"switch"`: `int` (0xF7–0xFA) for long layout, otherwise `None`; and `"system"`: `bool` (msg_type 6 or 7). With `system`, always short layout. All previous fields remain.
- `build_read_request(member_can_id, index, db=None, fmt="auto", switch=0xFA) -> (0x680, bytes)`: rules from 8.1; `ValueError` for invalid `switch`, for target 0x680, ID > 0x7FF, index > 0xFFFF, `fmt="short"` with index > 0xFF.
- `switch_label(switch: int | None) -> str`: `None` or `0xFA` → `""`, `0xF8` → `"min"`, `0xF9` → `"max"`, `0xF7` → `"f7"`.
- Rows/lines from recordings (`{"t","id","data","tx"}`) remain unchanged; the switch is derived from `data`.

### 8.3 MQTT extension (additive)
- `cmd/read` and `cmd/scan`: optional `"switch":"fa"|"f8"|"f9"` (also `"0xfa"` etc., case-insensitive), default `fa`; invalid → command rejected.
  Since 2026-11-15 also any other byte `f0`–`ff` for testing (long format `[d0,d1,sw,ih,il,0,0]`). The response matching only knows F7–FA; for the other bytes a `timeout` occurs, and the response is in the sniffer stream. Measurement WPL-A 07: F7 with a real index returns `0x8000`; F0–F4, FB–FD, FF respond in short layout without index echo, F5/F6 not at all (`daten/can-logs/f7test_20261115.jsonl`, `f0test_20261115.jsonl` in the project folder).
- `read/result`: additionally `"switch":"0xfa"`. `scan/progress` and `scan/result` (top level, next to `format`): additionally `"switch":"0xfa"`. If the field is missing (older firmware), `0xFA` applies.
- `gui/mqtt_client.py`: `send_read(member, index, fmt=None, switch=None)`, `send_scan(member, from_idx, to_idx, delay_ms, fmt=None, switch=None)`: `fmt` in `None|"auto"|"short"|"long"` (payload field `format` only if not `None`/`"auto"`), `switch` in `None|0xFA|0xF8|0xF9` (payload field only if not `None`/`0xFA`, as `"0xf8"`). The `read_result`/`scan_progress`/`scan_result` signal dicts always contain `"switch"` as `int` (default `0xFA`); `scan_result["items"]` likewise for each item.
- Availability: `mqtt_client.py` additionally subscribes to `heatingpump/status` (payload `online`/`offline`, production firmware). New signal `availability_source = Signal(str)` with `"debug"` (topic `stiebel-debug/availability`) or `"produktiv"` (topic `heatingpump/status`), emitted immediately before `availability`. Offline wins if either topic reports `offline` (remember the last reported state per source; effectively `online` only if no source reports `offline` and at least one reports `online`).

### 8.4 Debug firmware (C++)
- `elster_frame.h`: `build_read_request_id(uint16_t can_id, uint16_t index, uint8_t fmt, uint8_t sw, uint8_t out[7])` (sw ∈ {0xFA, 0xF8, 0xF9}, rules 8.1); `build_read_request(const Member&, index, out)` remains (calls with fmt 0, sw 0xFA). `struct Parsed` gets `uint8_t sw` (0 = short layout). `parse_frame`: long layout when `data[2]` ∈ {F7,F8,F9,FA}, then `sw = data[2]`. `is_response_for(const Parsed&, uint16_t sender, uint16_t member, uint16_t index, uint8_t sw)`: `msg_type == 2 && sender == member && p.index == index &&` (`sw == 0xFA ? (p.sw == 0 || p.sw == 0xFA) : p.sw == sw`).
- `debug_json.h`: `ReadCmd` and `ScanCmd` get `uint8_t sw` (default 0xFA, `dj_get_switch`); `format_read_result(member, index, sw, raw, status, out, cap)`, `format_scan_progress` and the scan/result chunk in `can_debug.h` contain `"switch":"0x%02x"`.
- `can_debug.h`: `req_switch` is set when sending; the response matching calls `is_response_for(..., req_switch)`. Probe stays `fmt 0`, `sw 0xFA`.

### 8.5 Production firmware (`esp32-s3-technik-mk1/ha-stiebel-control/`)
- New header `can_rx_stream.h` (+ copies of `ring_buffer.h`, `debug_json.h` from `mk1-debug/firmware/`), namespace `mkdbg`, functions: `void dbgrx_on_frame(uint32_t can_id, const std::vector<uint8_t>&)`, `void dbgrx_note_tx(uint32_t can_id, const std::vector<uint8_t>&)`, `void dbgrx_on_command(const std::string& suffix, const std::string& payload)` (only `"sniff"`), `void dbgrx_flush()`, `void dbgrx_publish_status()`.
- Topics: `stiebel-debug/status`, `heatingpump/status` (availability, `online`/`offline`). The sniffer stream runs via `heatingpump/can_debug/*` (see 8.7), no longer via `stiebel-debug/rx`/`cmd/sniff`. No commands read/scan/probe/stop. This state is older; for the current state see section 13.

### 8.7 Production sniffer `heatingpump/can_debug/*` (GUI side, 2026-11-23)
The production firmware no longer has `stiebel-debug/rx` and `stiebel-debug/cmd/sniff`. Instead (topic root `PROD_ROOT = "heatingpump"` in `mqtt_client.py`, all production topics derived from it):

| Topic | Direction | Payload |
|---|---|---|
| `heatingpump/can_debug/set` | GUI → ESP | `on` / `off` (raw text, no JSON; no filter in the firmware) |
| `heatingpump/can_debug/state` | ESP → GUI | `on` / `off`, retained |
| `heatingpump/can_debug/frames` | ESP → GUI | JSON array `[{"t":<ms since boot>,"id":"0x180","d":"<hex>","tx":false},...]`, without field `u` |

- `mqtt_client.py` subscribes to `frames` and `state` on connect. Frames are processed like `stiebel-debug/rx` (`_handle_frames`). If `u` is missing, `ts_src = "pc"` applies (ClockSync from `t`, section 11.2).
- Signal `can_debug_state = Signal(bool)`: only `"on"`/`"off"` are emitted, other values are ignored. `tab_live.py` then sets the checkbox "Sniffer an" without sending back.
- `send_sniff(on, ids, types)`: fw `prod` → `heatingpump/can_debug/set`; `ids`/`types` are dropped. Fw `debug` or unknown (before the first `status`) → `stiebel-debug/cmd/sniff` as before.
- Filter in production mode is client-side: `tab_live.py` discards frames that do not match the CAN ID list and type before they go into the buffer (`_frame_matches`). Reason: the firmware sends unfiltered, and the display buffer is limited. Frames received before a filter change are therefore missing afterwards.
- `stiebel-debug/status` (production) contains only `fw`, `rx`, `heap`, `ntp`, and with TWAI additionally `twai_err`, `bus_off`, `rx_missed`, `rx_overrun`, `rx_queue`, `tec`, `rec`. Missing fields (`tx`, `rx_fill`, `rx_cap`, `overflow`, ...) are shown as `-` by `tab_status.py`. Numeric fields with null, text, list or inf also count as missing; `bus_off` is only evaluated as a bool (null = `-`).
- Debug firmware (`stiebel-debug/rx`, `cmd/read`, `cmd/scan`, ...) remains supported unchanged.

### 8.6 Persistence (auto runner / evaluation)
- Tasks in `_state.json` optionally get `"switch":"0xfa"` (missing = `0xfa`). Rereads: optional `"switch"`. New optional list `state["minmax"]`: `[{"member":"0x180","index":"0x0003","switch":"0xf8","raw":"0x0100","status":"ok"}]`.
- `auto_runner.py`: new CLI switch `--minmax` (default off): after the rereads, for each `ok` hit one `cmd/read` each with switch `f8` and `f9`, results in `state["minmax"]`, resumable.
- `export_io.py`: `FRAME_CSV_HEADER` = `t;richtung;can_id;data_hex;absender;empfaenger;msg_type;index;switch;name`; `SCAN_CSV_HEADER` = `index;switch;name;raw;status;wert`. Switch column: `""` for short layout or 0xFA, otherwise `min`/`max`/`f7`.

## 9. Bit fields (reference extension)

### 9.1 Reference JSON
- New optional signal field `bits` (additive, type stays `et_default`):
```json
"bits": [{"bit": 0, "name": "VERDICHTER", "confirmed": true, "comment": "optional"}]
```
- `bit`: 0..15. `name`: unique per signal. Bits unique per signal.
- `comment`: optional.

Example `BETRIEBS_STATUS` (index `0x0176`, owner `0x180`):
- Bit 0 `VERDICHTER`, bit 6 `SPEICHERLADEPUMPE`, bit 12 `ZUSATZHEIZUNG`, bit 14 `WARMWASSER` (checked on the display: Warmwasser (hot water) 0x4041, Heizen (heating) 0x0041, Notlauf (emergency operation) 0x1040).
- Value big-endian from `data[5]`/`data[6]`.
- Measured: `0x0000`, `0x0041`.

Example `BETRIEBS_STATUS_WPLA` (index `0x4eef`, owner `0x480`, type `et_default`, source kr0ner/OneESP32ToRuleThemAll `yaml/wpl_a_base.yaml`, 12 bit):
- Bit 0 `HEIZKREISPUMPE_1`, 1 `HEIZKREISPUMPE_2`, 2 `AUFHEIZPROGRAMM`, 3 `NHZ_STUFEN_IN_BETRIEB`, 4 `WP_IM_HEIZBETRIEB`, 5 `WP_IM_WW_BETRIEB`, 6 `VERDICHTER_IN_BETRIEB`, 7 `SOMMERBETRIEB_AKTIV`, 8 `KUEHLBETRIEB_AKTIV`, 9 `ABTAUBETRIEB`, 10 `SILENT_MODE_1`, 11 `SILENT_MODE_2`.
- Confirmed (measured on the display): bits 0, 4, 6. Measured heating `0x0051` (2026-10-20). Other bits: only source kr0ner, not yet checked on the display.
- `0x8000` gives `-255` (bit 15 not defined).

### 9.2 Built-in default and API
- Built-in default: `elster.BUILTIN_BITS`.
- `elster.set_bit_names`, `elster.decode_bits`.
- `ElsterDb.bits_for`, `ElsterDb.decode`.
- Bit field text: `0x0041: VERDICHTER, SPEICHERLADEPUMPE`. `0x8000` gives `-255`.
- `ReferenceModel.bits`, `ReferenceModel.set_bits`.

### 9.3 Display
- Only for current values (switch `FA`), not for min/max.

## 10. Write (cmd/write)

Debug firmware only (`cmd/write`). The production firmware writes only via `heatingpump/raw/write`, and only with the build switch `can_raw_write` (section 13.4).

### 10.1 Payload `cmd/write` (GUI→ESP)
`{"member":"0x201","index":"0x0013","value":"0x0208","format":"auto"}`
- `member`: CAN ID `"0x%03x"`. `index`: `"0x%04x"`. `value`: raw value 0..0xFFFF (hex or decimal).
- `format` optional: `auto` (default, field missing) | `short` | `long`. As `cmd/read`.
- Text → raw value is done by the GUI (`elster.encode_value`); the firmware receives only raw values.
- Member: any CAN ID 0x000–0x7FF except 0x680, not only the members from `can_members`. Applies equally to query (read/scan/probe) and write. Unknown members are named `UNBEKANNT_0x…` (UNBEKANNT = unknown).

### 10.2 Frame layout for writing
Type 0 (`d0 & 0x0F == 0`), sender 0x680, DLC 7. `d0 = ((id >> 3) & 0xF0) | 0x00`, `d1 = id & 0x7F`.
- Short (index ≤ 0xFF, not 0xF7–0xFA, format auto/short): `[d0, d1, idx, vh, vl, 0, 0]`
- Long (format long or index > 0xFF or 0xF7–0xFA): `[d0, d1, 0xFA, ih, il, vh, vl]`
- Index 0xF7–0xFA is always long (collision with the identifier). Format short with index > 0xFF: error.

### 10.3 Safety rules
- Target 0x000–0x7FF, not 0x680. Otherwise rejected, no response.
- Index 0xF7–0xFA with value 0x8000: `status "rejected"`, nothing sent.
- Only in state MODE_IDLE. Otherwise ignored, no response.
- Invalid payload: ignored, no response (log only).

### 10.4 Sequence
1. Send write frame. It is mirrored in the sniffer as `tx: true`. Read/scan/probe/write are rejected until the end.
2. Ack window 1 s: first frame from the member with msg_type 3, 4 or 5 → `ack` = msg_type. msg_type 2 with the same index → `ack` = 2.
3. Read-back: read request with switch `0xFA`, format as the command, timeout 1 s. Value `0x8000` does not count as a read-back value.
4. Result: read-back value = value → `verified`; ≠ value → `mismatch`; no read-back value but ack → `timeout`; neither ack nor read-back value → `no_ack`. Stop or bus-off during the sequence → `timeout`.
- Note: ack type on our own bus not yet measured.

### 10.5 Topic `write/result` (ESP→GUI)
`{"member":"0x201","index":"0x0013","value":"0x0208","status":"verified","ack":4,"readback":"0x0208"}`
- `member`, `index`, `value`: echo of the command (`"0x%03x"`, `"0x%04x"`, `"0x%04x"`).
- `status`: `verified` | `mismatch` | `no_ack` | `timeout` | `rejected`.
- `ack`: integer (msg_type), missing without ack. `readback`: `"0x%04x"`, missing without read-back value.
- `rejected`: only member/index/value/status.

### 10.6 GUI protection (target)
- Writing only after unlocking (switch, default off).
- Confirmation dialog before every send: member, index, name, raw value and text.
- Types with `types[].writable` false (`elster.is_writable`) and unknown types are not offered. Note: raw value `0x…` is allowed for every type (`encode_value`), therefore the dialog always shows the raw value.

## 11. Timestamps

### 11.1 Firmware
- `t`: ms since boot (uint32, `esphome::millis`). Mandatory field in `rx` frames.
- `u`: Unix ms (uint64) from the ESP, only if SNTP is synchronous (`tv_sec >= 1700000000`). Otherwise missing.
- `status.ntp`: `true` | `false`, shows the SNTP status.

### 11.2 GUI (`mqtt_client.py`)
- `ts`: Unix seconds (float), rounded to 3 decimal places. `ts_src`: `"esp"` (from `u`) or `"pc"` (estimate).
- Fallback `pc` (`ClockSync`): per batch, arrival (PC) − t/1000 = offset + latency. The smallest offset since the last reset is used. Reset when a batch contains a smaller `t` than before (ESP restart). Only frames without `u` feed the estimate.
- Accuracy: `pc` approx. 10–50 ms, SNTP (`esp`) approx. 1–5 ms.
- Live tab (`tab_live.py`): column "ESP uptime (ms)" always shows `t` and sorts by `t`. Column "Clock time" shows `ts` as local time `hh:mm:ss.mmm` (suffix ` ~` and tooltip if `ts_src` is `pc`) and sorts by `ts`, else `t`.

### 11.3 Files
- CSV (`export_io.py`): columns `t;zeit_unix;zeit_lokal;…`. `zeit_unix` = `ts` with 3 decimal places. `zeit_lokal` = PC local time `JJJJ-MM-TT hh:mm:ss.mmm`. Without `ts` both stay empty.
- JSONL: field `ts` directly after `t` (only if valid). `ts_src` at the end.
- Old logs without `ts` remain readable. The lines are valid; only the timestamp is missing.
- Photos/EXIF: camera time is usually local time without a time zone. Check against PC time; mind time zone and daylight saving time.

## 12. Sender ID (cmd/read "src", 2026-11-02)

- `cmd/read` optional `"src":"0x700"`: sender CAN ID of the request (0x000–0x7FF, default `0x680`). `src` equal to `member`: rejected.
- With `src` ≠ 0x680 only a response to receiver `src` counts. Frames with CAN ID `src` are ignored during matching.
- `read/result` then additionally contains `"src":"0x700"` (at the end). Without `src` the output stays unchanged.
- The sniffer mirrors the request with `id` = `src` and `tx: true`.
- Scan, probe and write continue to send from 0x680.
- `cmd/read` optional `"timeout_ms"`: 100–10000 (default 1000, below 100 becomes 100). FES (0x100) responds only after 0.5–1.5 s, with backlog up to about 4 s (measured 2026-11-02).
- Caution: using an ID that itself sends on the bus as `src` can trigger collisions (error frames). Responses then go to the real device.
- Test tool: `tools/fes_src_test.py` (FES 0x100 with changing senders; active IDs only with `--spoof`).

## 13. Production mode (topic root `heatingpump/`)

Applies to the production firmware (`esp32-s3-technik-mk1`). The topic root is `PROD_ROOT = "heatingpump"` in `gui/prod_api.py` (also payload construction and response parser there) and adopted in `gui/mqtt_client.py`.

Source of the firmware side: PR #141 in the fork `bastard-GIT/ha-stiebel-control` (branch `feat/bus-analysis`, files `esphome/ha-stiebel-control/bus_scan.h`, `can_raw.h`, `can_debug.h`, `common.yaml`; docs `docs/CONFIGURATION.md`, section "Bus analysis"). The upstream PR is a draft. Status 2026-11-24. Which firmware runs on the Mk1 and whether it contains this state is stated in the tag, not in this section.

### 13.0 Build options

| Function | Build switch (`common.yaml`) | Topics |
|---|---|---|
| Bus scan | default, always built in | `scan/start`, `scan/stop`, `scan/status`, `scan/<name>/0x<idx>` |
| Participant scan | `can_debug: "true"` | `scan/members`, `scan/members/0x<id>` |
| Single read | `can_debug: "true"` | `raw/read`, `raw/result` |
| Write | `can_debug: "true"` and `can_raw_write: "true"` | `raw/write`, `raw/result` |
| CAN frame stream | `can_debug: "true"` | `can_debug/set`, `can_debug/state`, `can_debug/frames` |

Without a switch the firmware only logs that the function is not built in. Mk1 (production YAML): `can_debug: "true"`, `can_raw_write: "false"`.

### 13.1 Bus scan (`scan/*`)

Queries a member (or a CAN ID) for indices, either over the whole signal table or over a hex range. Only indices that respond are published.

| Topic | Direction | Payload |
|---|---|---|
| `heatingpump/scan/start` | GUI/HA → ESP | `MEMBER\|0xID [0xLO 0xHI] [fa\|f8\|f9]` |
| `heatingpump/scan/stop` | GUI/HA → ESP | any payload, aborts immediately |
| `heatingpump/scan/status` | ESP → | `{"state":"running\|done\|stopped","mode":"table\|range\|members","member":"…","done":…,"total":…,"hits":…,"seconds":…}`, retained |
| `heatingpump/scan/<name>/0x<idx>` | ESP → | `{"name":"…","raw":…,"value":"…"}`, retained |
| `heatingpump/scan/<name>/0x<idx>/f8`, `/f9` | ESP → | as above, for the switch bytes minimum and maximum |

Rules:
- `MEMBER`: name from the member list (case-insensitive, PC excluded) or hex CAN ID `0x000`–`0x7FF` (not `0x680`). A hex ID from the table scans the member under its name. Any other hex ID scans the target and publishes under `0x%03x` (lowercase).
- Without a range the whole signal table runs (approx. 3,600 indices, approx. 9 min at 150 ms), also with a hex ID.
- Range: exactly two hex numbers `LO HI`, each at most `0xFFFF`, `HI >= LO`. It replaces the table.
- Switch byte: `fa` (default, value), `f8` (minimum), `f9` (maximum). At most one, at any position.
- Invalid payload: log only, nothing changes. A valid start replaces a running scan.
- Pace fixed at 150 ms per request. Normal polling pauses during the scan.
- End: 1 s follow-up after the last index, then `done`. `stop` leads to `stopped` immediately.
- `scan/status` is sent at the start, then every 100 requests and at the end. `done` = requests so far, `total` = count, `hits` = hits, `seconds` = duration. `member` is empty in mode `members`.
- Results are sent individually and immediately, not collected. Only responses with raw value ≠ `0x8000` are published. Missing responses (timeouts) are not reported.
- `raw` is a decimal number, not a hex string. `name` comes from the table and is empty for indices without a table entry. `value` is the decoded value as text.

```
mosquitto_pub -h <broker> -u <user> -P <passwort> -t heatingpump/scan/start -m "MANAGER 0xfd00 0xfdff"
mosquitto_pub -h <broker> -u <user> -P <passwort> -t heatingpump/scan/start -m "0x180 0x0000 0x00ff f8"
mosquitto_pub -h <broker> -u <user> -P <passwort> -t heatingpump/scan/stop -m stop
```

### 13.2 Participant scan (`scan/members`)

Queries every CAN ID of a range for one index (switch byte `fa` only) and reports the responses.

| Topic | Direction | Payload |
|---|---|---|
| `heatingpump/scan/members` | GUI/HA → ESP | `[0xLO 0xHI] [0xIDX]`, default `0x000 0x7ff 0x000b` |
| `heatingpump/scan/members/0x<id>` | ESP → | `{"index":"0x<idx>","raw":<decimal>}`, retained |

- Without arguments: all IDs `0x000`–`0x7FF`, index `0x000b`. One value: index. Two values: range. Three values: range and index.
- The own ID (`0x680`) is skipped. The full range takes approx. 310 s (2,048 × 150 ms).
- Progress and end are reported on `scan/status` with `mode` `members`.
- Only with `can_debug: "true"`.

```
mosquitto_pub -h <broker> -u <user> -P <passwort> -t heatingpump/scan/members -m "0x100 0x1ff 0x0013"
```

### 13.3 Single read (`raw/read`)

Reads one index at a CAN ID with switch byte `fa` (default), `f8` or `f9`.

| Topic | Direction | Payload |
|---|---|---|
| `heatingpump/raw/read` | GUI/HA → ESP | `0xID 0xIDX [fa\|f8\|f9]` |
| `heatingpump/raw/result` | ESP → | one JSON object per operation, not retained |

Example: `{"op":"read","id":"0x180","index":"0x0013","switch":"fa","raw":"0x0208","status":"ok"}`

| `status` | Meaning | Field `raw` |
|---|---|---|
| `ok` | response with value | yes |
| `unsupported` | response with raw value `0x8000` | yes (`"0x8000"`) |
| `timeout` | no response within 1 s | no |
| `busy` | bus scan, another raw operation or passive mode is running | no |

- One operation at a time. The response to `raw/read` is not retained.
- Invalid payload or read of the own ID (`0x680`): log only, no response.
- Only with `can_debug: "true"`.

### 13.4 Write (`raw/write`)

Writes an index at a CAN ID and checks the result via read-back. Only with `can_debug: "true"` and `can_raw_write: "true"`.

| Topic | Direction | Payload |
|---|---|---|
| `heatingpump/raw/write` | GUI/HA → ESP | `0xID 0xIDX 0xVALUE` |
| `heatingpump/raw/result` | ESP → | one JSON object per operation, not retained |

> **Security:** With `can_raw_write: "true"` any MQTT client with broker access can write arbitrary values to arbitrary CAN IDs. There is no allowlist. On Mk1 the switch is off (`"false"`). Changing it is a build change and requires a flash.

Sequence: the write frame is sent to the target. Then an ack window of 1 s follows. An ack frame of type 3, 4 or 5, or a type-2 response for the same index, is recorded as `ack` (message type). Then the index is read back with switch byte `fa` (timeout 1 s).

Example: `{"op":"write","id":"0x180","index":"0x0013","value":"0x0208","status":"verified","ack":4,"readback":"0x0208"}`

| `status` | Meaning |
|---|---|
| `verified` | read-back value matches the written value |
| `mismatch` | read-back value differs |
| `timeout` | ack received, no read-back value (read-back value `0x8000` does not count as a value) |
| `no_ack` | neither ack nor read-back value |
| `rejected` | nothing sent: firmware without `can_raw_write`, write to the own ID (`0x680`), `read_only` build, index `0xF7`–`0xFA` with value `0x8000`, or another operation is running (`busy` case) |

With `rejected`, `ack` and `readback` are not set.

### 13.5 CAN frame stream (`can_debug/*`)

- Switch `can_debug` (Mk1: `"true"`). `can_debug_queue_size` (default 256 frames, 16 bytes per frame, only while the stream is on).
- `set` is not stored: after a restart the stream is off. `on` starts with an empty queue, `off` frees the memory.
- Frames include RX and ESP-sent frames (`tx: true`). No field `u`. No filter in the firmware.

| Topic | Direction | Payload |
|---|---|---|
| `heatingpump/can_debug/set` | GUI/HA → ESP | `on` / `off` (raw text, no JSON) |
| `heatingpump/can_debug/state` | ESP → | `on` / `off`, retained |
| `heatingpump/can_debug/frames` | ESP → | `[{"t":123456,"id":"0x180","d":"d200fa01120200","tx":false}]`, not retained, at most CAN_DEBUG_BATCH frames per message |

Other values at `set` are only logged.

### 13.6 GUI functions in production mode

The GUI selects the topic root via the firmware type (field `fw` in `stiebel-debug/status`; if missing, `prod` applies).

| GUI function | Topic | Restriction in production mode |
|---|---|---|
| Query read | `raw/read` | Query tab active (`tab_query.py`, `_apply_lock`). Sender (`src`), timeout and format are disabled (tooltip); fixed sender `0x680`, format `auto`, timeout 1 s. Status `busy` is shown, without value. |
| Query write | `raw/write` | Query tab active after unlocking and confirmation dialog (extra production line). Bridge (`send_write`) only with format `auto`. Status `rejected` (firmware without `can_raw_write` or blocking rule) appears as a red warning. |
| Scanner (range scan) | `scan/start` (with range) | Scanner tab active (`tab_scanner.py`): member by selection or hex ID, range, switch byte. Unknown hex ID: topic name `0x%03x`. Format and pause disabled (tooltip), pace fixed at 150 ms (`delay_ms` drops out). |
| Participant search | `scan/members` | Scanner tab, group "Participant search (production)": ID range, index (default `0x000b`), search/stop, progress, table ID/name/index/raw value, CSV export. Double-click copies the ID into the field "or hex ID". Range scan and participant search lock each other. Also in the auto runner (`--fw prod`). |
| Auto runner `--fw prod` | `scan/start`, `scan/members`, `raw/read`, `raw/result` | Stage A1 (format `short`) is skipped and noted in `state["skipped"]`. `delay_ms` is ignored (also noted in `state["skipped"]`). The participant search uses `scan/members`. |
| Live sniffer | `can_debug/set`, `state`, `frames` | Filter (CAN ID, type) only in the GUI. The firmware sends unfiltered. Frames before a filter change are missing. |
| Status tab | `stiebel-debug/status` | The production firmware sends only `fw`, `rx`, `heap`, `ntp` and, with TWAI, the error counters. The display shows missing fields as `-`. |

### 13.7 Not available in production mode

- Read with a foreign sender ID `src` (`cmd/read` "src"). The default sender is `0x680`.
- `delay_ms` in the scan. The pace is fixed at 150 ms.
- Format `short` and `long`. The production firmware only knows format `auto`.
- `timeout_ms` for read. The response time is fixed at 1 s. For slow participants (FES 0x100 needs 0.5–1.5 s according to the GUI note) this is not enough.
- A separate probe command (`cmd/probe`). The participant search with an arbitrary index runs only via `scan/members` (switch byte `fa`).
- The debug commands `stiebel-debug/cmd/read|scan|probe|stop|write`. Their functions are available via `heatingpump/scan/*` and `heatingpump/raw/*` (sections 13.1–13.4).
- Status fields `rx_fill`, `rx_cap`, `overflow`. The production firmware does not send them.

### 13.8 Retained legacy entries

- `scan/status`, `scan/<name>/0x<idx>` and `scan/members/0x<id>` are retained. After the scan ends they stay on the broker, including a `scan/status` with `running` after a restart.
- The bridge (`mqtt_client.py`) evaluates scan results only if it started a scan itself and `scan/status` reported `running`. Everything else is discarded and counted in `stale_ignored`.
- The auto runner evaluates `scan/status`, hits and member responses only after its own start and after `running`. Old entries are ignored.
- To remove old entries, a retained message with empty payload can be sent to the topic (broker standard). This is not tested.
