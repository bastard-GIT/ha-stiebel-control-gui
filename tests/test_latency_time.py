"""Time source in tools/latency.py: u (Unix ms) if > 0, otherwise t (ms since boot). Never mixed within a pair."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import latency  # noqa: E402

BASE_U = 1_700_000_000_000


def _line(t, tx, d, id_hex, u=None):
    r = {"t": t, "id": id_hex, "d": d.hex(), "tx": tx}
    if u is not None:
        r["u"] = u
    return json.dumps(r)


REQ = bytes([0x31, 0x80, 0x10, 0x00, 0x00])   # request to member 0x180
RESP = bytes([0x32, 0x80, 0x10, 0x00, 0x00])  # response from member 0x180


def _write(tmp_path, lines):
    p = tmp_path / "auto_test_rx.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_latency_uses_u_difference_in_ms(tmp_path):
    path = _write(tmp_path, [
        _line(1000, True, REQ, "600", u=BASE_U),
        _line(1100, False, RESP, "180", u=BASE_U + 350),
    ])
    lat, sent = latency.latencies([path])
    assert lat[0x180] == [350]
    assert sent[0x180] == 1


def test_latency_without_u_uses_t(tmp_path):
    path = _write(tmp_path, [
        _line(1000, True, REQ, "600"),
        _line(1250, False, RESP, "180"),
    ])
    lat, _ = latency.latencies([path])
    assert lat[0x180] == [250]


def test_latency_u_zero_counts_as_missing(tmp_path):
    path = _write(tmp_path, [
        _line(1000, True, REQ, "600", u=0),
        _line(1250, False, RESP, "180", u=0),
    ])
    lat, _ = latency.latencies([path])
    assert lat[0x180] == [250]


def test_latency_mixed_pair_falls_back_to_t(tmp_path):
    # request has u, response does not: both use t, even if the u difference would differ
    path = _write(tmp_path, [
        _line(1000, True, REQ, "600", u=BASE_U),
        _line(1300, False, RESP, "180"),
    ])
    lat, _ = latency.latencies([path])
    assert lat[0x180] == [300]


def test_latency_mixed_file_sorted_per_pair(tmp_path):
    # first pair with u, second pair without u (old lines) in the same file
    path = _write(tmp_path, [
        _line(1000, True, REQ, "600", u=BASE_U),
        _line(1100, False, RESP, "180", u=BASE_U + 120),
        _line(5000, True, REQ, "600"),
        _line(5400, False, RESP, "180"),
    ])
    lat, sent = latency.latencies([path])
    assert lat[0x180] == [120, 400]
    assert sent[0x180] == 2
