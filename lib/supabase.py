"""Supabase access through the Management API's SQL endpoint.
Needs SUPABASE_ACCESS_TOKEN (personal access token) and SUPABASE_PROJECT_REF in .env."""
import sys

from lib import env, http


def config(required=False):
    """(project ref, token), or None when the keys are blank."""
    token, ref = env.get("SUPABASE_ACCESS_TOKEN"), env.get("SUPABASE_PROJECT_REF")
    if token and ref:
        return ref, token
    if required:
        sys.exit("SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF must both be set in .env to use --table.")
    return None


def run_sql(cfg, query):
    ref, token = cfg
    status, data, raw = http.call("POST", f"https://api.supabase.com/v1/projects/{ref}/database/query",
                                  headers={"Authorization": f"Bearer {token}"}, body={"query": query}, timeout=120)
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status} {raw[:300]}")
    return data if isinstance(data, list) else []


def q(v):
    """SQL string literal (or NULL)."""
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("\x00", "").replace("'", "''") + "'"


def qb(v):
    """SQL boolean literal (or NULL)."""
    return "NULL" if v is None else ("true" if v else "false")


def ident(name):
    """Quoted SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'
