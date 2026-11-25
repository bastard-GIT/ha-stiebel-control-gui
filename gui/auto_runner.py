"""Headless automatic runner for mk1-debug (no Qt, paho-mqtt 2.x).

Flow and files per INTERFACES.md sections 7.3 and 7.5 resp. plan plan/mk1-debug-auto-plan.md.
Structure:
- pure functions (planning, secrets, state, evaluation, watchdog, bus safety),
- class Runner with the flow control, which only talks to the MQTT part via a transport
  object with publish()/poll(),
- PahoTransport as the real transport layer (rx capture in the paho thread).

Credentials from secrets.yaml are never printed, logged or written to files.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

# With -I the script directory is not on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mqtt_secrets import SECRETS_PATH, SECRET_KEYS, parse_secrets  # noqa: E402,F401
import prod_api as pa  # noqa: E402

PREFIX = "stiebel-debug/"
PC_CAN_ID = 0x680
PROBE_INDEX = 0x000B
PROBE_FULL = (0x000, 0x7FF)
PROBE_SMOKE = (0x170, 0x210)
PAGE = 0x1000
RX_TOPICS = (
    "rx",
    "read/result",
    "scan/progress",
    "scan/result",
    "status",
    "availability",
    "probe/progress",
    "probe/result",
)

EXIT_OK = 0
EXIT_OFFLINE = 2
EXIT_BUS = 3
EXIT_ERROR = 4
EXIT_INTERRUPTED = 130  # Ctrl+C, in addition to 0/2/3/4 from plan 7.3

ONLINE_WAIT_S = 300.0
STALL_S = 60.0
READ_TIMEOUT_S = 2.0
REREAD_GAP_S = 60.0
REREAD_PASSES = 3
REREAD_SAVE_EVERY = 100  # write state file only every N reads
SMOKE_REREAD_PASSES = 1
TWAI_ERR_LIMIT = 100
MIN_DELAY_MS = 100
DEFAULT_PASSIVE_S = 60.0
DEFAULT_SECRETS = str(SECRETS_PATH)   # secrets.yaml in the repo root folder

# (stage, format for cmd/scan, inclusive ranges). Order per plan step 2.
FULL_STAGES = (
    ("A1", "short", ((0x0000, 0x00FF),)),
    ("A2", "long", ((0x0000, 0x00FF),)),
    ("B", "auto", ((0x0100, 0x0FFF),)),
    ("C", "auto", ((0x4E00, 0x4FFF), (0xC000, 0xC3FF), (0xFFF0, 0xFFFF))),
    ("D", "auto", ((0x1000, 0x4DFF), (0x5000, 0xBFFF), (0xC400, 0xFFEF))),
)
SMOKE_STAGES = (
    ("S1", "auto", ((0x0000, 0x001F),)),
    ("S2", "auto", ((0x00F8, 0x0102),)),
)
# Stage D only for these members (default, CLI --d-members).
D_MEMBERS = (0x180, 0x480, 0x500, 0x514)
# Additional members (FES, does not respond per probe), CLI --extra-members.
EXTRA_MEMBERS = (0x100,)
# Stages in which extra members are planned statically. Stage D only dynamically.
EXTRA_STAGES = ("A1", "A2", "B", "C")
# Switch bytes (INTERFACES.md 8.1/8.6): 0xFA = current value (default), 0xF8 = min, 0xF9 = max.
SWITCH_DEFAULT = 0xFA
SWITCH_VALUES = (0xF7, 0xF8, 0xF9, 0xFA)
MINMAX_SWITCHES = (0xF8, 0xF9)
# Firmware choice (CLI --fw). prod = production API (prod_api.py, topic root heatingpump).
FW_DEBUG = "debug"
FW_PROD = "prod"
BUSY_PAUSE_S = 1.0  # pause before the one retry after "busy"
PROD_A1_SKIP = "format short not available (production)"
PROD_DELAY_NOTE = "delay_ms ignored (production: firmware cycle 150 ms)"
PROD_TOPICS = (pa.PROD_ROOT + "/status", pa.TOPIC_CAN_DEBUG_FRAMES,
               pa.TOPIC_SCAN_WILDCARD, pa.TOPIC_RAW_RESULT)
PROD_SUFFIXES = ("scan/status", "scan/hit", "scan/member", "raw/result")


class ConfigError(Exception):
    """Error in secrets.yaml. Messages never contain values."""


class RunAbort(Exception):
    """Abort run with exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _Retry(Exception):
    """Repeat partial scan/probe/read completely after offline or stall."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Timing:
    """Wait times. Shortenable in tests."""

    passive_s: float = DEFAULT_PASSIVE_S
    gap_s: float = 0.1
    online_wait_s: float = ONLINE_WAIT_S
    stall_s: float = STALL_S
    read_timeout_s: float = READ_TIMEOUT_S
    reread_gap_s: float = REREAD_GAP_S
    busy_pause_s: float = BUSY_PAUSE_S


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hex_member(value: int) -> str:
    return f"0x{value:03x}"


def hex_index(value: int) -> str:
    return f"0x{value:04x}"


def parse_hex(value) -> int:
    """"0x180", "0X180", "180" or int converted to int."""
    if isinstance(value, bool):
        raise TypeError("bool is not a hex value")
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 16)


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_switch(value) -> int:
    """Switch from a field ("fa", "0xf8", int; missing/None = 0xFA). Otherwise ValueError."""
    if value is None:
        return SWITCH_DEFAULT
    sw = parse_hex(value)
    if sw not in SWITCH_VALUES:
        raise ValueError(f"invalid switch: {value}")
    return sw


def switch_cmd(sw: int) -> str:
    """Switch for cmd/scan and cmd/read: "f8"."""
    return f"{sw:02x}"


def switch_state(sw: int) -> str:
    """Switch for _state.json: "0xf8"."""
    return f"0x{sw:02x}"


def entry_switch(entry: dict) -> int:
    """Switch of a task, reread or min/max entry; missing = 0xFA."""
    return parse_switch(entry.get("switch"))


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------
def load_broker_config(path: str) -> BrokerConfig:
    with open(path, encoding="utf-8") as fh:
        values = parse_secrets(fh.read())
    missing = [k for k in SECRET_KEYS if k not in values]
    if missing:
        raise ConfigError(f"secrets.yaml: missing keys: {', '.join(missing)}")
    try:
        port = int(values["mqtt_port"])
    except ValueError:
        raise ConfigError("secrets.yaml: mqtt_port is not a number") from None
    return BrokerConfig(
        host=values["mqtt_server"],
        port=port,
        user=values["mqtt_username"],
        password=values["mqtt_password"],
    )


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------
def split_range(lo: int, hi: int, size: int = PAGE) -> list[tuple[int, int]]:
    """Split range [lo, hi] (inclusive) into sub-ranges of at most `size` indices."""
    out = []
    cur = lo
    while cur <= hi:
        end = min(cur + size - 1, hi)
        out.append((cur, end))
        cur = end + 1
    return out


def build_stage_tasks(stage: str, fmt: str, ranges, member: int) -> list[dict]:
    """Partial scans of one member for one stage (ranges split into parts of at most PAGE indices)."""
    out = []
    for lo, hi in ranges:
        for clo, chi in split_range(lo, hi):
            out.append({
                "stage": stage,
                "member": hex_member(member),
                "format": fmt,
                "from": hex_index(clo),
                "to": hex_index(chi),
                "done": False,
                "items": [],
                "timeouts": 0,
            })
    return out


def plan_tasks(members: list[int], stages=FULL_STAGES, d_members=None) -> list[dict]:
    """Task list: stage -> member -> range -> partial scan. Order per plan step 2.

    d_members: stage D only for these members (None = all).
    """
    tasks = []
    for stage, fmt, ranges in stages:
        for member in members:
            if stage == "D" and d_members is not None and member not in d_members:
                continue
            tasks.extend(build_stage_tasks(stage, fmt, ranges, member))
    return tasks


def refine_tasks(tasks: list[dict], d_members, extra_members, stages=FULL_STAGES) -> list[dict]:
    """Idempotent. Removes unfinished D tasks of foreign members (d_members None = remove nothing)
    and adds missing A1/A2/B/C tasks of extra members directly after the last task of their stage.
    Finished tasks stay untouched. Empty list stays empty (planning happens before).
    """
    if not tasks:
        return tasks
    if d_members is not None:
        allowed = {hex_member(m) for m in d_members}
        tasks[:] = [t for t in tasks
                    if t["done"] or t["stage"] != "D" or t["member"] in allowed]
    order = [s[0] for s in stages]
    for stage, fmt, ranges in stages:
        if stage not in EXTRA_STAGES:
            continue
        for member in extra_members:
            if member == PC_CAN_ID:
                continue
            mhex = hex_member(member)
            if any(t["stage"] == stage and t["member"] == mhex for t in tasks):
                continue
            idx = [i for i, t in enumerate(tasks) if t["stage"] == stage]
            if idx:
                pos = idx[-1] + 1
            else:
                pos = next((i for i, t in enumerate(tasks)
                            if t["stage"] in order and order.index(t["stage"]) > order.index(stage)),
                           len(tasks))
            tasks[pos:pos] = build_stage_tasks(stage, fmt, ranges, member)
    return tasks


def members_from_probe(items: list[dict]) -> list[int]:
    """Members = IDs that replied (ok/unsupported), sorted, without own ID."""
    ids = set()
    for item in items:
        if item.get("status") in ("ok", "unsupported"):
            ids.add(parse_hex(item["id"]))
    ids.discard(PC_CAN_ID)
    return sorted(ids)


def passive_senders(frames: list[dict]) -> set[int]:
    """Sender IDs from rx frames (tx == false), without own ID."""
    return {
        int(f["id"])
        for f in frames
        if not f.get("tx") and int(f["id"]) != PC_CAN_ID
    }


def silent_ids(passive: set[int], members: list[int]) -> list[str]:
    """Passively seen senders without probe reply."""
    return [hex_member(i) for i in sorted(passive - set(members))]


def apply_scan_result(task: dict, payload: dict) -> int:
    """Take items with ok/unsupported into the task, only count timeouts. Returns number of items taken."""
    stored = 0
    for item in payload.get("items", []):
        status = item.get("status")
        if status in ("ok", "unsupported"):
            task["items"].append({
                "index": item["index"],
                "raw": item["raw"],
                "status": status,
            })
            stored += 1
        elif status == "timeout":
            task["timeouts"] = task.get("timeouts", 0) + 1
    return stored


def collect_hits(tasks: list[dict]) -> list[tuple[str, str]]:
    """All hits (status ok) as unique (member, index) pairs, sorted.
    Only tasks with default switch (0xFA); min/max values (0xF8/0xF9) are no hits for rereads."""
    seen = set()
    for task in tasks:
        if entry_switch(task) != SWITCH_DEFAULT:
            continue
        member = parse_hex(task["member"])
        for item in task["items"]:
            if item.get("status") == "ok":
                seen.add((member, parse_hex(item["index"])))
    return [(hex_member(m), hex_index(i)) for m, i in sorted(seen)]


# ----------------------------------------------------------------------
# Watchdog and bus safety
# ----------------------------------------------------------------------
def stalled(last_progress: float, now: float, stall_s: float) -> bool:
    return now - last_progress > stall_s


def bus_problem(prev_twai_err: int | None, status: dict) -> str | None:
    """`bus_off` true or twai_err increase > TWAI_ERR_LIMIT relative to the last status message."""
    if bool(status.get("bus_off")):
        return "bus_off"
    err = _as_int(status.get("twai_err"), None)
    if err is not None and prev_twai_err is not None and err - prev_twai_err > TWAI_ERR_LIMIT:
        return "twai_err"
    return None


# ----------------------------------------------------------------------
# State (_state.json)
# ----------------------------------------------------------------------
def new_state(delay_ms: int, smoke: bool, fw: str = FW_DEBUG) -> dict:
    return {
        "version": 1,
        "fw": fw,
        "started": now_iso(),
        "finished": None,
        "delay_ms": delay_ms,
        "smoke": smoke,
        "passive_ids": [],
        "probe": None,
        "members": None,
        "silent": None,
        "tasks": [],
        "rereads": [],
        "skipped": [],
    }


def save_state(path: str, state: dict) -> None:
    """Write atomically: tmp file, then os.replace."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_state(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("unknown state version")
    return state


# ----------------------------------------------------------------------
# Flow control
# ----------------------------------------------------------------------
class Runner:
    """Flow steps 1 to 3. Events come from transport.poll(), commands go via transport.publish()."""

    def __init__(self, transport, state: dict, *, state_path: str, timing: Timing,
                 log: logging.Logger, smoke: bool, delay_ms: int, status_fp=None,
                 clock=time.monotonic, d_members=D_MEMBERS, extra_members=EXTRA_MEMBERS,
                 minmax: bool = False, prod: bool = False):
        self.transport = transport
        self.prod = prod
        self.state = state
        self.state_path = state_path
        self.timing = timing
        self.log = log
        self.smoke = smoke
        self.delay_ms = delay_ms
        self.status_fp = status_fp
        self.clock = clock
        self.minmax = minmax
        self.probe_range = PROBE_SMOKE if smoke else PROBE_FULL
        self.stages = SMOKE_STAGES if smoke else FULL_STAGES
        self.d_members = None if d_members is None else tuple(d_members)
        self.extra_members = tuple(m for m in extra_members if m != PC_CAN_ID)

        self.online = False
        self.went_offline = False
        self._collect_passive = False
        self._passive: set[int] = set()
        self._last_twai: int | None = None
        self._last_progress = clock()
        self._scan_done = False
        self._scan_results: dict[tuple[int, int], list[dict]] = {}  # (member, switch)
        self._probe_done = False
        self._probe_items: list[dict] = []
        self._read_results: dict[tuple[int, int, int], dict] = {}  # (member, index, switch)
        # Production mode: own scan. expect = {"mode", "name" (lowercase, None = any), "switch"}.
        # Hits/members/status are only counted after "running" with matching mode.
        self._prod_expect: dict | None = None
        self._prod_armed = False
        self._prod_done = False
        self._prod_hits: list[dict] = []
        self._prod_members: list[dict] = []

    # -- Entry ---------------------------------------------------------
    def execute(self) -> int:
        """Run with abort handling. Returns exit code."""
        try:
            return self.run()
        except RunAbort as exc:
            self.log.error("%s", exc)
            code = exc.code
        except KeyboardInterrupt:
            self.log.warning("Ctrl+C: run stopped, state saved")
            code = EXIT_INTERRUPTED
        except Exception:
            self.log.exception("Unexpected error")
            code = EXIT_ERROR
        self._stop()
        return code

    def run(self) -> int:
        st = self.state
        st["fw"] = FW_PROD if self.prod else FW_DEBUG
        st.setdefault("skipped", [])
        if self.prod and self.delay_ms != MIN_DELAY_MS:
            self._note_skipped(PROD_DELAY_NOTE)
            self.log.warning("%s", PROD_DELAY_NOTE)
        self._wait_online()
        self._sniff(True)
        if st["probe"] is None:
            self._step1()
        if not st["tasks"] and st["members"] is not None:
            self._plan()
        st["tasks"] = refine_tasks(st["tasks"], self.d_members, self.extra_members, self.stages)
        if st["tasks"]:
            self._log_stage_counts("Step 2 tasks per stage")
        self._save()
        self._run_tasks()
        self._rereads()
        if self.minmax:
            self._minmax()
        st["finished"] = now_iso()
        self._save()
        self._sniff(False)
        self.log.info("Automatic run finished")
        return EXIT_OK

    def _stop(self) -> None:
        try:
            if self.prod:
                self.transport.publish_text(pa.TOPIC_SCAN_STOP, "stop")
                self.transport.publish_text(pa.TOPIC_CAN_DEBUG_SET, "off")
            else:
                self.transport.publish("cmd/stop", {})
        except Exception:
            pass
        try:
            self._save()
        except OSError as exc:
            self.log.error("State could not be saved: %s", exc)

    # -- Step 1 --------------------------------------------------------
    def _step1(self) -> None:
        self._collect_passive = True
        self._passive = set()
        self.log.info("Step 1: collect passively (%s s)", self.timing.passive_s)
        self._wait_until(lambda: False, self.timing.passive_s, guard=False)
        self._collect_passive = False
        self.state["passive_ids"] = [hex_member(i) for i in sorted(self._passive)]
        self._save()
        while True:
            try:
                self._probe()
                break
            except _Retry as exc:
                self.log.warning("Probe is repeated (%s)", exc.reason)
                self._wait_online()

    def _probe(self) -> None:
        lo, hi = self.probe_range
        if self.prod:
            items = self._probe_prod(lo, hi)
        else:
            self._probe_items = []
            self._probe_done = False
            self._last_progress = self.clock()
            self._publish("cmd/probe", {
                "index": hex_index(PROBE_INDEX),
                "from_id": hex_member(lo),
                "to_id": hex_member(hi),
                "delay_ms": self.delay_ms,
            })
            self._wait_until(lambda: self._probe_done, stall=True)
            # wait for stragglers of the last probe/result chunks
            self._wait_until(lambda: False, self.timing.read_timeout_s)
            items = list(self._probe_items)
        members = members_from_probe(items)
        self.state["probe"] = {"index": hex_index(PROBE_INDEX), "items": items}
        self.state["members"] = [hex_member(m) for m in members]
        self.state["silent"] = silent_ids(self._passive, members)
        self._save()
        self.log.info("Members found: %d (%s)", len(members),
                      ", ".join(hex_member(m) for m in members))
        if self.state["silent"]:
            self.log.info("Passive, without probe reply: %s", ", ".join(self.state["silent"]))
        if not members:
            self.log.warning("No members found")

    # -- Step 2 --------------------------------------------------------
    def _plan(self) -> None:
        """Plan step 2. Members = probe members + extra members (without 0x680).
        Stage D only for probe members in d_members; extra members get D dynamically."""
        st = self.state
        base = [parse_hex(m) for m in st["members"]]
        d_static = [m for m in base if self.d_members is None or m in self.d_members]
        extra_only = [m for m in self.extra_members if m not in base]
        extra_hex = {hex_member(m) for m in extra_only}
        planned = plan_tasks(sorted(set(base) | set(extra_only)), self.stages, d_members=d_static)
        st["tasks"] = [t for t in planned
                       if t["member"] not in extra_hex or t["stage"] in EXTRA_STAGES]
        self.log.info("Step 2: %d partial scans planned", len(st["tasks"]))
        self._save()

    def _log_stage_counts(self, prefix: str) -> None:
        names = [s[0] for s in self.stages]
        counts = {n: 0 for n in names}
        for task in self.state["tasks"]:
            counts[task["stage"]] = counts.get(task["stage"], 0) + 1
        self.log.info("%s: %s", prefix, ", ".join(f"{n}={counts.get(n, 0)}" for n in names))

    def _expand_extra_d(self, tasks: list, pos: int) -> int:
        """Insert stage D for extra members with at least one ok hit (A/B/C) before tasks[pos].
        Returns number of inserted tasks. Duplicate ranges are not inserted."""
        d_spec = next((s for s in self.stages if s[0] == "D"), None)
        if d_spec is None:
            return 0
        _, fmt, ranges = d_spec
        base_hex = set(self.state.get("members") or [])
        extra = [m for m in self.extra_members if hex_member(m) not in base_hex]
        hit_members = {m for m, _ in collect_hits([t for t in tasks if t["stage"] in EXTRA_STAGES])}
        present = {(t["member"], t["from"], t["to"]) for t in tasks if t["stage"] == "D"}
        new: list[dict] = []
        for member in extra:
            mhex = hex_member(member)
            if mhex not in hit_members:
                self.log.info("Extra member %s without answer, stage D skipped", mhex)
                continue
            for task in build_stage_tasks("D", fmt, ranges, member):
                if (task["member"], task["from"], task["to"]) not in present:
                    new.append(task)
        if new:
            tasks[pos:pos] = new
            self.log.info("Stage D for extra member added: %d partial scans", len(new))
        return len(new)

    def _run_tasks(self) -> None:
        tasks = self.state["tasks"]
        expanded = False
        i = 0
        while i < len(tasks):
            task = tasks[i]
            if task["done"]:
                i += 1
                continue
            if self.prod and task["stage"] == "A1":
                self._skip_task(task, PROD_A1_SKIP)
                i += 1
                continue
            if task["stage"] == "D" and not expanded:
                expanded = True  # check once before the first D task
                if self._expand_extra_d(tasks, i):
                    self._save()
                    task = tasks[i]
            number = i + 1
            label = (f"{task['stage']} {task['member']} "
                     f"{task['from']}-{task['to']} {task['format']}")
            while True:
                try:
                    self._run_scan_task(task)
                    break
                except _Retry as exc:
                    self.log.warning("Partial scan %s is repeated (%s)", label, exc.reason)
                    task["items"] = []
                    task["timeouts"] = 0
                    self._wait_online()
            self.log.info("Partial scan %d/%d done: %s, %d hits",
                          number, len(tasks), label, len(task["items"]))
            self._save()
            i += 1

    def _run_scan_task(self, task: dict) -> None:
        if self.prod:
            self._run_prod_scan(task)
            return
        member = parse_hex(task["member"])
        sw = entry_switch(task)
        key = (member, sw)
        self._scan_done = False
        self._scan_results.pop(key, None)
        self._last_progress = self.clock()
        payload = {
            "member": task["member"],
            "from": task["from"],
            "to": task["to"],
            "format": task["format"],
            "delay_ms": self.delay_ms,
        }
        if sw != SWITCH_DEFAULT:
            payload["switch"] = switch_cmd(sw)
        self._publish("cmd/scan", payload)
        self._wait_until(lambda: self._scan_done, stall=True)
        # The firmware sends scan/result in chunks of 32 items, the last chunk before the done progress.
        # Briefly wait for stragglers, then take over all chunks.
        self._wait_until(lambda: False, self.timing.read_timeout_s / 2, guard=False)
        chunks = self._scan_results.pop(key, [])
        if not chunks:
            self.log.info("No scan/result for %s (no replies)", task["member"])
        for payload in chunks:
            apply_scan_result(task, payload)
        task["done"] = True

    # -- Step 3 --------------------------------------------------------
    def _rereads(self) -> None:
        st = self.state
        passes = SMOKE_REREAD_PASSES if self.smoke else REREAD_PASSES
        hits = collect_hits(st["tasks"])
        self.log.info("Step 3: %d hits, %d pass(es)", len(hits), passes)
        last_end = None
        since_save = 0
        for number in range(1, passes + 1):
            done = {(r["member"], r["index"], r["pass"], entry_switch(r)) for r in st["rereads"]}
            todo = [h for h in hits if (h[0], h[1], number, SWITCH_DEFAULT) not in done]
            if not todo:
                continue
            if last_end is not None:
                self.log.info("Pause until pass %d (%s s)", number, self.timing.reread_gap_s)
                self._wait_until(lambda: False, self.timing.reread_gap_s, guard=False)
            for member, index in todo:
                while True:
                    try:
                        result = self._read_one(member, index)
                        break
                    except _Retry as exc:
                        self.log.warning("Read is repeated (%s)", exc.reason)
                        self._wait_online()
                st["rereads"].append({
                    "member": member,
                    "index": index,
                    "pass": number,
                    "t": now_iso(),
                    "raw": result["raw"],
                    "status": result["status"],
                })
                # _state.json is tens of MB after the scans: do not write after every read
                since_save += 1
                if since_save >= REREAD_SAVE_EVERY:
                    self._save()
                    since_save = 0
            self._save()
            since_save = 0
            last_end = self.clock()
            self.log.info("Pass %d done", number)

    def _read_one(self, member: str, index: str, switch: int = SWITCH_DEFAULT) -> dict:
        if self.prod:
            return self._read_one_prod(member, index, switch)
        key = (parse_hex(member), parse_hex(index), switch)
        self._read_results.pop(key, None)
        payload = {"member": member, "index": index}
        if switch != SWITCH_DEFAULT:
            payload["switch"] = switch_cmd(switch)
        self._publish("cmd/read", payload)
        self._wait_until(lambda: key in self._read_results, self.timing.read_timeout_s)
        res = self._read_results.pop(key, None)
        self._wait_until(lambda: False, self.timing.gap_s, guard=False)
        if res is None:
            return {"raw": None, "status": "timeout"}
        return {"raw": res.get("raw"), "status": res.get("status")}

    def _minmax(self) -> None:
        """Step 4 (only with --minmax): per hit cmd/read with switch f8 (min) and f9 (max).
        Results in state["minmax"]; already existing entries are skipped."""
        st = self.state
        entries = st.setdefault("minmax", [])
        hits = collect_hits(st["tasks"])
        done = {(e["member"], e["index"], entry_switch(e)) for e in entries}
        todo = [(m, i, sw) for m, i in hits for sw in MINMAX_SWITCHES
                if (m, i, sw) not in done]
        self.log.info("Min/Max: %d hits, %d reads", len(hits), len(todo))
        since_save = 0
        for member, index, sw in todo:
            while True:
                try:
                    result = self._read_one(member, index, sw)
                    break
                except _Retry as exc:
                    self.log.warning("Min/Max read is repeated (%s)", exc.reason)
                    self._wait_online()
            entries.append({
                "member": member,
                "index": index,
                "switch": switch_state(sw),
                "raw": result["raw"],
                "status": result["status"],
            })
            since_save += 1
            if since_save >= REREAD_SAVE_EVERY:
                self._save()
                since_save = 0
        self._save()
        self.log.info("Min/Max done")

    # -- Connection / waiting ------------------------------------------
    def _wait_online(self) -> None:
        self.went_offline = False
        self.log.info("Waiting for availability online")
        if not self._wait_until(lambda: self.online, self.timing.online_wait_s, guard=False):
            raise RunAbort(EXIT_OFFLINE, "ESP not online")
        self._last_progress = self.clock()
        self.log.info("ESP online")

    def _wait_until(self, cond, timeout: float | None = None, *,
                    stall: bool = False, guard: bool = True) -> bool:
        """Process events until cond() is true or timeout expires.

        guard: raise _Retry on offline message. stall: raise _Retry after 60 s without progress.
        """
        start = self.clock()
        while True:
            if cond():
                return True
            if guard and self.went_offline:
                raise _Retry("offline")
            now = self.clock()
            if stall and stalled(self._last_progress, now, self.timing.stall_s):
                raise _Retry("stall")
            wait = 0.2
            if timeout is not None:
                remaining = timeout - (now - start)
                if remaining <= 0:
                    self._pump(0.0)  # still process events already received
                    return cond()
                wait = min(wait, remaining)
            self._pump(wait)

    def _pump(self, wait: float) -> None:
        for suffix, data in self.transport.poll(wait):
            self._dispatch(suffix, data)

    def _publish(self, suffix: str, payload: dict) -> None:
        self.transport.publish(suffix, payload)

    def _save(self) -> None:
        save_state(self.state_path, self.state)

    # -- Incoming messages ---------------------------------------------
    def _dispatch(self, suffix: str, data) -> None:
        if self.prod and suffix in PROD_SUFFIXES:
            self._dispatch_prod(suffix, data)
            return
        if suffix == "availability":
            if data == "online":
                self.online = True
            elif data == "offline":
                self.online = False
                self.went_offline = True
            self.log.info("availability: %s", data)
            return

        if suffix == "status":
            self._write_status(data)
            reason = bus_problem(self._last_twai, data)
            err = _as_int(data.get("twai_err"), None)
            if err is not None:
                self._last_twai = err
            if reason is not None:
                raise RunAbort(EXIT_BUS, f"Bus problem ({reason}), automatic run stopped")
            return

        if suffix == "rx":
            if self._collect_passive:
                self._passive |= passive_senders(data)
            return

        if suffix == "scan/progress":
            self._last_progress = self.clock()
            if data.get("state") == "done":
                self._scan_done = True
            return

        if suffix == "scan/result":
            try:
                key = (parse_hex(data["member"]), parse_switch(data.get("switch")))
                self._scan_results.setdefault(key, []).append(data)
            except (KeyError, TypeError, ValueError):
                pass
            return

        if suffix == "probe/progress":
            self._last_progress = self.clock()
            if data.get("state") == "done":
                self._probe_done = True
            return

        if suffix == "probe/result":
            self._probe_items.extend(data.get("items", []))
            return

        if suffix == "read/result":
            try:
                key = (parse_hex(data["member"]), parse_hex(data["index"]),
                       parse_switch(data.get("switch")))
                self._read_results[key] = data
            except (KeyError, TypeError, ValueError):
                pass

    # -- Production mode (prod_api) ------------------------------------
    def _send_text(self, topic: str, text: str) -> None:
        self.transport.publish_text(topic, text)

    def _sniff(self, on: bool) -> None:
        if self.prod:
            self._send_text(pa.TOPIC_CAN_DEBUG_SET, "on" if on else "off")
        else:
            self._publish("cmd/sniff", {"on": on, "ids": [], "types": []})

    def _note_skipped(self, text: str) -> None:
        skipped = self.state.setdefault("skipped", [])
        if text not in skipped:
            skipped.append(text)

    def _skip_task(self, task: dict, text: str) -> None:
        task["done"] = True
        task["skipped"] = text
        self._note_skipped(text)
        self.log.warning("Partial scan %s %s %s-%s skipped: %s", task["stage"],
                         task["member"], task["from"], task["to"], text)
        self._save()

    def _prod_begin(self, mode: str, name: str | None, switch: int | None) -> None:
        self._prod_expect = {"mode": mode, "name": name.lower() if name else None,
                             "switch": switch}
        self._prod_armed = False
        self._prod_done = False
        self._prod_hits = []
        self._prod_members = []

    def _prod_end(self) -> None:
        self._prod_expect = None
        self._prod_armed = False

    def _prod_match(self, mode, member) -> bool:
        exp = self._prod_expect
        if exp is None or mode != exp["mode"]:
            return False
        return exp["name"] is None or str(member).lower() == exp["name"]

    def _prod_status(self, data: dict) -> None:
        if self._prod_expect is None:
            return  # no own scan active: ignore retained leftovers
        if not self._prod_armed:
            if data.get("state") == "running" and self._prod_match(data.get("mode"),
                                                                   data.get("member")):
                self._prod_armed = True
                self._last_progress = self.clock()
            return
        self._last_progress = self.clock()
        if data.get("state") in ("done", "stopped") and self._prod_match(data.get("mode"),
                                                                         data.get("member")):
            self._prod_done = True
            self._prod_expect = None
            self._prod_armed = False

    def _prod_hit(self, data: dict) -> None:
        exp = self._prod_expect
        if exp is None or not self._prod_armed or exp["mode"] != "range":
            return
        if str(data.get("name", "")).lower() != exp["name"] or data.get("switch") != exp["switch"]:
            return
        self._prod_hits.append(data)

    def _prod_member(self, data: dict) -> None:
        exp = self._prod_expect
        if exp is None or not self._prod_armed or exp["mode"] != "members":
            return
        self._prod_members.append(data)

    def _dispatch_prod(self, suffix: str, data) -> None:
        if suffix == "scan/status":
            self._prod_status(data)
            self._write_status(data)
        elif suffix == "scan/hit":
            self._prod_hit(data)
        elif suffix == "scan/member":
            self._prod_member(data)
        elif suffix == "raw/result" and data.get("op") == "read":
            self._read_results[(data["id"], data["index"], data["switch"])] = data

    def _probe_prod(self, lo: int, hi: int) -> list[dict]:
        """Member search via scan/members. Reply 0x8000 = unsupported (member responds)."""
        self._prod_begin("members", None, None)
        self._last_progress = self.clock()
        try:
            self._send_text(pa.TOPIC_SCAN_MEMBERS, pa.members_payload(lo, hi, PROBE_INDEX))
            self._wait_until(lambda: self._prod_done, stall=True)
            found = list(self._prod_members)
        finally:
            self._prod_end()
        return [{"id": hex_member(m["id"]),
                 "status": "unsupported" if m["raw"] == 0x8000 else "ok",
                 "raw": m["raw"]} for m in found]

    def _run_prod_scan(self, task: dict) -> None:
        member = parse_hex(task["member"])
        sw = entry_switch(task)
        self._prod_begin("range", pa.member_topic_name(member), sw)
        self._last_progress = self.clock()
        try:
            self._send_text(pa.TOPIC_SCAN_START, pa.scan_start_payload(
                member, parse_hex(task["from"]), parse_hex(task["to"]), sw))
            self._wait_until(lambda: self._prod_done, stall=True)
            hits = list(self._prod_hits)
        finally:
            self._prod_end()
        if not hits:
            self.log.info("No hit for %s", task["member"])
        for hit in hits:
            task["items"].append({"index": hex_index(hit["index"]), "raw": hit["raw"],
                                  "status": "ok"})
        task["done"] = True

    def _read_one_prod(self, member: str, index: str, switch: int) -> dict:
        mid = parse_hex(member)
        idx = parse_hex(index)
        key = (mid, idx, switch)
        res = None
        for attempt in (1, 2):
            self._read_results.pop(key, None)
            self._send_text(pa.TOPIC_RAW_READ, pa.raw_read_payload(mid, idx, switch))
            self._wait_until(lambda: key in self._read_results, self.timing.read_timeout_s)
            res = self._read_results.pop(key, None)
            if res is None or res.get("status") != "busy" or attempt == 2:
                break
            self.log.info("Read %s %s busy, retrying", member, index)
            self._wait_until(lambda: False, self.timing.busy_pause_s, guard=False)
        self._wait_until(lambda: False, self.timing.gap_s, guard=False)
        if res is None:
            return {"raw": None, "status": "timeout"}
        return {"raw": res.get("raw"), "status": res.get("status")}

    def _write_status(self, data: dict) -> None:
        if self.status_fp is None:
            return
        line = {"pc": now_iso(), "status": data}
        self.status_fp.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.status_fp.flush()


def execute(transport, state: dict, *, state_path: str, timing: Timing,
            log: logging.Logger, smoke: bool, delay_ms: int, status_fp=None,
            clock=time.monotonic, d_members=D_MEMBERS, extra_members=EXTRA_MEMBERS,
            minmax: bool = False, prod: bool = False) -> int:
    runner = Runner(transport, state, state_path=state_path, timing=timing, log=log,
                    smoke=smoke, delay_ms=delay_ms, status_fp=status_fp, clock=clock,
                    d_members=d_members, extra_members=extra_members, minmax=minmax,
                    prod=prod)
    return runner.execute()


# ----------------------------------------------------------------------
# Real MQTT transport layer (paho-mqtt 2.x)
# ----------------------------------------------------------------------
class PahoTransport:
    """publish() sends; poll() delivers events (suffix, data). rx frames are written to rx_fp in the paho thread.

    prod=True: production API (prod_api), publish_text() sends raw text to full topics,
    poll() delivers availability, rx, scan/status, scan/hit, scan/member and raw/result.
    """

    _prod = False  # class default, if an object is built without __init__ (tests)

    def __init__(self, cfg: BrokerConfig, rx_fp, log: logging.Logger, prod: bool = False):
        self._prod = prod
        self._cfg = cfg
        self._rx = rx_fp
        self._log = log
        self._queue: queue.Queue = queue.Queue()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="", clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        if cfg.user:
            self._client.username_pw_set(cfg.user, cfg.password)

    def connect(self) -> None:
        self._client.connect(self._cfg.host, self._cfg.port, keepalive=30)
        self._client.loop_start()

    def close(self) -> None:
        try:
            self._client.disconnect()
        except Exception:
            pass
        try:
            self._client.loop_stop()
        except Exception:
            pass

    def publish(self, suffix: str, payload: dict) -> None:
        self._client.publish(PREFIX + suffix, json.dumps(payload), qos=0)

    def publish_text(self, topic: str, text: str) -> None:
        """Raw text to a full topic (production mode)."""
        self._client.publish(topic, text, qos=0)

    def poll(self, timeout: float) -> list[tuple[str, object]]:
        events = []
        try:
            events.append(self._queue.get(timeout=max(0.0, timeout)))
        except queue.Empty:
            return events
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        # paho 2.x: ReasonCode (is_failure); plain int (0 = ok) still accepted.
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            self._log.error("Broker rejects connection (%s)", reason_code)
            return
        topics = PROD_TOPICS if self._prod else tuple(PREFIX + t for t in RX_TOPICS)
        for topic in topics:
            client.subscribe(topic, qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            if self._prod:
                self._on_prod_message(msg)
                return
            if not msg.topic.startswith(PREFIX):
                return
            suffix = msg.topic[len(PREFIX):]
            if suffix not in RX_TOPICS:
                return
            text = msg.payload.decode("utf-8")
            if suffix == "availability":
                self._queue.put(("availability", text.strip()))
                return
            data = json.loads(text)
            if suffix == "rx":
                self._queue_rx(data)
                return
            self._queue.put((suffix, data))
        except Exception:
            self._log.debug("Invalid message ignored")

    def _queue_rx(self, data) -> None:
        frames = []
        for item in data:
            self._rx.write(json.dumps(item, separators=(",", ":")) + "\n")
            frame = {
                "t": int(item["t"]),
                "id": parse_hex(item["id"]),
                "data": bytes.fromhex(item["d"]),
                "tx": bool(item["tx"]),
            }
            u = int(item.get("u") or 0)
            if u > 0:
                frame["ts"] = round(u / 1000, 3)
            frames.append(frame)
        self._rx.flush()
        if frames:
            self._queue.put(("rx", frames))

    def _on_prod_message(self, msg) -> None:
        topic = msg.topic
        text = msg.payload.decode("utf-8")
        if topic == PROD_TOPICS[0]:
            self._queue.put(("availability", text.strip()))
        elif topic == pa.TOPIC_CAN_DEBUG_FRAMES:
            self._queue_rx(json.loads(text))
        elif topic == pa.TOPIC_RAW_RESULT:
            res = pa.parse_raw_result(text)
            if res is not None:
                self._queue.put(("raw/result", res))
        elif topic == pa.TOPIC_SCAN_STATUS:
            res = pa.parse_scan_status(text)
            if res is not None:
                self._queue.put(("scan/status", res))
        else:
            can_id = pa.parse_member_topic(topic)
            if can_id is not None:
                res = pa.parse_member_result(text)
                if res is not None:
                    self._queue.put(("scan/member", {"id": can_id, **res}))
                return
            parsed = pa.parse_scan_result_topic(topic)
            if parsed is not None:
                res = pa.parse_scan_result(text)
                if res is not None:
                    name, index, switch = parsed
                    self._queue.put(("scan/hit", {"name": name, "index": index,
                                                  "switch": switch, "raw": res["raw"],
                                                  "value": res["value"]}))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _parse_member_ids(text: str) -> tuple[int, ...]:
    out = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = parse_hex(part)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a hex ID: {part}") from None
        if not 0 <= value <= 0x7FF or value == PC_CAN_ID:
            raise argparse.ArgumentTypeError(f"invalid member ID: {part}")
        out.add(value)
    return tuple(sorted(out))


def parse_d_members(text: str):
    """Hex IDs comma-separated; 'all' = None (stage D for all)."""
    if text.strip().lower() == "all":
        return None
    return _parse_member_ids(text)


def parse_extra_members(text: str) -> tuple[int, ...]:
    """Hex IDs comma-separated; 'none' = no extra members."""
    if text.strip().lower() == "none":
        return ()
    return _parse_member_ids(text)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="mk1-debug automatic run (headless)")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--secrets", default=DEFAULT_SECRETS, help="secrets.yaml")
    ap.add_argument("--delay", type=int, default=MIN_DELAY_MS, help="pause between requests in ms (min. 100)")
    ap.add_argument("--passive", type=float, default=DEFAULT_PASSIVE_S, help="seconds to collect passively")
    ap.add_argument("--resume", metavar="STATE_JSON", help="resume run from _state.json")
    ap.add_argument("--smoke", action="store_true", help="short test with small ranges")
    ap.add_argument("--d-members", type=parse_d_members, default=D_MEMBERS,
                    help="stage D only for these members (hex, comma-separated; 'all' = all)")
    ap.add_argument("--extra-members", type=parse_extra_members, default=EXTRA_MEMBERS,
                    help="extra members for A1/A2/B/C (hex, comma-separated; 'none' = none)")
    ap.add_argument("--minmax", action="store_true",
                    help="after the rereads, read min (switch f8) and max (switch f9) per hit")
    ap.add_argument("--fw", choices=(FW_DEBUG, FW_PROD), default=FW_DEBUG,
                    help="firmware: debug (stiebel-debug) or prod (production API heatingpump)")
    return ap


def make_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("auto_runner")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = out_dir / f"auto_{stamp}"
    log = make_logger(f"{prefix}_runner.log")
    delay_ms = max(MIN_DELAY_MS, args.delay)

    try:
        cfg = load_broker_config(args.secrets)
    except (OSError, ConfigError) as exc:
        log.error("Configuration: %s", exc)
        return EXIT_ERROR

    if args.resume:
        try:
            state = load_state(args.resume)
        except (OSError, ValueError) as exc:
            log.error("State not readable: %s", exc)
            return EXIT_ERROR
        state_path = args.resume
        smoke = bool(state.get("smoke", args.smoke))
        fw = state.get("fw") or FW_DEBUG
        log.info("Resuming from %s", state_path)
    else:
        fw = args.fw
        state = new_state(delay_ms, args.smoke, fw)
        state_path = f"{prefix}_state.json"
        smoke = args.smoke
    prod = fw == FW_PROD
    log.info("Firmware: %s", fw)

    timing = Timing(passive_s=max(0.0, args.passive), gap_s=delay_ms / 1000.0)
    rx_fp = open(f"{prefix}_rx.jsonl", "a", encoding="utf-8", buffering=1)
    status_fp = open(f"{prefix}_status.jsonl", "a", encoding="utf-8", buffering=1)
    transport = PahoTransport(cfg, rx_fp, log, prod=prod)
    try:
        try:
            transport.connect()
        except Exception as exc:
            log.error("Broker not reachable (%s)", type(exc).__name__)
            return EXIT_ERROR
        return execute(transport, state, state_path=state_path, timing=timing, log=log,
                       smoke=smoke, delay_ms=delay_ms, status_fp=status_fp,
                       d_members=args.d_members, extra_members=args.extra_members,
                       minmax=args.minmax, prod=prod)
    finally:
        transport.close()
        rx_fp.close()
        status_fp.close()


if __name__ == "__main__":
    sys.exit(main())
