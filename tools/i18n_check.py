"""Check the translation catalogs against the source.

Collects every string literal that is the first argument of _("...") or N_("...") in gui/*.py and compares
it with gui/locales/<code>.json.

Usage (repo root):
    python -I tools/i18n_check.py            # report for all catalogs, exit 1 on problems
    python -I tools/i18n_check.py --keys     # print all source keys as JSON (template for a new catalog)
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = ROOT / "gui"
LOCALES_DIR = GUI_DIR / "locales"
MARKERS = {"_", "N_"}
PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def collect_keys(gui_dir: Path = GUI_DIR) -> dict[str, list[str]]:
    """key -> list of 'file:line'. Non-literal arguments are reported under the key None-marker '<dynamic>'."""
    keys: dict[str, list[str]] = {}
    for path in sorted(gui_dir.glob("*.py")):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in MARKERS and node.args):
                continue
            arg = node.args[0]
            where = f"{path.name}:{node.lineno}"
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.setdefault(arg.value, []).append(where)
            else:
                keys.setdefault("<dynamic>", []).append(where)
    return keys


def placeholders(text: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(text))


def check(code: str, keys: dict[str, list[str]]) -> list[str]:
    """Problems of one catalog (empty list = ok)."""
    problems: list[str] = []
    path = LOCALES_DIR / f"{code}.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    source = {k for k in keys if k != "<dynamic>"}
    for key in sorted(source - set(catalog)):
        problems.append(f"{code}: missing translation: {key!r} ({keys[key][0]})")
    for key in sorted(set(catalog) - source):
        problems.append(f"{code}: unused key: {key!r}")
    for key, value in catalog.items():
        if key in source and placeholders(key) != placeholders(value):
            problems.append(f"{code}: placeholders differ: {key!r}")
    return problems


def main(argv: list[str]) -> int:
    keys = collect_keys()
    if "--keys" in argv:
        print(json.dumps({k: "" for k in sorted(keys) if k != "<dynamic>"}, indent=2, ensure_ascii=False))
        return 0
    problems = [f"dynamic argument (not extractable): {w}" for w in keys.get("<dynamic>", [])]
    for path in sorted(LOCALES_DIR.glob("*.json")):
        problems += check(path.stem, keys)
    for line in problems:
        print(line)
    print(f"{len(keys) - ('<dynamic>' in keys)} source keys, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
