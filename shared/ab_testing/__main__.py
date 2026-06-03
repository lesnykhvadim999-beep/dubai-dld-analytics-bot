"""CLI helper: `python -m shared.ab_testing register_experiment ...`."""
from __future__ import annotations

import argparse
import json
import sys

from .client import (
    register_experiment, list_running, set_winner, pause_experiment, ensure_schema,
)


def _parse_kv_list(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"Bad --variants/--split item: {it!r} (expected key=value)")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cmd_register(args) -> int:
    variants = _parse_kv_list(args.variants)
    split = _parse_kv_list(args.split or [])
    if split:
        split = {k: float(v) for k, v in split.items()}
    else:
        split = None
    row = register_experiment(
        experiment_key=args.key,
        description=args.description,
        variants=variants,
        traffic_split=split,
        bot_name=args.bot,
        default_variant=args.default or "A",
    )
    print(json.dumps(row, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_list(_args) -> int:
    rows = list_running()
    print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_set_winner(args) -> int:
    set_winner(args.key, args.variant)
    print(f"Set winner of {args.key} → {args.variant}")
    return 0


def cmd_pause(args) -> int:
    pause_experiment(args.key, args.reason or "")
    print(f"Paused {args.key}")
    return 0


def cmd_init_schema(_args) -> int:
    ensure_schema()
    print("Schema OK")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m ab_testing")
    sp = p.add_subparsers(dest="cmd", required=True)

    r = sp.add_parser("register_experiment", help="Register new experiment")
    r.add_argument("--key", required=True)
    r.add_argument("--description", required=True)
    r.add_argument("--variants", nargs="+", required=True,
                   help="VAR=label pairs, e.g. A=old B=new_with_thesis")
    r.add_argument("--split", nargs="+", help="VAR=weight pairs, e.g. A=0.5 B=0.5")
    r.add_argument("--bot")
    r.add_argument("--default")
    r.set_defaults(fn=cmd_register)

    l = sp.add_parser("list", help="List running experiments")
    l.set_defaults(fn=cmd_list)

    w = sp.add_parser("set_winner", help="Manually declare a winner")
    w.add_argument("--key", required=True)
    w.add_argument("--variant", required=True)
    w.set_defaults(fn=cmd_set_winner)

    pause = sp.add_parser("pause", help="Pause an experiment")
    pause.add_argument("--key", required=True)
    pause.add_argument("--reason")
    pause.set_defaults(fn=cmd_pause)

    init = sp.add_parser("init_schema", help="Create/refresh ab_testing tables")
    init.set_defaults(fn=cmd_init_schema)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
