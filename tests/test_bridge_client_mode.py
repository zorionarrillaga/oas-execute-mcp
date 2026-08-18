#!/usr/bin/env python3
"""test_bridge_client_mode.py — the :16275 collision fix (2026-07-28).

THE DEFECT THIS PINS. The EA polls exactly ONE 127.0.0.1:16275, but that listener was hosted
inside a PER-SESSION MCP process. A second Claude session's server raised OSError(EADDRINUSE) in
_Bridge.start() and then every mcp__oas-execute__* call failed with Errno 48 — while oas_smoke
reported 0 FAIL, because the bridge itself was perfectly healthy. Account reads and position polls
were dead in that window. The standing mitigation was a NOUN: "quit the other window".

Now the loser attaches as a CLIENT of the owner. These tests drive a REAL owner + REAL client over
a scratch port (OAS_BRIDGE_PORT) — never :16275, which a live session owns.

Run: PYTHONPATH=. python3 tests/test_bridge_client_mode.py
"""
import json
import os
import socket
import sys
import threading
import urllib.request
from pathlib import Path

_EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _EXT)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
os.environ["OAS_BRIDGE_PORT"] = str(PORT)

from oas_execute_mcp import config  # noqa: E402
from oas_execute_mcp.backends import mt5_demo  # noqa: E402

RESULTS = []


def ok(msg):
    RESULTS.append(True)
    print(f"  ✓ {msg}")


def bad(msg):
    RESULTS.append(False)
    print(f"  ✗ {msg}")


def post(path, payload, token, timeout=5.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json", "X-Submit-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# ── Fake EA: answers the owner's queue so forwarded commands actually resolve ────────────────
def fake_ea(owner, stop):
    """Poll like the real EA does and immediately POST a result back, so a forwarded command
    completes end-to-end instead of timing out."""
    while not stop.is_set():
        pending = owner.pop_next()
        if pending is None:
            stop.wait(0.02)
            continue
        corr = ""
        for line in pending.body.splitlines():
            if line.startswith("correlation_id="):
                corr = line.split("=", 1)[1]
        owner.resolve(corr, {"status": "ok", "echo_op": "seen", "correlation_id": corr})


print("── 1. owner binds; a SECOND bridge does NOT raise, it attaches as a client ──")
owner = mt5_demo._Bridge()
owner.start()
if owner._server is not None and not owner._client_mode:
    ok("owner bound the port and is NOT in client mode")
else:
    bad("owner failed to bind")

token_after_owner = config.SUBMIT_TOKEN_PATH.read_text().strip()

stop = threading.Event()
threading.Thread(target=fake_ea, args=(owner, stop), daemon=True).start()

client = mt5_demo._Bridge()
try:
    client.start()
    if client._client_mode:
        ok("second bridge entered CLIENT MODE instead of raising Errno 48 (the defect)")
    else:
        bad("second bridge did not enter client mode")
except OSError as e:
    bad(f"second bridge RAISED — the defect is unfixed: {e}")

print("── 2. the token is NOT clobbered (the 2026-07-02 orphaning of go.sh/charge.sh) ──")
if config.SUBMIT_TOKEN_PATH.read_text().strip() == token_after_owner:
    ok("client did not overwrite submit_token.txt — go.sh stays bound to the owner")
else:
    bad("client CLOBBERED the submit token — go.sh/charge.sh would 403 mid-session")

print("── 3. a real command forwards through the owner and returns the EA result ──")
resp = client.submit({"op": "account_info"}, timeout_sec=5.0)
if isinstance(resp, dict) and resp.get("status") == "ok":
    ok("client-mode account_info round-tripped via the owner (Errno 48 path now WORKS)")
else:
    bad(f"client-mode forward failed: {resp!r}")
if resp.get("_bridge_mode") == "client":
    ok("response is TAGGED _bridge_mode=client (the mode is never invisible)")
else:
    bad("response not tagged with the bridge mode")

print("── 4. /cmd REFUSES op=submit — capital cannot bypass the owner's entry gates ──")
code, body = post("/cmd", {"fields": {"op": "submit", "symbol": "X", "side": "buy", "lots": 1}},
                  token_after_owner)
if code == 403 and "op_submit_refused" in str(body.get("reason", "")):
    ok("/cmd refuses op=submit server-side (independent of the client-side router)")
else:
    bad(f"/cmd ACCEPTED op=submit — gate bypass! code={code} body={body!r}")

print("── 5. /cmd auth: a bad token cannot drive the bridge ──")
code, body = post("/cmd", {"fields": {"op": "account_info"}}, "not-the-token")
if code == 403:
    ok("/cmd rejects a bad submit token")
else:
    bad(f"/cmd accepted a bad token: code={code}")

code, body = post("/cmd", {"fields": {}}, token_after_owner)
if code == 400:
    ok("/cmd rejects a payload with no op (no silent no-op)")
else:
    bad(f"/cmd accepted an empty command: code={code}")

print("── 6. FAIL-CLOSED: port held by a NON-bridge ⇒ the bind error still propagates ──")
stop.set()
owner._server.shutdown()
owner._server.server_close()

squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
squatter.bind(("127.0.0.1", PORT))
squatter.listen(1)

victim = mt5_demo._Bridge()
try:
    victim.start()
    bad("attached to a NON-bridge squatter — capital commands would go somewhere unknown")
except OSError:
    ok("refused to attach to a non-OAS process holding the port (fails closed, loudly)")
except Exception as e:
    bad(f"unexpected error type: {type(e).__name__}: {e}")
squatter.close()

print()
print(f"── RESULT: {sum(RESULTS)} passed, {len(RESULTS) - sum(RESULTS)} failed ──")
sys.exit(0 if all(RESULTS) else 1)
