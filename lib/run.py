"""What every tool shares: the command-line flags, picking which rows still need doing, and the loop that
sends each row to a lookup and writes the answer back."""
import argparse
import json
import re
import sys

from lib import sources
from lib.env import ROOT
from lib.sources import text

RAW = ROOT / "raw"


class Stop(Exception):
    """The whole run must stop (rejected key, out of credits). Rows already done stay saved."""


def phone10(s):
    """10-digit US number, or None."""
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def err(status, raw):
    return f"error: HTTP {status} {' '.join((raw or '').split())[:120]}".strip()


def save_raw(tool, key, data):
    """Full provider response -> raw/<tool>/<key>.json"""
    try:
        d = RAW / tool
        d.mkdir(parents=True, exist_ok=True)
        (d / (re.sub(r"[^A-Za-z0-9@._-]", "_", str(key))[:120] + ".json")).write_text(json.dumps(data, indent=1))
    except OSError as e:
        print(f"WARNING: could not save raw response for {key}: {e}", file=sys.stderr)


def parser(doc, inputs):
    """inputs: [(name, default column, description)]. Each becomes --name (single value) and --name-col (column override)."""
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="CSV or XLSX file; results are appended to it")
    ap.add_argument("--sheet", help="XLSX sheet name (default: the first sheet)")
    ap.add_argument("--table", help="Supabase table, e.g. public.leads; results go into existing columns")
    ap.add_argument("--key", default="id", help="column that identifies a row (default: id)")
    ap.add_argument("--where", help="SQL condition for --table, e.g. \"state = 'NY'\"")
    ap.add_argument("--ids", nargs="+", help="only rows whose --key value is one of these")
    ap.add_argument("--limit", type=int, help="at most this many rows")
    ap.add_argument("--dry-run", action="store_true", help="count the rows that would be sent; no API call, nothing written")
    ap.add_argument("--force", action="store_true", help="redo rows that already have a result")
    ap.add_argument("--prefix", default="", help="text put in front of every result column name, e.g. biz_")
    for name, col, desc in inputs:
        flag = name.replace("_", "-")
        ap.add_argument(f"--{flag}", help=f"single value: {desc}")
        ap.add_argument(f"--{flag}-col", default=col, metavar="COLUMN", help=f"column holding the {desc} (default: {col})")
    return ap


def open_source(args, inputs):
    singles = {getattr(args, f"{n}_col"): getattr(args, n) for n, _, _ in inputs if getattr(args, n) is not None}
    if sum(map(bool, (args.file, args.table, singles))) != 1:
        flags = " ".join(f"--{n.replace('_', '-')}" for n, _, _ in inputs)
        sys.exit(f"Pick exactly one source: --file FILE, --table TABLE, or single values ({flags}). See --help.")
    if singles:
        return sources.SingleSource(singles)
    if args.table:
        return sources.SupabaseSource(args.table, args.key, args.where)
    if args.file.lower().endswith((".xlsx", ".xlsm")):
        return sources.XlsxSource(args.file, args.sheet)
    return sources.CsvSource(args.file)


def values(row, args, inputs):
    return {n: text(row.get(getattr(args, f"{n}_col"))).strip() for n, _, _ in inputs}


def prior(row, args, out_cols):
    """What the row's result columns hold now."""
    return {c: text(row.get(args.prefix + c)).strip() for c in out_cols}


def pick(args, src, inputs, out_cols, ready, todo=None, redo=None):
    """-> ([(row, values)] to send, count already done, count without usable input).
    Default todo: the first result column (the tool's status column) is blank or starts with 'error'."""
    redo = args.force if redo is None else redo
    todo = todo or (lambda p: not p[out_cols[0]] or p[out_cols[0]].startswith("error"))
    ids = set(args.ids or [])
    targets, n_done, n_noinput = [], 0, 0
    for r in src.rows:
        if ids and text(r.get(args.key)) not in ids:
            continue
        v = values(r, args, inputs)
        if not ready(v):
            n_noinput += 1
        elif redo or todo(prior(r, args, out_cols)):
            targets.append((r, v))
        else:
            n_done += 1
    if args.limit:
        targets = targets[:args.limit]
    return targets, n_done, n_noinput


def run(args, src, tool, inputs, out_cols, lookup, ready, todo=None, redo=None, emit=True):
    """lookup(values, prior) -> {result column: value}. Raises Stop to end the run."""
    cols = [args.prefix + c for c in out_cols]
    src.ensure(cols)        # a Supabase table missing a result column exits here, before any API call
    targets, n_done, n_noinput = pick(args, src, inputs, out_cols, ready, todo, redo)
    counts = f"{n_done} already done (skipped) | {n_noinput} without usable input (skipped)"
    single = src.kind == "single"
    if args.dry_run:
        print(f"{tool} (dry run): {len(targets)} rows would be sent | {counts} | no API call made, nothing written")
        return
    if single and emit and n_noinput:
        sys.exit(f"{tool}: not enough input for a lookup. See --help.")
    done = 0
    try:
        for i, (r, v) in enumerate(targets, 1):
            res = lookup(v, prior(r, args, out_cols))
            for c in out_cols:
                if c in res:
                    r[args.prefix + c] = res[c]
            src.write(r, cols)
            done += 1
            if not single:
                print(f"{tool} {text(r.get(args.key)) or i}: " + " | ".join(text(r.get(c)) for c in cols[:4]))
            if i % 20 == 0:
                src.flush()
    except Stop as e:
        sys.exit(f"STOPPED: {e} ({done} rows were saved before stopping)")
    finally:
        src.flush()
    if not single:
        print(f"{tool}: {done} rows done | {counts} | saved to {src.where}")
    elif emit:
        print(json.dumps({c: src.rows[0].get(c, "") for c in cols}, indent=1))
