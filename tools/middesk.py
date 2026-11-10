#!/usr/bin/env python3
"""middesk: does this business show an MCA default? Three steps against Middesk, starting from an EIN.

  Step 1  POST /v1/prefill/businesses   EIN -> legal name, address, officers
  Step 2  POST /v1/businesses           create the business with the searches bundled as orders
  Step 3  GET  /v1/businesses/{id}      read litigations[], liens[], bankruptcies[] -> MCA default reading

  python3 tools/middesk.py --ein 811234567
  python3 tools/middesk.py --file leads.csv
  python3 tools/middesk.py --table public.leads --key id
  python3 tools/middesk.py --file leads.csv --wait 0      order now, rerun the same command later to fetch
  python3 tools/middesk.py --business-id <id>             fetch one business that was already ordered

Step 1 is skipped for a row that already has a business name and a full address (street, city, state, ZIP).
Middesk's searches take seconds to minutes. The tool checks back for --wait seconds (default 180); rows still
not ready keep mid_status = ordered and their mid_business_id, and the next run fetches them WITHOUT ordering again.
A row that has a mid_business_id is never ordered a second time. To order again, blank that cell.
--force re-reads results for rows that have a business id (and reads them even if some orders are unfinished).

Key in .env: MIDDESK_API_KEY (mk_live_...; a mk_test_... key needs --sandbox)
Each order placed in step 2 is billed by Middesk. The EIN lookup, litigations, liens and bankruptcies each have to
be enabled on the Middesk account; a product that is not enabled shows up as an error in mid_status or as an
unfinished order in mid_orders. Read mid_orders before trusting a "none".

Result columns:
  mid_status            ordered / done / partial / no_match (Middesk could not resolve the EIN) / error...
  mid_business_id       Middesk's id for the business
  mid_business_status   Middesk's own status (open, pending, in_review, approved, rejected...)
  mid_name              the business name that was submitted
  mid_orders            each order and its status, e.g. "litigations:completed, liens:completed"
  mid_mca_default       strong / possible / mca_activity / none   (rules below)
  mid_mca_signals       the findings behind that reading, in words
  mid_lit_defendant     number of lawsuits where the business is the defendant
  mid_judgment_total    dollars of judgments entered in those lawsuits
  mid_liens_enforcement number of judgment or attachment liens
  mid_ucc_active        number of active UCC liens
  mid_bankruptcies      number of bankruptcy filings

How mid_mca_default is decided (Middesk has no "MCA default" field; this reads its arrays):
  strong        sued as defendant by a funder listed in mca_funders.txt, OR a judgment entered in a suit where the
                business is the defendant, OR a judgment/attachment lien
  possible      a debt/collection suit as defendant with neither of the above, OR any bankruptcy filing
  mca_activity  none of the above, but an active UCC lien on receivables / all assets or filed through
                "Corporation Service Company / C T Corporation System, as Representative" (MCA exposure, not default)
  none          nothing above was found in what Middesk returned
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import env, http, run, sources
from lib.env import ROOT

TOOL = "middesk"
INPUTS = [("ein", "ein", "9-digit EIN"), ("name", "legal_name", "business legal name"), ("address", "address", "street address"),
          ("city", "city", "city"), ("state", "state", "2-letter state"), ("zip", "zip", "ZIP code"),
          ("owner_first", "owner_first", "owner first name"), ("owner_last", "owner_last", "owner last name")]
OUT = ["mid_status", "mid_business_id", "mid_business_status", "mid_name", "mid_orders", "mid_mca_default", "mid_mca_signals",
       "mid_lit_defendant", "mid_judgment_total", "mid_liens_enforcement", "mid_ucc_active", "mid_bankruptcies"]
PRODUCTS = "business_verification_verify,tin,litigations,people_litigations,liens,bankruptcies"
FINISHED = {"completed", "approved", "rejected"}                  # order statuses that will not change again
VERIFIED = {"in_review", "approved", "rejected"}                  # business statuses meaning verification is over
REPRESENTATIVE = re.compile(r"(corporation service company|c\.? ?t\.? corporation).*representative", re.I)


def load_funders():
    p = ROOT / "mca_funders.txt"
    if not p.exists():
        return []
    return [line.strip().lower() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]


def mca_signals(biz, funders):
    """The MCA default reading from one Middesk business object. Rules are in the module docstring."""
    suits = [s for s in biz.get("litigations") or [] if (s.get("party_type") or "").lower() == "defendant"]
    liens = biz.get("liens") or []
    bankruptcies = biz.get("bankruptcies") or []
    signals, strong, possible, judgment_cents = [], False, False, 0
    for s in suits:
        plaintiffs = [p.get("name") or "" for p in s.get("parties") or [] if (p.get("role") or "").lower() == "plaintiff"]
        who = plaintiffs[0] if plaintiffs else (s.get("case_name") or "unknown plaintiff")
        where = " ".join(x for x in (s.get("jurisdiction_state"), s.get("filing_date")) if x)
        listed = [n for n in plaintiffs if any(f in n.lower() for f in funders)]
        judgments = s.get("judgments") or []
        cents = sum(j.get("amount_cents") or 0 for j in judgments)
        judgment_cents += cents
        if listed:
            strong = True
            signals.append(f"sued by listed MCA funder {listed[0]} ({where})")
        if judgments:
            strong = True
            signals.append("judgment entered against the business" + (f" ${cents / 100:,.2f}" if cents else "") + f" ({who})")
        if not listed and not judgments and re.search(r"debt|collection", s.get("case_type") or "", re.I):
            possible = True
            signals.append(f"debt-collection suit as defendant ({who}, {where})")
    enforcement = [x for x in liens if (x.get("type") or "").lower() in ("judgment", "attachment")]
    ucc = [x for x in liens if (x.get("type") or "").lower() == "ucc" and (x.get("status_category") or "").lower() == "active"]
    receivables = [x for x in ucc if re.search(r"receivable|all assets", x.get("collateral_type") or "", re.I)]
    via_rep = [x for x in ucc if any(REPRESENTATIVE.search(p.get("name") or "") for p in x.get("secured_parties") or [])]
    if enforcement:
        strong = True
        signals.append(f"{len(enforcement)} judgment/attachment lien(s)")
    if ucc:
        signals.append(f"{len(ucc)} active UCC lien(s): {len(receivables)} on receivables/all assets, {len(via_rep)} filed through a representative")
    for b in bankruptcies:
        possible = True
        signals.append(" ".join(str(x) for x in ("bankruptcy", f"chapter {b['chapter']}" if b.get("chapter") else "",
                                                 b.get("filing_date") or "", f"({b['status']})" if b.get("status") else "") if x))
    level = "strong" if strong else "possible" if possible else "mca_activity" if (receivables or via_rep) else "none"
    return {"mid_mca_default": level, "mid_mca_signals": "; ".join(signals), "mid_lit_defendant": len(suits),
            "mid_judgment_total": f"{judgment_cents / 100:.2f}" if judgment_cents else "", "mid_liens_enforcement": len(enforcement),
            "mid_ucc_active": len(ucc), "mid_bankruptcies": len(bankruptcies)}


def api(ctx, method, path, body=None):
    status, d, raw = http.call(method, ctx["base"] + path, headers={"Authorization": f"Bearer {ctx['key']}"}, body=body, timeout=60)
    if status == 401:
        raise run.Stop("Middesk rejected the key (HTTP 401). Check MIDDESK_API_KEY. A mk_test key needs --sandbox; a mk_live key must not use it.")
    return status, d, raw


def problem(status, d, raw):
    errors = d.get("errors") if isinstance(d, dict) else None
    if errors:
        return f"error: HTTP {status} " + "; ".join(str(e.get("message") or e) for e in errors)[:200]
    return run.err(status, raw)


def digits(s):
    return re.sub(r"\D", "", s or "")


def row_address(v):
    if v["address"] and v["city"] and v["state"] and v["zip"]:
        return {"address_line1": v["address"], "city": v["city"], "state": v["state"], "postal_code": v["zip"]}
    return None


def can_start(v):
    return len(digits(v["ein"])) == 9 or bool(v["name"] and row_address(v))


def start(v, prior, ctx):
    """Steps 1 and 2: resolve the EIN if the row lacks a name or address, then create the business with its orders."""
    ein = digits(v["ein"])
    name, address = v["name"], row_address(v)
    people = [" ".join(x for x in (v["owner_first"], v["owner_last"]) if x)] if (v["owner_first"] or v["owner_last"]) else []
    if not (name and address):
        status, d, raw = api(ctx, "POST", "/prefill/businesses", {"tin": ein})
        if status == 200 and not d:
            return {"mid_status": "no_match"}
        if status != 200 or not isinstance(d, dict):
            return {"mid_status": problem(status, d, raw)}
        run.save_raw(TOOL, f"prefill_{ein}", d)
        names = [n for n in d.get("names") or [] if n.get("name")]
        legal = [n["name"] for n in names if n.get("type") == "legal"] + [n["name"] for n in names]
        found = (d.get("addresses") or [{}])[0]
        parts = {k: found[k] for k in ("address_line1", "address_line2", "city", "state", "postal_code") if found.get(k)}
        if "address_line1" not in parts and found.get("full_address"):
            parts = {"full_address": found["full_address"]}
        name = name or (legal[0] if legal else "")
        address = address or parts
        people = people or [p["name"] for p in d.get("people") or [] if p.get("name")][:5]
        if not (name and address):
            return {"mid_status": "no_match"}
    body = {"name": name, "addresses": [address], "orders": ctx["orders"]}
    if len(ein) == 9:
        body["tin"] = {"tin": ein}
    if people:
        body["people"] = [{"name": n} for n in people]
    status, d, raw = api(ctx, "POST", "/businesses", body)
    if status not in (200, 201) or not isinstance(d, dict) or not d.get("id"):
        return {"mid_status": problem(status, d, raw)}
    return {"mid_status": "ordered", "mid_business_id": d["id"], "mid_business_status": d.get("status") or "", "mid_name": name}


def fetch(v, prior, ctx):
    """Step 3: read the business; when its orders are finished, turn the arrays into the MCA default reading."""
    business_id = prior["mid_business_id"]
    while True:
        status, d, raw = api(ctx, "GET", f"/businesses/{business_id}")
        if status != 200 or not isinstance(d, dict):
            return {"mid_status": problem(status, d, raw)}
        orders = d.get("orders") or []
        ready = all(o.get("status") in FINISHED for o in orders) if orders else d.get("status") in VERIFIED
        if ready or ctx["force"] or time.time() >= ctx["deadline"]:
            break
        time.sleep(10)
    out = {"mid_business_status": d.get("status") or "",
           "mid_orders": ", ".join(f"{o.get('product')}:{o.get('status')}" for o in orders)}
    if not (ready or ctx["force"]):
        return {**out, "mid_status": "ordered"}
    run.save_raw(TOOL, business_id, d)
    return {**out, **mca_signals(d, ctx["funders"]), "mid_status": "done" if ready else "partial"}


def main(argv=None):
    ap = run.parser(__doc__, INPUTS)
    ap.add_argument("--business-id", help="single value: fetch results for a Middesk business that was already ordered")
    ap.add_argument("--wait", type=int, default=180, help="seconds to keep checking for results before leaving rows as 'ordered' (default 180)")
    ap.add_argument("--products", default=PRODUCTS, help=f"comma-separated Middesk products to order (default: {PRODUCTS})")
    ap.add_argument("--sandbox", action="store_true", help="use api-sandbox.middesk.com (mk_test key, fake data, no charge)")
    args = ap.parse_args(argv)
    if args.business_id:
        src = sources.SingleSource({args.prefix + "mid_status": "ordered", args.prefix + "mid_business_id": args.business_id})
    else:
        src = run.open_source(args, INPUTS)
        if src.kind == "single" and not can_start(run.values(src.rows[0], args, INPUTS)):
            sys.exit("middesk needs a 9-digit --ein, or --name with --address --city --state --zip.")
    ctx = None
    if not args.dry_run:
        products = [p.strip() for p in args.products.split(",") if p.strip()]
        ctx = {"key": env.need("MIDDESK_API_KEY")[0], "force": args.force, "funders": load_funders(), "deadline": 0,
               "base": f"https://{'api-sandbox' if args.sandbox else 'api'}.middesk.com/v1",
               "orders": [{"product": p, "subproducts": ["ucc_documents"]} if p == "liens" else {"product": p} for p in products]}
    # A row with a business id is never ordered again, whatever flags are passed.
    run.run(args, src, "middesk order", INPUTS, OUT, lambda v, p: start(v, p, ctx), can_start, redo=False, emit=False,
            todo=lambda p: not p["mid_business_id"] and (not p["mid_status"] or p["mid_status"].startswith("error")))
    if ctx:
        ctx["deadline"] = time.time() + args.wait
    run.run(args, src, "middesk fetch", INPUTS, OUT, lambda v, p: fetch(v, p, ctx), lambda v: True, redo=False,
            todo=lambda p: bool(p["mid_business_id"]) and (args.force or p["mid_status"] == "ordered" or p["mid_status"].startswith("error")))


if __name__ == "__main__":
    main()
