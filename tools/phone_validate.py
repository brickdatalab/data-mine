#!/usr/bin/env python3
"""phone_validate: is this number live, what kind of line is it, whose name is on it, is it on a do-not-call or
litigator list, and how well does the name match the lead. Uses RealPhoneValidation DNC Plus + Twilio Lookup caller name, with an
optional Supabase cache table (real_validation) so a number already checked is not billed again.

  python3 tools/phone_validate.py --phone 6508235397 --owner-first Jane --owner-last Roe
  python3 tools/phone_validate.py --file leads.csv              owner_cell_phone -> status / phone_type / caller_name / score ...
  python3 tools/phone_validate.py --file leads.csv --biz        business_phone   -> biz_status / biz_phone_type / ...
  python3 tools/phone_validate.py --table public.leads --key id
  python3 tools/phone_validate.py --file leads.csv --dry-run    cache lookup only: how many are free vs need an API

Per number, the cache is read first and only the missing information is fetched:
  status, line type and caller name all cached -> no API call, no cost
  no caller name                               -> Twilio only
  no status or no line type                    -> RealPhoneValidation DNC Plus only
  neither, or not in the cache                 -> both, at the same time
  a dead line (status other than connected*)   -> needs no line type and no name lookup
Cache update: new information is added and a value is replaced only by a newer non-empty one. A stored line type
is never overwritten. A Twilio name of null is stored as "Unavailable" so the number is not billed again.

Keys in .env: REALVALIDATION_TOKEN, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
Cache (optional): SUPABASE_ACCESS_TOKEN + SUPABASE_PROJECT_REF, and a real_validation table in that project.
Without the keys or without the table, the tool warns and runs API-only.

Result columns: status, phone_type (Mobile / Landline / VoIP), caller_name, score, litigator, natdnc, stdnc, dma
(the last four are true / false / blank for unknown)
Score: litigator = true -> -25 always; disconnected -> 0; connected with caller name "Unavailable" -> 90;
caller name matches the owner, legal name or DBA -> 100; same last name, different first -> 75; otherwise the
fuzzy match (0-100), raised to 60 when the caller name shares the owner's last name or a distinctive business word.
The score reads owner_first, owner_last, legal_name, doing_business_as, business_email, owner_email.

--force sends every number to both APIs even if cached (billed). --ids re-fills those rows (cache still applies).
Requires: pip install -r requirements.txt (rapidfuzz, nicknames)
"""
import base64
import datetime
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import env, http, run, supabase
from lib.supabase import q, qb

try:
    from nicknames import NickNamer
    from rapidfuzz import fuzz
except ImportError:
    sys.exit("phone_validate needs rapidfuzz and nicknames. Run: pip install -r requirements.txt")

TOOL = "phone_validate"
INPUTS = [("phone", "owner_cell_phone", "10-digit US phone number"),
          ("owner_first", "owner_first", "owner first name (for the score)"), ("owner_last", "owner_last", "owner last name (for the score)"),
          ("legal_name", "legal_name", "business legal name (for the score)"), ("doing_business_as", "doing_business_as", "DBA (for the score)"),
          ("business_email", "business_email", "business email (for the score)"), ("owner_email", "owner_email", "owner email (for the score)")]
LEAD = [n for n, _, _ in INPUTS[1:]]
OUT = ["status", "phone_type", "caller_name", "score", "litigator", "natdnc", "stdnc", "dma"]

DNC_URL = "https://api.realvalidation.com/rpvWebService/DNCPlus.php"
TWILIO_URL = "https://lookups.twilio.com/v2/PhoneNumbers/%2B1{phone}?Fields=caller_name"
IS_CELL = {"Y": "Mobile", "N": "Landline", "V": "VoIP"}   # DNC Plus iscell -> the wording the sheet uses
FLAG = {"Y": True, "N": False}                              # "?" (timeout / other list) -> unknown (NULL)
TYPE_MAP = {"cellular": "Mobile", "landline": "Landline", "voip": "VoIP"}  # cache wording -> sheet wording
TABLE = "public.real_validation"
LOOKUP_BATCH = 300
WRITE_BATCH = 200
FLUSH_EVERY = 50


# ---------- scoring ----------
NICK = NickNamer()
BUSINESS_SUFFIXES = {"llc", "inc", "corp", "corporation", "co", "ltd", "llp", "pllc", "pc", "company"}


def tokens(name):
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return [w for w in words if w not in BUSINESS_SUFFIXES]


def stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def has_word(w, words):
    """Exact match, or a one-letter typo on words of 5+ letters (greenwood ~ greenwoood)."""
    return w in words or (len(w) >= 5 and any(len(x) >= 5 and fuzz.ratio(w, x) >= 88 for x in words))


def name_score(caller, candidate):
    """100 if every caller word appears in the candidate (the last caller word may be
    cut off by the API, e.g. 'secu' -> 'security', or 'a' -> 'and' when the name hit the API's 15-character limit; one-letter typos on 5+ letter words still match), else fuzzy token-sort ratio."""
    c, n = tokens(caller), tokens(candidate)
    if not c or not n:
        return 0
    c, n = [stem(w) for w in c], [stem(w) for w in n]  # plural-insensitive: autos == auto
    if len(c) >= 2:
        *full, last = c
        if all(has_word(w, n) for w in full) and (has_word(last, n) or ((len(last) >= 3 or len(caller.strip()) >= 15) and any(w.startswith(last) for w in n))):
            return 100
    return round(fuzz.token_sort_ratio(" ".join(c), " ".join(n)))


def same_first(a, b):
    return a == b or b in NICK.nicknames_of(a) or b in NICK.canonicals_of(a) \
        or bool(NICK.canonicals_of(a) & NICK.canonicals_of(b))


def last_name_score(row):
    """Caller shares the owner's last name. Caller name comes as 'FIRST LAST' or 'LAST,FIRST'."""
    caller = row["caller_name"]
    first, last = tokens(row["owner_first"]), tokens(row["owner_last"])
    if not first or not last:
        return None
    last = " ".join(last)
    if "," in caller:
        c_last, _, c_first = caller.partition(",")
        c_last, c_first = " ".join(tokens(c_last)), tokens(c_first)
    else:
        words = tokens(caller)
        c_last, c_first = " ".join(words[-1:]), words[:-1]
    if c_last != last and not " ".join(tokens(caller)).endswith(last):
        return None
    if not c_first or len(c_first[0]) == 1:           # no first name, or initial only
        return 100
    if same_first(first[0], c_first[0]):              # same name or nickname
        return 100
    business = tokens(row.get("legal_name", "")) + tokens(row.get("doing_business_as", ""))
    if last in business:                              # last name in business name
        return 100
    return 75                                         # same last, different first


def score(row):
    if row["status"].startswith("connected") and row["caller_name"].strip().lower() == "unavailable":
        return 90
    candidates = [f"{row['owner_first']} {row['owner_last']}", row.get("legal_name", ""), row.get("doing_business_as", "")]
    best = max(name_score(row["caller_name"], c) for c in candidates if c.strip())
    if best == 100:
        return 100
    return last_name_score(row) or best


FLOOR = 60
GENERIC = set("llc inc corp company co ltd group services service logistics transport transportation trucking "
              "construction enterprises enterprise holdings solutions industries industry auto the and of management "
              "consulting contractors contracting restaurant pizza cafe bakery store shop repair properties property "
              "realty homes home sales systems supply".split())


def words(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def shares_with_lead(caller, row):
    """Caller name has the owner's last name (4+ letters) as a word or at the start of a word, or a non-generic
    word (4+ letters) that is also in the legal name, DBA, owner first name or an email local part."""
    cw = words(caller)
    for last in (w for w in words(row.get("owner_last")) if len(w) >= 4):
        if any(w.startswith(last) for w in cw):
            return True
    pool = set(words(row.get("legal_name")) + words(row.get("doing_business_as")) + words(row.get("owner_first"))
               + words((row.get("business_email") or "").split("@")[0]) + words((row.get("owner_email") or "").split("@")[0]))
    return any(len(w) >= 4 and w not in GENERIC and any(p == w or p.startswith(w) or (w.startswith(p) and len(p) >= 4) for p in pool)
               for w in cw)


# ---------- the two API calls, merged ----------
def merged_lookup(phone, creds, dnc=True, tw=True):
    """The needed calls at the same time -> one merged dict. Keys only appear for services that were called and answered.
    raw/phone_validate/<phone>.json accumulates: a later partial call adds to the file instead of replacing it."""
    token, sid, auth = creds
    basic = "Basic " + base64.b64encode(f"{sid}:{auth}".encode()).decode()
    with ThreadPoolExecutor(max_workers=2) as ex:
        jobs = {}
        if dnc:
            jobs["dnc"] = ex.submit(http.call, "GET", DNC_URL, None, {"output": "json", "phone": phone, "token": token})
        if tw:
            jobs["tw"] = ex.submit(http.call, "GET", TWILIO_URL.format(phone=phone), {"Authorization": basic})
        res = {k: f.result() for k, f in jobs.items()}
    merged = {"phone": phone}
    if dnc:
        d_http, d, d_raw = res["dnc"]
        d = d if isinstance(d, dict) else {}
        if d_http != 200 or "status" not in d:
            return {"status": "api-error", "error_text": f"DNC Plus HTTP {d_http} {d_raw[:120]}"}
        merged.update({"status": d.get("status"), "error_text": d.get("error_text", ""), "iscell": d.get("iscell"),
                       "national_dnc": d.get("national_dnc"), "state_dnc": d.get("state_dnc"),
                       "dma": d.get("dma"), "litigator": d.get("litigator")})
    if tw:
        t_http, t, _ = res["tw"]
        t = t if isinstance(t, dict) else {}
        cn = t.get("caller_name") if isinstance(t.get("caller_name"), dict) else None
        if t_http == 200 and cn is not None:
            merged.update({"caller_name": cn.get("caller_name"), "caller_type": cn.get("caller_type")})
        else:
            merged["twilio_error"] = f"HTTP {t_http}"
    f = run.RAW / TOOL / f"{phone}.json"
    try:
        saved = json.loads(f.read_text()) if f.exists() else {}
    except (OSError, ValueError):
        saved = {}
    saved.update({k: v for k, v in merged.items() if k != "twilio_error"})
    run.save_raw(TOOL, phone, saved)
    return merged


def api_call(phone, creds, dnc=True, tw=True):
    for attempt in (1, 2):
        m = merged_lookup(phone, creds, dnc, tw)
        if m.get("status") != "api-error" or attempt == 2:
            return m
        time.sleep(1)


# ---------- the real_validation cache table ----------
def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def cache_ready(cfg):
    """False when the Supabase project has no real_validation table: the cache is optional."""
    try:
        supabase.run_sql(cfg, f"select 1 from {TABLE} limit 1")
        return True
    except RuntimeError as e:
        if "does not exist" in str(e) or "42P01" in str(e):
            return False
        sys.exit(f"Supabase lookup failed: {e}")


def cache_lookup(phones, cfg):
    """{phone: cache row} for phones present in real_validation."""
    found = {}
    phones = sorted(set(phones))
    for i in range(0, len(phones), LOOKUP_BATCH):
        chunk = phones[i:i + LOOKUP_BATCH]
        try:
            rows = supabase.run_sql(cfg, "select phone, phone_status, line_type, caller_name, caller_type, carrier, natdnc, stdnc, dma, litigator "
                                         f"from {TABLE} where phone in ({','.join(q(p) for p in chunk)})")
        except RuntimeError as e:
            sys.exit(f"Supabase lookup failed: {e}")
        for h in rows:
            found[str(h["phone"])] = h
    return found


def upsert(cfg, records):
    """Insert new numbers / update existing ones (a NULL never erases a stored value; created_at set on insert)."""
    if not records:
        return True
    ok = True
    cols = ["phone_status", "line_type", "caller_name", "caller_type", "carrier", "natdnc", "stdnc", "dma", "litigator"]
    for i in range(0, len(records), WRITE_BATCH):
        vals = ",".join(
            f"({q(r['phone'])},{q(r['phone_status'])},{q(r['line_type'])},{q(r['caller_name'])},{q(r['caller_type'])},{q(r['carrier'])},"
            f"{qb(r['natdnc'])},{qb(r['stdnc'])},{qb(r['dma'])},{qb(r['litigator'])},{q(now())},{q(r['updated_at'])})"
            for r in records[i:i + WRITE_BATCH])
        sets = ", ".join(f"{c} = coalesce(excluded.{c}, real_validation.{c})" for c in cols)
        query = (f"insert into {TABLE} (phone, {', '.join(cols)}, created_at, updated_at) values {vals} "
                 f"on conflict (phone) do update set {sets}, updated_at = excluded.updated_at")
        try:
            supabase.run_sql(cfg, query)
        except RuntimeError as e:
            print(f"WARNING: cache write failed: {e}", file=sys.stderr)
            ok = False
    return ok


# ---------- logic ----------
def needs(hit, force=False):
    """(need_dnc, need_twilio) for one number, from what the cache already holds."""
    if force or not hit:
        return True, True
    status = (hit.get("phone_status") or "").strip()
    ltype = (hit.get("line_type") or "").strip()
    name = (hit.get("caller_name") or "").strip()
    dead = bool(status) and not status.startswith("connected")
    return (not status) or (not dead and not ltype), (not name) and not dead


def record_from(phone, m, hit):
    """What the cache row should hold after this call: new information added, newer non-empty values win."""
    old = hit or {}
    has = lambda v: v is not None and str(v).strip() != ""
    new_name = m.get("caller_name")
    if has(new_name):
        name = new_name.strip()
    elif has(old.get("caller_name")):
        name = old["caller_name"].strip()
    elif "caller_name" in m:
        name = "Unavailable"          # Twilio answered and has no name: remember it so it is not re-billed
    else:
        name = None                   # Twilio not called, or it failed: leave blank so a later run retries it
    ctype = m.get("caller_type")
    cols = {"national_dnc": "natdnc", "state_dnc": "stdnc", "dma": "dma", "litigator": "litigator"}
    flag = lambda key: FLAG[m[key]] if m.get(key) in FLAG else old.get(cols[key])   # Y/N from the API, else keep what the cache has
    return {
        "phone": phone,
        "phone_status": m.get("status") if has(m.get("status")) else old.get("phone_status"),
        "line_type": old["line_type"] if has(old.get("line_type")) else IS_CELL.get(m.get("iscell")),
        "caller_name": name,
        "caller_type": ctype if has(ctype) and ctype != "UNDETERMINED" else (old.get("caller_type") if has(old.get("caller_type")) else ctype),
        "carrier": old.get("carrier"),
        "natdnc": flag("national_dnc"), "stdnc": flag("state_dnc"), "dma": flag("dma"), "litigator": flag("litigator"),
        "updated_at": now(),
    }


def final_of(rec):
    """What the sheet needs, from a cache row or a fresh record (they share column names)."""
    return {"status": rec.get("phone_status") or "", "line_type": rec.get("line_type") or "",
            "caller_name": (rec.get("caller_name") or "").strip(), "litigator": rec.get("litigator") is True,
            "flags": [rec.get(k) for k in ("litigator", "natdnc", "stdnc", "dma")]}


def resolve(rows_phones, cfg, creds, dry=False, force=False):
    """Return ({phone: final dict}, stats). Cache first; each service is called only for the information that is missing."""
    phones = sorted(set(rows_phones))
    hits = cache_lookup(phones, cfg) if cfg else {}
    final, need = {}, {}
    for p in phones:
        h = hits.get(p)
        nd, nt = needs(h, force)
        if nd or nt:
            need[p] = (nd, nt)
        else:
            final[p] = final_of(h)
    stats = {"phones": len(phones), "from_table": len(final), "need_api": len(need),
             "need_dnc": sum(1 for v in need.values() if v[0]), "need_tw": sum(1 for v in need.values() if v[1]),
             "new": sum(1 for p in need if p not in hits), "partial": sum(1 for p in need if p in hits),
             "api_calls": 0, "dnc_calls": 0, "tw_calls": 0, "failed": [], "stopped": None, "table_ok": True}
    if dry:
        return final, stats

    pend = []

    def flush():
        ok = upsert(cfg, pend) if cfg else True
        stats["table_ok"] = stats["table_ok"] and ok
        pend.clear()

    try:
        for p, (nd, nt) in need.items():
            resp = api_call(p, creds, nd, nt)
            stats["api_calls"] += 1
            stats["dnc_calls"] += nd
            stats["tw_calls"] += nt
            st = resp.get("status")
            if st == "unauthorized":
                stats["stopped"] = resp.get("error_text") or "unauthorized"
                break
            if st == "api-error" or (nd and not st):
                print(f"FAILED {p}: {resp.get('error_text') or resp}", file=sys.stderr)
                stats["failed"].append(p)
                continue
            rec = record_from(p, resp, hits.get(p))
            pend.append(rec)
            final[p] = final_of(rec)
            if len(pend) >= FLUSH_EVERY:
                flush()
            time.sleep(0.12)  # RealPhoneValidation allows <= 10 calls/sec
    finally:
        flush()  # the cache is updated before the sheet is written
    return final, stats


def fill(row, lead, f, cols):
    st, ltype, name = f["status"], f["line_type"], f["caller_name"]
    row[cols[0]] = st
    row[cols[1]] = TYPE_MAP.get(ltype.lower(), ltype)
    row[cols[2]] = name
    if f["litigator"]:
        row[cols[3]] = -25                            # litigator true: always -25, trumps every name match
    elif st.lower().startswith("disconnected"):
        row[cols[3]] = 0
    elif name:
        try:
            s = score({**lead, "status": st, "caller_name": name})
        except ValueError:                            # the lead has no owner or business name to compare against
            s = ""
        if s != "" and s < FLOOR and st.lower().startswith("connected") and shares_with_lead(name, lead):
            s = FLOOR                                 # the caller name shares something with the lead
        row[cols[3]] = s
    else:
        row[cols[3]] = ""
    for col, value in zip(cols[4:], f["flags"]):
        row[col] = "" if value is None else str(bool(value)).lower()


def main(argv=None):
    ap = run.parser(__doc__, INPUTS)
    ap.add_argument("--biz", action="store_true", help="shorthand for --phone-col business_phone --prefix biz_")
    args = ap.parse_args(argv)
    if args.biz:
        args.phone_col, args.prefix = "business_phone", "biz_"
    src = run.open_source(args, INPUTS)
    cols = [args.prefix + c for c in OUT]
    src.ensure(cols)
    single = src.kind == "single"
    targets, n_done, n_bad = run.pick(
        args, src, INPUTS, OUT, lambda v: bool(run.phone10(v["phone"])), redo=args.force or bool(args.ids),
        todo=lambda p: not p["status"] or (p["status"].startswith("connected") and not (p["phone_type"] and p["caller_name"])))
    if single and not targets:
        sys.exit("phone_validate needs a 10-digit US number in --phone.")
    cfg = supabase.config()
    if cfg and not cache_ready(cfg):
        cfg = None
        print("WARNING: this Supabase project has no real_validation table. Running API-only: nothing is cached.", file=sys.stderr)
    elif not cfg:
        print("WARNING: SUPABASE_ACCESS_TOKEN / SUPABASE_PROJECT_REF are blank in .env. Running API-only: "
              "the real_validation cache is NOT read or updated.", file=sys.stderr)
    creds = None if args.dry_run else env.need("REALVALIDATION_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
    final, st = resolve([run.phone10(v["phone"]) for _, v in targets], cfg, creds, dry=args.dry_run, force=args.force)
    if args.dry_run:
        print(f"{TOOL} (dry run): {len(targets)} rows, {st['phones']} unique numbers | free from cache: {st['from_table']} | "
              f"need the API: {st['need_api']} ({st['new']} not in cache, {st['partial']} partly there) | "
              f"RealPhoneValidation calls: {st['need_dnc']} | Twilio calls: {st['need_tw']} | "
              f"{n_done} rows already done | {n_bad} rows without a usable number | nothing written")
        return
    for r, v in targets:
        f = final.get(run.phone10(v["phone"]))
        if not f:
            continue
        fill(r, {k: v[k] for k in LEAD}, f, cols)
        src.write(r, cols)
        if not single:
            print(f"{TOOL} {run.phone10(v['phone'])}: " + " | ".join(str(r[c]) for c in cols[:4]) + (" | LITIGATOR" if f["litigator"] else ""))
    src.flush()
    if single:
        print(json.dumps({c: src.rows[0].get(c, "") for c in cols}, indent=1))
    else:
        print(f"{TOOL}: {len(targets)} rows | from cache: {st['from_table']} (free) | numbers sent to an API: {st['api_calls']} "
              f"(RealPhoneValidation {st['dnc_calls']}, Twilio {st['tw_calls']}; {st['new']} new, {st['partial']} updated) | "
              f"failed: {len(st['failed'])} | cache {'updated' if cfg and st['table_ok'] else 'NOT updated'} | "
              f"{n_done} rows already done | {n_bad} rows without a usable number | saved to {src.where}")
    if st["failed"]:
        print("failed (left blank): " + " ".join(st["failed"]), file=sys.stderr)
    if st["stopped"]:
        sys.exit(f"STOPPED: {st['stopped']} (what was done is saved)")


if __name__ == "__main__":
    main()
