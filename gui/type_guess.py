"""Type heuristic and evaluation of the automatic scans (no Qt, no paho).

Specification: INTERFACES.md 7.4 and 7.5, plan/mk1-debug-auto-plan.md step 3.
CLI: python -I type_guess.py <state.json>
Output: <prefix>_analyse.csv and <prefix>_analyse.md (prefix = state file without "_state.json").

Notes on the heuristic:
- The scores are plausibility values 0..1, not probabilities.
- Raw value 0x8000 (not supported) is removed before scoring.
- If there are only 0 raw values, all scores are halved (0 is possible almost everywhere).
- Comparison with the clock (et_zeit/et_datum) is not implemented: guess_types gets no time of day.
- et_double_val and et_triple_val are not estimated from single values, but output as a
  pair hint for adjacent indices (_H/_L, _WH/_KWH/_MWH).
- Min/Max (switch 0xF8/0xF9 from state["minmax"]) are never mixed into the raw values. If min
  and max lie within the range of the type, the candidate gets +0.1 (max 1.0).
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parent
if str(GUI_DIR) not in sys.path:  # python -I startet ohne Skriptverzeichnis im Pfad
    sys.path.insert(0, str(GUI_DIR))

from elster import ElsterDb, NOT_SUPPORTED_RAW, decode_value  # noqa: E402

MKGIT_DEFAULT = Path(os.environ.get("ELSTER_TABLE_H", "ElsterTable.h"))  # optional: ElsterTable.h from ha-stiebel-control
_MKGIT_RE = re.compile(
    r'^\s*\{\s*"([^"]+)"\s*,\s*(0[xX][0-9a-fA-F]+)\s*,\s*(et_\w+|0)\s*[,}]'
)
_PAIR_RULES = [("_H", "_L"), ("_WH", "_KWH"), ("_WH", "_MWH")]
SWITCH_DEFAULT = 0xFA
SWITCH_MIN = 0xF8
SWITCH_MAX = 0xF9
MINMAX_BONUS = 0.1


def _num(value) -> int:
    """'0x0112' / '0x180' / int -> int."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text, 10)


def _s16(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


def _sw(value) -> int:
    """Switch from state field ("0xf8", int, missing = 0xFA)."""
    return SWITCH_DEFAULT if value is None else _num(value)


def _minmax_plausible(name: str, lo: int, hi: int) -> bool | None:
    """Are min (lo) and max (hi) within the range of the type? None if the type has no rule.
    Ranges in decimal units: et_dec_val /10 in -50..150, et_cent_val /100 in -10..20,
    et_mil_val /1000 in -5..5."""
    slo, shi = _s16(lo), _s16(hi)
    if name == "et_dec_val":
        return -500 <= slo and shi <= 1500 and slo <= shi
    if name == "et_cent_val":
        return -1000 <= slo and shi <= 2000 and slo <= shi
    if name == "et_mil_val":
        return -5000 <= slo and shi <= 5000 and slo <= shi
    if name == "et_bool":
        return lo in (0, 1) and hi in (0, 1) and lo <= hi
    if name == "et_little_bool":
        return lo in (0x0000, 0x0100) and hi in (0x0000, 0x0100) and lo <= hi
    if name == "et_byte":
        return lo >> 8 == 0 and hi >> 8 == 0 and lo <= hi
    if name == "zaehler":
        return lo <= hi
    return None


@lru_cache(maxsize=1)
def _betriebsart_count() -> int:
    return len(ElsterDb().betriebsart_list)


@lru_cache(maxsize=1)
def _reference_db() -> ElsterDb:
    return ElsterDb()


def load_mkgit_table(path: str | Path = MKGIT_DEFAULT) -> dict[int, tuple[str, str]]:
    """Read mk-git ElsterTable.h (read only). Index -> (name, type).

    Type 0 is recorded as et_default. For duplicate indices the first entry wins.
    """
    table: dict[int, tuple[str, str]] = {}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        m = _MKGIT_RE.match(line)
        if not m:
            continue
        name, index_hex, type_name = m.group(1), m.group(2), m.group(3)
        if type_name == "0":
            type_name = "et_default"
        table.setdefault(int(index_hex, 16), (name, type_name))
    return table


def guess_types(raws: list[int],
                minmax: tuple[int, int] | None = None) -> list[tuple[str, float, str]]:
    """Type candidates for a sequence of raw values (chronological).

    minmax: optional (min_raw, max_raw) from switch 0xF8/0xF9. If the type fits min and max,
    its score rises by MINMAX_BONUS (max 1.0).
    Returns: [(type, score 0..1, decoded example)], sorted by score descending.
    Empty list if nothing remains after removing 0x8000.
    """
    values = [int(r) & 0xFFFF for r in raws if int(r) & 0xFFFF != NOT_SUPPORTED_RAW]
    if not values:
        return []

    distinct = set(values)
    all_zero = distinct == {0}
    betriebsart_n = _betriebsart_count()
    candidates: list[tuple[str, float]] = []

    def add(name: str, score: float) -> None:
        if score > 0:
            candidates.append((name, score))

    # et_bool / et_little_bool: nur 0/1 bzw. 0/0x0100
    if distinct <= {0x0000, 0x0001}:
        add("et_bool", 0.9 if 0x0001 in distinct else 0.3)
    if distinct <= {0x0000, 0x0100}:
        add("et_little_bool", 0.9 if 0x0100 in distinct else 0.3)

    # et_dec_val: int16 / 10 in range -50 ... 150 (temperatures)
    if all(-500 <= _s16(v) <= 1500 for v in values):
        add("et_dec_val", 0.8)
    # et_cent_val / et_mil_val: int16 / 100 bzw. / 1000, Plausibilitaet -10 ... 20 bzw. -5 ... 5
    if all(-1000 <= _s16(v) <= 2000 for v in values):
        add("et_cent_val", 0.4)
    if all(-5000 <= _s16(v) <= 5000 for v in values):
        add("et_mil_val", 0.3)

    # et_byte: high byte 0, value <= 255
    if all(v >> 8 == 0 for v in values):
        add("et_byte", 0.5)

    # et_little_endian: byte-swapped smaller than direct (0x0100 = 1)
    swapped = [((v & 0xFF) << 8) | (v >> 8) for v in values]
    if all(sw < v for sw, v in zip(swapped, values)):
        add("et_little_endian", 0.6)

    # et_zeit (hour:minute) and et_datum (day.month) with byte plausibility
    if all((v >> 8) < 24 and (v & 0xFF) < 60 for v in values):
        add("et_zeit", 0.4)
    if all(1 <= (v >> 8) <= 31 and 1 <= (v & 0xFF) <= 12 for v in values):
        add("et_datum", 0.3)

    # et_betriebsart: Low-Byte 0, High-Byte in betriebsart_list
    if all((v & 0xFF) == 0 and (v >> 8) < betriebsart_n for v in values):
        add("et_betriebsart", 0.7)

    # counter: only rising over several raw values (order as passed)
    if len(values) >= 2:
        non_decreasing = all(b >= a for a, b in zip(values, values[1:]))
        if non_decreasing and values[-1] > values[0]:
            add("zaehler", 0.9 if len(values) >= 3 else 0.6)

    factor = 0.5 if all_zero else 1.0
    result: list[tuple[str, float, str]] = []
    for name, score in candidates:
        if name == "zaehler":
            sample = str(values[-1])
        else:
            sample = decode_value(name, values[-1])
        final = min(score * factor, 1.0)
        if minmax is not None and _minmax_plausible(name, minmax[0] & 0xFFFF,
                                                    minmax[1] & 0xFFFF):
            final = min(final + MINMAX_BONUS, 1.0)
        result.append((name, round(final, 2), sample))
    result.sort(key=lambda item: (-item[1], item[0]))
    return result


def _parse_state(state: dict) -> tuple[dict, dict, dict, dict]:
    """Liefert:
    raws[(member, index, switch)] -> list of scan and reread raw values (ok); switch 0xFA = default,
    stage_items[(member, stage, format, switch)] -> {index: (status, raw)},
    stage_counts[(member, stage, format, switch)] -> {"tasks", "done", "ok", "unsupported", "ohne_antwort"},
    minmax[(member, index)] -> {"min": raw, "max": raw} from state["minmax"] (status ok only).
    """
    raws: dict[tuple[int, int, int], list[int]] = {}
    stage_items: dict[tuple[int, str, str, int], dict[int, tuple[str, int]]] = {}
    stage_counts: dict[tuple[int, str, str, int], dict[str, int]] = {}
    minmax: dict[tuple[int, int], dict[str, int]] = {}

    for task in state.get("tasks", []):
        member = _num(task["member"])
        sw = _sw(task.get("switch"))
        key = (member, task.get("stage", "?"), task.get("format", "auto"), sw)
        counts = stage_counts.setdefault(
            key, {"tasks": 0, "done": 0, "ok": 0, "unsupported": 0, "ohne_antwort": 0}
        )
        counts["tasks"] += 1
        if task.get("done"):
            counts["done"] += 1
        span = _num(task["to"]) - _num(task["from"]) + 1
        items_map = stage_items.setdefault(key, {})
        answered = 0
        for item in task.get("items", []):
            status = item.get("status", "")
            index = _num(item["index"])
            raw = _num(item["raw"])
            items_map[index] = (status, raw)
            if status == "ok":
                counts["ok"] += 1
                raws.setdefault((member, index, sw), []).append(raw)
                answered += 1
            elif status == "unsupported":
                counts["unsupported"] += 1
                answered += 1
        if task.get("done"):
            counts["ohne_antwort"] += max(0, span - answered)

    for rr in state.get("rereads", []):
        if rr.get("status") != "ok":
            continue
        key = (_num(rr["member"]), _num(rr["index"]), _sw(rr.get("switch")))
        raws.setdefault(key, []).append(_num(rr["raw"]))

    for entry in state.get("minmax", []):
        if entry.get("status") != "ok" or entry.get("raw") is None:
            continue
        sw = _sw(entry.get("switch"))
        if sw == SWITCH_MIN:
            side = "min"
        elif sw == SWITCH_MAX:
            side = "max"
        else:
            continue
        pair = minmax.setdefault((_num(entry["member"]), _num(entry["index"])), {})
        pair[side] = _num(entry["raw"])

    return raws, stage_items, stage_counts, minmax


def _pair_hints(names: dict[tuple[int, int], str], present: set[tuple[int, int]]) -> list[dict]:
    hints = []
    for (member, index), name in sorted(names.items()):
        nxt = (member, index + 1)
        if nxt not in names or nxt not in present or (member, index) not in present:
            continue
        name_b = names[nxt]
        for suffix_a, suffix_b in _PAIR_RULES:
            if name.endswith(suffix_a) and name_b.endswith(suffix_b):
                base_a = name[: -len(suffix_a)]
                base_b = name_b[: -len(suffix_b)]
                if base_a == base_b:
                    hints.append({
                        "member": member, "index": index, "index_b": index + 1,
                        "name_a": name, "name_b": name_b,
                        "regel": f"{suffix_a}/{suffix_b}",
                    })
                    break
    return hints


def _hex4(value: int) -> str:
    return f"0x{value:04x}"


def _md_table(headers: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def analyse(state_json_path: str, out_prefix: str) -> tuple[str, str]:
    """Evaluate state.json. Writes <prefix>_analyse.csv and <prefix>_analyse.md."""
    state_path = Path(state_json_path)
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    db = _reference_db()
    mkgit = load_mkgit_table()
    mkgit_found = bool(mkgit)
    raws, stage_items, stage_counts, minmax = _parse_state(state)
    # table rows only for the default switch; min/max are in the columns min/max
    base_keys = sorted({(m, i) for (m, i, sw) in raws if sw == SWITCH_DEFAULT})

    # names and types from reference and mk-git
    ref_name: dict[int, str] = {}
    ref_type: dict[int, str] = {}
    for index in {idx for (_m, idx) in base_keys}:
        sig = db.signal_by_index(index)
        if sig:
            ref_name[index] = sig["name"]
            ref_type[index] = sig["type"]

    rows = []
    names_for_pairs: dict[tuple[int, int], str] = {}
    for (member, index) in base_keys:
        values = raws[(member, index, SWITCH_DEFAULT)]
        name_mk, type_mk = mkgit.get(index, ("", ""))
        name_ref = ref_name.get(index, "")
        type_ref = ref_type.get(index, "")
        if name_ref:
            names_for_pairs[(member, index)] = name_ref
        elif name_mk:
            names_for_pairs[(member, index)] = name_mk
        if len(values) >= 2:
            konstant = "yes" if len(set(values)) == 1 else "no"
        else:
            konstant = "unknown"
        mm = minmax.get((member, index), {})
        pair = (mm["min"], mm["max"]) if "min" in mm and "max" in mm else None
        cands = guess_types(values, pair)
        conflict = ""
        if type_ref and type_mk and type_ref != type_mk and type_mk != "et_default" \
                and type_ref != "et_default":
            conflict = f"yes: ref={type_ref} mkgit={type_mk}"
        rows.append({
            "member": f"0x{member:03x}",
            "member_name": db.member_name(member),
            "index": _hex4(index),
            "name_ref": name_ref,
            "type_ref": type_ref,
            "name_mkgit": name_mk,
            "type_mkgit": type_mk,
            "raws": ",".join(_hex4(v) for v in values),
            "min": _hex4(mm["min"]) if "min" in mm else "",
            "max": _hex4(mm["max"]) if "max" in mm else "",
            "konstant": konstant,
            "kandidaten": " | ".join(f"{n}:{s:.2f} ({smp})" for n, s, smp in cands),
            "konflikt": conflict,
            "_cands": cands,
            "_values": values,
            "_minmax": pair,
            "_member_int": member,
            "_index_int": index,
        })

    csv_path = f"{out_prefix}_analyse.csv"
    csv_cols = ["member", "member_name", "index", "name_ref", "type_ref", "name_mkgit",
                "type_mkgit", "raws", "min", "max", "konstant", "kandidaten", "konflikt"]
    csv_headers = ["member", "member_name", "index", "name_ref", "type_ref", "name_mkgit",
                   "type_mkgit", "raws", "min", "max", "constant", "candidates", "conflict"]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(csv_headers)
        for r in rows:
            writer.writerow([r[c] for c in csv_cols])

    present = {(r["_member_int"], r["_index_int"]) for r in rows}
    md = _build_markdown(state_path, state, rows, stage_items, stage_counts,
                         names_for_pairs, present, mkgit_found)
    md_path = f"{out_prefix}_analyse.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    return csv_path, md_path


def _build_markdown(state_path, state, rows, stage_items, stage_counts,
                    names_for_pairs, present, mkgit_found) -> list[str]:
    db = _reference_db()
    md: list[str] = []
    md.append(f"# Automatic analysis: {state_path.name}")
    md.append("")
    md.append(f"Status: started {state.get('started')}, finished {state.get('finished')}, "
              f"pause {state.get('delay_ms')} ms")
    md.append("")

    members = sorted({_num(m) for m in state.get("members", [])}
                     | {r["_member_int"] for r in rows}
                     | {k[0] for k in stage_counts})
    md.append("## Summary")
    md.append("")
    md.append(f"- Members: {', '.join(f'0x{m:03x} {db.member_name(m)}' for m in members) or '-'}")
    silent = state.get("silent", [])
    md.append(f"- Passively seen, without probe response (silent): {', '.join(silent) or '-'}")
    konst = sum(1 for r in rows if r["konstant"] == "yes")
    veraend = sum(1 for r in rows if r["konstant"] == "no")
    konflikte = sum(1 for r in rows if r["konflikt"])
    unbekannt = sum(1 for r in rows if not r["name_ref"] and not r["name_mkgit"])
    md.append(f"- Indices with response not 0x8000: {len(rows)} "
              f"(constant {konst}, changing {veraend}, without repeat reading "
              f"{len(rows) - konst - veraend})")
    md.append(f"- Type conflicts reference vs. mk-git: {konflikte}")
    md.append(f"- Without names (neither reference nor mk-git): {unbekannt}")
    md.append("")

    md.append("## Hits per stage and member")
    md.append("")
    stage_rows = []
    for key in sorted(stage_counts):
        member, stage, fmt, sw = key
        c = stage_counts[key]
        stage_rows.append([f"0x{member:03x}", stage, fmt, f"0x{sw:02x}", c["tasks"], c["done"],
                           c["ok"], c["unsupported"], c["ohne_antwort"]])
    if stage_rows:
        md += _md_table(["Member", "Stage", "Format", "Switch", "Partial scans", "done", "ok",
                         "unsupported (0x8000)", "no response"], stage_rows)
    else:
        md.append("No scan tasks in state.")
    md.append("")

    md.append("## Comparison short/long for 0x00-0xFF")
    md.append("")
    any_cmp = False
    for member in members:
        short = stage_items.get((member, "A1", "short", SWITCH_DEFAULT))
        long_ = stage_items.get((member, "A2", "long", SWITCH_DEFAULT))
        if short is None and long_ is None:
            continue
        any_cmp = True
        if short is None or long_ is None:
            fehlt = "A1 (short)" if short is None else "A2 (long)"
            md.append(f"**Member 0x{member:03x} {db.member_name(member)}**: stage {fehlt} "
                      f"missing, no comparison possible")
            md.append("")
            continue
        same = diff = only_one = 0
        diff_list = []
        for index in range(0x100):
            s = short.get(index) if short else None
            l_ = long_.get(index) if long_ else None
            s_ok = s is not None and s[0] in ("ok", "unsupported")
            l_ok = l_ is not None and l_[0] in ("ok", "unsupported")
            if s_ok and l_ok:
                if s[1] == l_[1]:
                    same += 1
                else:
                    diff += 1
                    diff_list.append(f"{_hex4(index)} short={_hex4(s[1])} long={_hex4(l_[1])}")
            elif s_ok or l_ok:
                only_one += 1
        md.append(f"**Member 0x{member:03x} {db.member_name(member)}**: same {same}, "
                  f"differs {diff}, in only one format {only_one}")
        md.append("")
        if diff_list:
            md.append("Differs: " + "; ".join(diff_list))
            md.append("")
    if not any_cmp:
        md.append("No stages A1/A2 in state, no comparison possible.")
        md.append("")

    md.append("## Min/Max (switch 0xF8/0xF9)")
    md.append("")
    mm_rows = []
    for r in rows:
        if not r["min"] and not r["max"]:
            continue
        pair = r["_minmax"]
        plaus = "-"
        if pair is not None:
            names = [n for n, _s, _x in r["_cands"] if _minmax_plausible(n, *pair)]
            plaus = ", ".join(names) or "-"
        mm_rows.append([r["member"], r["index"], r["name_ref"] or r["name_mkgit"],
                        r["min"] or "-", r["max"] or "-", plaus])
    if mm_rows:
        md += _md_table(["Member", "Index", "Name", "min", "max", "Type fits (bonus)"], mm_rows)
    else:
        md.append("No min/max values in state.")
    md.append("")

    md.append("## Type conflicts reference vs. mk-git")
    md.append("")
    conf_rows = [[r["member"], r["index"], r["name_ref"] or r["name_mkgit"], r["type_ref"],
                  r["type_mkgit"], r["raws"]] for r in rows if r["konflikt"]]
    if conf_rows:
        md += _md_table(["Member", "Index", "Name", "Type reference", "Type mk-git",
                         "Raw values"], conf_rows)
    else:
        md.append("None.")
    if not mkgit_found:
        md.append("")
        md.append("Note: mk-git ElsterTable.h not found, no comparison.")
    md.append("")

    md.append("## Unknown indices with candidates")
    md.append("")
    unk_rows = []
    for r in rows:
        if r["name_ref"] or r["name_mkgit"]:
            continue
        top = " | ".join(f"{n}:{s:.2f} ({smp})" for n, s, smp in r["_cands"][:3])
        unk_rows.append([r["member"], r["index"], r["raws"], r["konstant"], top or "-"])
    if unk_rows:
        md += _md_table(["Member", "Index", "Raw values", "constant", "Top candidates"], unk_rows)
    else:
        md.append("None.")
    md.append("")

    md.append("## Pair hints (adjacent indices)")
    md.append("")
    hints = _pair_hints(names_for_pairs, present)
    if hints:
        md += _md_table(
            ["Member", "Index", "Index+1", "Name", "Name+1", "Rule"],
            [[f"0x{h['member']:03x}", _hex4(h["index"]), _hex4(h["index_b"]),
              h["name_a"], h["name_b"], h["regel"]] for h in hints],
        )
    else:
        md.append("None.")
    md.append("")
    return md


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python -I type_guess.py <state.json>", file=sys.stderr)
        return 2
    state_path = Path(argv[1])
    base = state_path.name
    if base.endswith("_state.json"):
        base = base[: -len("_state.json")]
    else:
        base = state_path.stem
    out_prefix = str(state_path.with_name(base))
    csv_path, md_path = analyse(str(state_path), out_prefix)
    print(f"CSV: {csv_path}")
    print(f"MD:  {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
