#!/usr/bin/env python3
"""
PROOF: the /quote route (F3 no-price fire anchor, spec 2026-07-02) is READ-ONLY, token-gated,
and passes the broker quote / EA-down refusal through verbatim.

Fidelity: drives the REAL shipped handler over a loopback HTTPServer with the real shared-secret
token, exactly like test_charged_sl_rederive.py. ONLY broker I/O is faked:
  - MT5DemoBackend.__init__  -> no-op (don't start the real :16275 bridge)
  - MT5DemoBackend.get_quote -> controlled bid/ask OR an ea_not_live refusal
"""
import sys, os, json, threading, urllib.request, urllib.error
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

_EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _EXT)
import oas_execute_mcp.backends.mt5_demo as m

TOKEN = "TESTTOKEN123"

def fake_init(self):
    return None
def make_fake_quote(bid, ask, status="ok", reason=None):
    def _q(self, symbol):
        if status != "ok":
            return {"status": status, "reason": reason or "feed_down"}
        return {"status": "ok", "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0, "symbol": symbol}
    return _q

m.MT5DemoBackend.__init__ = fake_init

fake_bridge = SimpleNamespace(_submit_token=TOKEN)
handler_cls = m._make_handler(fake_bridge)
srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def post_quote(token=None, body=b"{}", extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Submit-Token"] = token
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/quote", data=body,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

results = []
def check(name, ok, detail):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

print("Driving the REAL /quote handler over loopback (broker I/O faked)\n")

# A — no token → 403 (the keystroke-auth layer)
code, r = post_quote(token=None)
check("A no-token 403", code == 403 and r.get("reason") == "bad_or_missing_submit_token",
      f"http={code} reason={r.get('reason')}")

# B — wrong token → 403
code, r = post_quote(token="WRONG")
check("B wrong-token 403", code == 403, f"http={code}")

# C — browser Origin header → 403 (DNS-rebind guard holds on the read-only route too)
code, r = post_quote(token=TOKEN, extra_headers={"Origin": "http://evil.example"})
check("C origin 403", code == 403 and "browser_origin_refused" in r.get("reason", ""),
      f"http={code} reason={r.get('reason')}")

# D — good token + live quote → 200 with bid/ask/mid passthrough
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29900.0, ask=29901.0)
code, r = post_quote(token=TOKEN)
check("D live quote 200", code == 200 and r.get("status") == "ok"
      and r.get("bid") == 29900.0 and r.get("ask") == 29901.0 and r.get("mid") == 29900.5,
      f"http={code} q={r}")

# E — EA down → 200 with the refusal passed through (the client's fail-closed signal)
m.MT5DemoBackend.get_quote = make_fake_quote(0, 0, status="rejected", reason="ea_not_live: heartbeat_stale")
code, r = post_quote(token=TOKEN)
check("E ea-down passthrough", code == 200 and r.get("status") == "rejected"
      and "ea_not_live" in r.get("reason", ""),
      f"http={code} status={r.get('status')} reason={r.get('reason')}")

# F — bad JSON body → 400
code, r = post_quote(token=TOKEN, body=b"{not json")
check("F bad-json 400", code == 400, f"http={code} reason={r.get('reason')}")

# G — custom symbol forwarded
seen = {}
def spy_quote(self, symbol):
    seen["symbol"] = symbol
    return {"status": "ok", "bid": 1.0, "ask": 2.0, "mid": 1.5, "symbol": symbol}
m.MT5DemoBackend.get_quote = spy_quote
code, r = post_quote(token=TOKEN, body=json.dumps({"symbol": "GER40.cash"}).encode())
check("G symbol forwarded", code == 200 and seen.get("symbol") == "GER40.cash",
      f"symbol_seen={seen.get('symbol')}")

srv.shutdown()
n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n=== {n_pass}/{len(results)} checks passed ===")
sys.exit(0 if n_pass == len(results) else 1)
