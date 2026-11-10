#!/usr/bin/env python3
"""emailable_verify: is this email address deliverable (Emailable single verification).

  python3 tools/emailable_verify.py --email jane@roe.com
  python3 tools/emailable_verify.py --file leads.csv
  python3 tools/emailable_verify.py --file leads.csv --email-col hunter_email --prefix found_
  python3 tools/emailable_verify.py --table public.leads --key id

Key in .env: EMAILABLE_API_KEY (live_... keys spend credits; test_... keys do not)
Result columns: emailable_state (deliverable / undeliverable / risky / unknown / error...), emailable_reason,
emailable_score (0-100), emailable_did_you_mean, emailable_flags (any of accept_all, disposable, free,
mailbox_full, no_reply, role that are true)
One verification per row sent. A slow lookup answers HTTP 249 "send again"; the tool resends, which Emailable's
docs say is free for 5 minutes.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import env, http, run

TOOL = "emailable_verify"
URL = "https://api.emailable.com/v1/verify"
INPUTS = [("email", "owner_email", "email address")]
OUT = ["emailable_state", "emailable_reason", "emailable_score", "emailable_did_you_mean", "emailable_flags"]
FLAGS = ["accept_all", "disposable", "free", "mailbox_full", "no_reply", "role"]
TRIES = 8


def lookup(v, prior, key):
    for _ in range(TRIES):
        status, d, raw = http.call("GET", URL, headers={"Authorization": f"Bearer {key}"}, params={"email": v["email"]}, timeout=40)
        if status not in (249, 429):        # 249: still working, send again. 429: per-second rate limit.
            break
        time.sleep(3)
    else:
        return {"emailable_state": f"error: no answer after {TRIES} tries (HTTP {status})"}
    if status in (401, 403):
        raise run.Stop(f"Emailable rejected the key (HTTP {status}). Check EMAILABLE_API_KEY.")
    if status == 402:
        raise run.Stop("Emailable account is out of credits (HTTP 402).")
    if status != 200 or not isinstance(d, dict) or not d.get("state"):
        return {"emailable_state": run.err(status, raw)}
    run.save_raw(TOOL, v["email"], d)
    return {"emailable_state": d["state"], "emailable_reason": d.get("reason") or "",
            "emailable_score": "" if d.get("score") is None else d["score"], "emailable_did_you_mean": d.get("did_you_mean") or "",
            "emailable_flags": ",".join(f for f in FLAGS if d.get(f) is True)}


def main(argv=None):
    args = run.parser(__doc__, INPUTS).parse_args(argv)
    src = run.open_source(args, INPUTS)
    key = None if args.dry_run else env.need("EMAILABLE_API_KEY")[0]
    run.run(args, src, TOOL, INPUTS, OUT, lambda v, p: lookup(v, p, key), lambda v: "@" in v["email"])


if __name__ == "__main__":
    main()
