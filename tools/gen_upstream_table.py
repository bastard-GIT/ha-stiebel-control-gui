"""Generate ElsterTable.<model>.json from the pinned upstream sources (see gui/upstream_table.py).

Examples:
  python -I tools/gen_upstream_table.py --list
  python -I tools/gen_upstream_table.py --pin
  python -I tools/gen_upstream_table.py --model wpla7 --out out/ElsterTable.wpla7.json
  python -I tools/gen_upstream_table.py --all --out-dir out

Exit codes: 0 on success, 2 on any error (message on stderr).
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gui"))
import upstream_table as ut  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--model", help="model name, e.g. wpla7")
    mode.add_argument("--all", action="store_true", help="generate all models into --out-dir")
    mode.add_argument("--list", action="store_true", help="list available models")
    mode.add_argument("--pin", action="store_true", help="show the pin and verify the upstream files")
    parser.add_argument("--out", help="output file for --model")
    parser.add_argument("--out-dir", help="output directory for --all")
    parser.add_argument("--indent", type=int, default=1, choices=(1, 2), help="JSON indent (default 1)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.list:
            for name in ut.available_models():
                print(name)
            return 0
        if args.pin:
            info = ut.pin_info()
            ut.verify_pin()
            print(f"repo:   {info['repo']}")
            print(f"commit: {info['commit']}")
            print(f"date:   {info['date']}")
            print("verify: ok")
            return 0
        if args.model:
            if not args.out:
                parser.error("--model needs --out")
            started = time.perf_counter()
            path = ut.write_table(args.model, args.out, indent=args.indent)
            elapsed = time.perf_counter() - started
            print(f"{args.model}: written {path} ({elapsed:.2f} s)")
            return 0
        if not args.out_dir:
            parser.error("--all needs --out-dir")
        for name in ut.available_models():
            started = time.perf_counter()
            data = ut.generate(name)
            elapsed = time.perf_counter() - started
            path = os.path.join(args.out_dir, f"ElsterTable.{name}.json")
            ut.save_table(data, path, indent=args.indent)
            print(f"{name}: {len(data['signals'])} signals, "
                  f"{len(data['source']['duplicates_dropped'])} duplicates dropped, "
                  f"generate {elapsed:.2f} s")
        return 0
    except ut.UpstreamTableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
