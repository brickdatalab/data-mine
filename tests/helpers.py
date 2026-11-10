"""Shared test setup: no network, no real .env, no real sleeping, raw/ redirected to a temp folder."""
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

KEYS = {k: "test-" + k.lower() for k in (
    "REALVALIDATION_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TRESTLE_API_KEY", "ENFORMION_AP_NAME",
    "ENFORMION_AP_PASSWORD", "HUNTER_API_KEY", "EMAILABLE_API_KEY", "MIDDESK_API_KEY")}
SUPABASE_KEYS = {"SUPABASE_ACCESS_TOKEN": "test-pat", "SUPABASE_PROJECT_REF": "testref"}


class FakeHttp:
    """Stands in for lib.http.call. routes: list of ((METHOD, url fragment), response).
    response is (status, json), a list of those (served in order, last one repeats), or a function(method, url, params, body)."""

    def __init__(self, routes):
        self.routes = [(m, frag, [r] if isinstance(r, tuple) else r) for (m, frag), r in routes]
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, body=None, timeout=30):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "params": params or {}, "body": body})
        for m, frag, resp in self.routes:
            if m == method and frag in url:
                if callable(resp):
                    status, data = resp(method, url, params, body)
                else:
                    status, data = resp.pop(0) if len(resp) > 1 else resp[0]
                return status, data, json.dumps(data)
        raise AssertionError(f"unexpected call: {method} {url}")

    def count(self, method, frag):
        return sum(1 for c in self.calls if c["method"] == method and frag in c["url"])

    def last(self, method, frag):
        return [c for c in self.calls if c["method"] == method and frag in c["url"]][-1]


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self._patch("lib.env.load_env", return_value=dict(KEYS))
        self._patch("lib.run.RAW", self.tmp / "raw")
        self._patch("time.sleep")

    def _patch(self, target, *a, **kw):
        p = mock.patch(target, *a, **kw)
        self.addCleanup(p.stop)
        return p.start()

    def fake(self, routes):
        self.http = FakeHttp(routes)
        self._patch("lib.http.call", self.http)
        return self.http

    def csv(self, name, header, rows):
        path = self.tmp / name
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return path

    def read_csv(self, path):
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    def run_tool(self, module, *argv):
        """Run a tool's main(); returns (stdout text, exit code or exit message or None)."""
        out, code = io.StringIO(), None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                module.main([str(a) for a in argv])
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code
