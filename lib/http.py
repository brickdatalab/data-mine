"""The one place every tool sends an HTTP request from."""
import json
import urllib.error
import urllib.parse
import urllib.request


def call(method, url, headers=None, params=None, body=None, timeout=30):
    """-> (HTTP status, parsed JSON or None, raw text). Error responses keep their body; a network failure is status 0."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "data-mine/1.0", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, None, str(e)
    try:
        return status, json.loads(raw), raw
    except ValueError:
        return status, None, raw
