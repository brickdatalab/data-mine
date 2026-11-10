#!/usr/bin/env python3
"""trestle_cnam: whose name is on this phone number (Trestle Smart CNAM 3.1).

  python3 tools/trestle_cnam.py --phone 6508235397
  python3 tools/trestle_cnam.py --file leads.csv
  python3 tools/trestle_cnam.py --file leads.xlsx --phone-col business_phone --prefix biz_
  python3 tools/trestle_cnam.py --table public.leads --key id

Key in .env: TRESTLE_API_KEY
Result columns: trestle_status (found / no_match / error...), trestle_name, trestle_first, trestle_middle,
trestle_last, trestle_valid (true/false: is the number itself valid)
One billed call per row sent.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import env, http, run

TOOL = "trestle_cnam"
URL = "https://api.trestleiq.com/3.1/cnam"
INPUTS = [("phone", "owner_cell_phone", "10-digit US phone number")]
OUT = ["trestle_status", "trestle_name", "trestle_first", "trestle_middle", "trestle_last", "trestle_valid"]


def lookup(v, prior, key):
    phone = run.phone10(v["phone"])
    for attempt in (1, 2):
        status, d, raw = http.call("GET", URL, headers={"x-api-key": key}, params={"phone": phone}, timeout=60)
        if status != 429 or attempt == 2:
            break
        time.sleep(2)
    if status == 403:
        raise run.Stop(f"Trestle rejected the key (HTTP 403 {raw[:100]}). Check TRESTLE_API_KEY.")
    if status == 429:
        raise run.Stop(f"Trestle rate limit or quota reached (HTTP 429 {raw[:100]}).")
    if status != 200 or not isinstance(d, dict):
        return {"trestle_status": run.err(status, raw)}
    run.save_raw(TOOL, phone, d)
    who = d.get("belongs_to") or {}
    valid = d.get("is_valid")
    out = {"trestle_name": who.get("name") or "", "trestle_first": who.get("firstname") or "",
           "trestle_middle": who.get("middlename") or "", "trestle_last": who.get("lastname") or "",
           "trestle_valid": "" if valid is None else str(bool(valid)).lower()}
    if out["trestle_name"]:
        out["trestle_status"] = "found"
    elif d.get("error"):
        out["trestle_status"] = f"error: {(d['error'] or {}).get('name') or d['error']}"
    else:
        out["trestle_status"] = "no_match"
    return out


def main(argv=None):
    args = run.parser(__doc__, INPUTS).parse_args(argv)
    src = run.open_source(args, INPUTS)
    key = None if args.dry_run else env.need("TRESTLE_API_KEY")[0]
    run.run(args, src, TOOL, INPUTS, OUT, lambda v, p: lookup(v, p, key), lambda v: bool(run.phone10(v["phone"])))


if __name__ == "__main__":
    main()
