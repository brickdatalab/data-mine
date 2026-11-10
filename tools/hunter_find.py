#!/usr/bin/env python3
"""hunter_find: find a person's work email from their name and company (Hunter.io Email Finder).

  python3 tools/hunter_find.py --first Jane --last Roe --domain roe.com
  python3 tools/hunter_find.py --first Jane --last Roe --company "Roe Plumbing LLC"
  python3 tools/hunter_find.py --file leads.csv --domain-col website
  python3 tools/hunter_find.py --table public.leads --key id

Needs first name + last name + (domain or company name). A website URL in the domain column is reduced to its
domain. The company name is sent only when the row has no domain.

Key in .env: HUNTER_API_KEY
Result columns: hunter_status (found / no_match / error...), hunter_email, hunter_score (0-100 confidence),
hunter_verification (valid / accept_all / unknown), hunter_position
Hunter's docs say no credit is charged when no email is found.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import env, http, run

TOOL = "hunter_find"
URL = "https://api.hunter.io/v2/email-finder"
INPUTS = [("first", "owner_first", "person's first name"), ("last", "owner_last", "person's last name"),
          ("domain", "website", "company website or domain"), ("company", "legal_name", "company name")]
OUT = ["hunter_status", "hunter_email", "hunter_score", "hunter_verification", "hunter_position"]


def domain_of(s):
    """'https://www.Roe.com/about' or 'jane@roe.com' -> 'roe.com'"""
    s = re.sub(r"^[a-z]+://", "", (s or "").strip().lower()).split("/")[0].split("@")[-1]
    s = re.sub(r"^www\.", "", s)
    return s if "." in s else ""


def lookup(v, prior, key):
    params = {"first_name": v["first"], "last_name": v["last"]}
    if domain_of(v["domain"]):
        params["domain"] = domain_of(v["domain"])
    else:
        params["company"] = v["company"]
    for attempt in (1, 2):
        status, d, raw = http.call("GET", URL, headers={"X-API-KEY": key}, params=params, timeout=40)
        if status != 403 or attempt == 2:      # 403 is Hunter's per-second rate limit
            break
        time.sleep(2)
    if status == 401:
        raise run.Stop("Hunter rejected the key (HTTP 401). Check HUNTER_API_KEY.")
    if status == 429:
        raise run.Stop(f"Hunter usage limit reached or account restricted (HTTP 429 {raw[:100]}).")
    data = d.get("data") if isinstance(d, dict) else None
    if status in (404, 451) or (status == 200 and not (data or {}).get("email")):
        return {"hunter_status": "no_match"}
    if status != 200 or not data:
        details = "; ".join(str(e.get("details") or e.get("id")) for e in (d or {}).get("errors") or []) if isinstance(d, dict) else ""
        return {"hunter_status": f"error: HTTP {status} {details}".strip() if details else run.err(status, raw)}
    run.save_raw(TOOL, data["email"], d)
    return {"hunter_status": "found", "hunter_email": data["email"], "hunter_score": data.get("score") or "",
            "hunter_verification": (data.get("verification") or {}).get("status") or "", "hunter_position": data.get("position") or ""}


def main(argv=None):
    args = run.parser(__doc__, INPUTS).parse_args(argv)
    src = run.open_source(args, INPUTS)
    key = None if args.dry_run else env.need("HUNTER_API_KEY")[0]
    run.run(args, src, TOOL, INPUTS, OUT, lambda v, p: lookup(v, p, key),
            lambda v: bool(v["first"] and v["last"] and (domain_of(v["domain"]) or v["company"])))


if __name__ == "__main__":
    main()
