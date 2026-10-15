"""End-to-end check of the localhost transport that replaced the pywebview bridge.

Drives the real serve() on a background thread with the browser launch stubbed
out, then exercises static files, the JSON-RPC endpoint and the SSE event
stream. Run it with the repo root as cwd:

    python tools/smoke_rpc.py
"""

import http.server
import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# launcher.log accumulates and launcher.py reads this at import time, so a run
# against a shared data dir would read the *previous* run's port.
os.environ["LODESTONE_DATA"] = tempfile.mkdtemp(prefix="lodestone-smoke-")

import LodeStone
from LodeStone import paths as launcher_paths  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def read_url():
    """Wait for serve() to announce its port, then recover the token."""
    log = launcher_paths.LOG_FILE
    for _ in range(100):
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                if "UI listening on" in line:
                    return line.split("UI listening on", 1)[1].strip()
        time.sleep(0.1)
    return None


def read_sse_frame(resp):
    """Read one SSE frame the way a browser does.

    http.client strips the chunk framing now that Transfer-Encoding is declared
    correctly, so this only has to honour the `data:` prefix. Skipping that step
    is what let a stream with no SSE fields at all look healthy.
    """
    raw = resp.readline()
    while raw in (b"\n", b"\r\n", b""):   # frames are separated by a blank line
        raw = resp.readline()
    assert raw.startswith(b"data: "), f"not an SSE data frame: {raw[:40]!r}"
    return json.loads(raw[len(b"data: "):].strip())


def post(port, token, method, args=None, headers=None, ctype="application/json"):
    body = json.dumps({"method": method, "args": args or []}).encode()
    hdrs = {"Content-Type": ctype}
    if token is not None:
        hdrs["X-Lodestone-Token"] = token
    hdrs.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}/rpc", data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:120]


def main():
    opened = []
    webbrowser.open = lambda url: opened.append(url)

    print("smoke: starting launcher transport")
    thread = threading.Thread(target=LodeStone.serve, daemon=True)
    thread.start()

    url = read_url()
    if not url:
        print("  FAIL  launcher never reported a URL (see launcher.log)")
        return 1
    check("serve() reports a listening URL", True, url)

    base, _, frag = url.partition("#")
    port = int(base.rstrip("/").rsplit(":", 1)[1])
    token = frag
    check("binds loopback only", base.startswith("http://127.0.0.1:"))
    check("opens the browser at the tokenised URL", opened == [url])
    check("token is not in the path", "#" in url and token and token not in base)

    # --- static files ---
    # HTML doctype is case-insensitive; PNG magic bytes are not, and .lower()
    # would corrupt them.
    for path, needle, label, fold in (
        ("/", b"<!doc", "serves the UI at /", True),
        ("/index.html", b"<!doc", "serves the UI at /index.html", True),
        ("/logo.png", b"\x89PNG", "serves logo.png", False),
        ("/background.png", b"\x89PNG", "serves background.png", False),
    ):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            body = r.read(64)
        probe = body[:16].lower() if fold else body[:16]
        check(label, probe.startswith(needle), f"status {r.status}")

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=10)
        check("unknown path is 404", False, "no error raised")
    except urllib.error.HTTPError as e:
        check("unknown path is 404", e.code == 404, f"status {e.code}")

    # --- RPC ---
    status, payload = post(port, token, "get_state")
    ok = (status == 200 and isinstance(payload, dict)
          and payload.get("success") and "instances" in payload.get("state", {}))
    check("valid call returns the method's own JSON", ok, f"status {status}")

    status, payload = post(port, token, "get_disk_usage", ["instances"])
    ok = status == 200 and payload.get("success") and isinstance(payload.get("sizes"), dict)
    check("arguments are passed through", ok, f"status {status}")

    status, payload = post(port, token, "get_disk_usage")
    ok = status == 500 and payload.get("success") is False
    check("a missing required argument fails loudly", ok, f"status {status}")

    status, _ = post(port, token, "_state_dict")
    check("private methods are not reachable", status == 404, f"status {status}")

    status, _ = post(port, token, "no_such_method")
    check("unknown methods are not reachable", status == 404, f"status {status}")

    status, _ = post(port, "wrong-token", "get_state")
    check("a bad token is refused", status == 403, f"status {status}")

    status, _ = post(port, None, "get_state")
    check("a missing token is refused", status == 403, f"status {status}")

    status, _ = post(port, token, "get_state", headers={"Origin": "http://evil.example"})
    check("a foreign Origin is refused", status == 403, f"status {status}")

    status, _ = post(port, token, "get_state",
                     headers={"Origin": f"http://127.0.0.1:{port}"})
    check("our own Origin is allowed", status == 200, f"status {status}")

    status, _ = post(port, token, "get_state", ctype="text/plain")
    check("a non-JSON content type is refused", status == 415, f"status {status}")

    status, payload = post(port, token, "get_disk_usage", ["servers"])
    check("a second target round-trips", status == 200, f"status {status}")

    # --- SSE ---
    req = urllib.request.Request(f"http://127.0.0.1:{port}/events?token={token}")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        first = read_sse_frame(resp)
        ok = first["name"] == "stateChanged" and "instances" in first["data"]
        check("SSE opens and sends a state snapshot", ok, str(first.get("name")))

        # A pushed event must arrive on the same stream, with the same framing.
        status, _ = post(port, token, "push_state")
        check("push_state accepted", status == 200, f"status {status}")
        pushed = read_sse_frame(resp)
        ok = pushed["name"] == "stateChanged" and "instances" in pushed["data"]
        check("SSE delivers a pushed event", ok, str(pushed.get("name")))
        resp.close()
    except Exception as exc:
        check("SSE stream", False, repr(exc))

    req = urllib.request.Request(f"http://127.0.0.1:{port}/events?token=wrong")
    try:
        urllib.request.urlopen(req, timeout=10)
        check("SSE refuses a bad token", False, "it accepted the connection")
    except urllib.error.HTTPError as e:
        check("SSE refuses a bad token", e.code == 403, f"status {e.code}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
