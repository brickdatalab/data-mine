#!/usr/bin/env python3
"""enformion_enrich: find a PERSON's phones, emails and address (Enformion Contact Enrich Plus).
It looks up a person, not a business: on a business row, point it at the owner.

  python3 tools/enformion_enrich.py --first Jane --last Roe --email jane@roe.com
  python3 tools/enformion_enrich.py --file leads.csv
  python3 tools/enformion_enrich.py --table public.leads --key id

Needs at least TWO of: name (first + last), phone, address (street + city/state or ZIP), email.
Rows with fewer are skipped without a call.

Keys in .env: ENFORMION_AP_NAME, ENFORMION_AP_PASSWORD
Result columns: enf_status (found / no_match / error...), enf_identity_score, enf_name, enf_age,
enf_phone_1..3, enf_phone_1_type..3_type, enf_email_1..3, enf_address
Enformion's docs say Contact Enrich is charged on a successful match only.

The Contact Enrich Plus response format is not published. Columns are filled from the field names documented for
plain Contact Enrich. The full response is in raw/enformion_enrich/ - read it there for the Plus-only fields
(associates, income, property and so on).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import env, http, run

TOOL = "enformion_enrich"
URL = "https://devapi.enformion.com/Contact/EnrichPlus"
INPUTS = [("first", "owner_first", "person's first name"), ("last", "owner_last", "person's last name"),
          ("address", "address", "street address"), ("city", "city", "city"), ("state", "state", "2-letter state"),
          ("zip", "zip", "ZIP code"), ("phone", "owner_cell_phone", "phone number"), ("email", "owner_email", "email address")]
OUT = (["enf_status", "enf_identity_score", "enf_name", "enf_age"]
       + [f"enf_phone_{i}{s}" for i in (1, 2, 3) for s in ("", "_type")]
       + ["enf_email_1", "enf_email_2", "enf_email_3", "enf_address"])


def request_body(v):
    """Only the inputs this row has. Enformion needs two of name / phone / address / email."""
    body = {}
    if v["first"] and v["last"]:
        body["FirstName"], body["LastName"] = v["first"], v["last"]
    p = run.phone10(v["phone"])
    if p:
        body["Phone"] = f"{p[:3]}-{p[3:6]}-{p[6:]}"
    line2 = " ".join(x for x in (", ".join(x for x in (v["city"], v["state"]) if x), v["zip"]) if x)
    if v["address"] and line2:
        body["Address"] = {"addressLine1": v["address"], "addressLine2": line2}
    if "@" in v["email"]:
        body["Email"] = v["email"]
    return body


def ready(v):
    b = request_body(v)
    return sum(k in b for k in ("FirstName", "Phone", "Address", "Email")) >= 2


def lookup(v, prior, creds):
    body = request_body(v)
    status, d, raw = http.call("POST", URL, body=body, timeout=60, headers={
        "galaxy-ap-name": creds[0], "galaxy-ap-password": creds[1], "galaxy-search-type": "DevAPIContactEnrichPlus"})
    if status in (401, 403):
        raise run.Stop(f"Enformion rejected the credentials (HTTP {status}). Check ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD.")
    if status != 200 or not isinstance(d, dict):
        return {"enf_status": run.err(status, raw)}
    run.save_raw(TOOL, body.get("Email") or body.get("Phone") or f"{v['first']}_{v['last']}_{v['zip']}", d)
    if d.get("isError"):
        problems = (d.get("error") or {}).get("inputErrors") or d.get("message") or "isError"
        return {"enf_status": f"error: {problems}"[:150]}
    person = d.get("person") or {}
    name = person.get("name") or {}
    full = " ".join(x for x in (name.get("firstName"), name.get("middleName"), name.get("lastName")) if x)
    phones, emails, addrs = person.get("phones") or [], person.get("emails") or [], person.get("addresses") or []
    if not (full or phones or emails or addrs):
        return {"enf_status": "no_match"}
    out = {"enf_status": "found", "enf_identity_score": d.get("identityScore", ""), "enf_name": full, "enf_age": person.get("age") or ""}
    for i, ph in enumerate(phones[:3], 1):
        out[f"enf_phone_{i}"] = run.phone10(ph.get("number")) or ph.get("number") or ""
        out[f"enf_phone_{i}_type"] = ph.get("type") or ""
    for i, em in enumerate(emails[:3], 1):
        out[f"enf_email_{i}"] = em.get("email") or ""
    if addrs:
        a = addrs[0]
        street = " ".join(x for x in (a.get("street"), a.get("unit")) if x)
        out["enf_address"] = ", ".join(x for x in (street, a.get("city"), " ".join(x for x in (a.get("state"), a.get("zip")) if x)) if x)
    return out


def main(argv=None):
    args = run.parser(__doc__, INPUTS).parse_args(argv)
    src = run.open_source(args, INPUTS)
    creds = None if args.dry_run else env.need("ENFORMION_AP_NAME", "ENFORMION_AP_PASSWORD")
    run.run(args, src, TOOL, INPUTS, OUT, lambda v, p: lookup(v, p, creds), ready)


if __name__ == "__main__":
    main()
