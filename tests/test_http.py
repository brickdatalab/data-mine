"""lib.http.call against a real local server: the one piece every other test replaces with a fake."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import helpers  # noqa: F401  (puts the project on sys.path)
from lib import http


class Handler(BaseHTTPRequestHandler):
    def reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        seen = {"method": self.command, "path": self.path, "key": self.headers.get("x-api-key"),
                "body": json.loads(self.rfile.read(length)) if length else None}
        status = int(self.path.split("/")[1])
        payload = b"not json" if "text" in self.path else json.dumps(seen).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = reply

    def log_message(self, *a):
        pass


class HttpCallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_sends_headers_and_query_params_and_parses_json(self):
        status, data, raw = http.call("GET", self.base + "/200/x", headers={"x-api-key": "k"}, params={"phone": "650 823"})
        self.assertEqual((status, data["key"], data["path"]), (200, "k", "/200/x?phone=650+823"))

    def test_post_sends_a_json_body(self):
        status, data, _ = http.call("POST", self.base + "/201/x", body={"tin": "811234567"})
        self.assertEqual((status, data["method"], data["body"]), (201, "POST", {"tin": "811234567"}))

    def test_error_status_keeps_its_body(self):
        status, data, _ = http.call("GET", self.base + "/422/x")
        self.assertEqual((status, data["path"]), (422, "/422/x"))

    def test_http_249_is_returned_as_249(self):
        self.assertEqual(http.call("GET", self.base + "/249/x")[0], 249)

    def test_non_json_body_gives_none_and_the_raw_text(self):
        self.assertEqual(http.call("GET", self.base + "/200/text")[1:], (None, "not json"))

    def test_unreachable_host_is_status_zero(self):
        self.assertEqual(http.call("GET", "http://127.0.0.1:1/x", timeout=2)[:2], (0, None))


if __name__ == "__main__":
    unittest.main()
