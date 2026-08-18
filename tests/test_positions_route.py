#!/usr/bin/env python3
"""
PROOF: the /positions route (D037 foreign-fire guard, 2026-07-07) is READ-ONLY, token-gated,
and passes the position list — including per-position magic — through verbatim, and NEVER
fabricates flat on a dead EA (UNKNOWN ≠ FLAT: list_positions raising must surface status=error).

Context: 2026-07-07 a trade fired from the MT5 MOBILE APP (magic 0 — no bin/ gate) ended the
account; the lockdown watcher read only equity and never saw the position. This route is what
lets bin/lockdown/watcher.sh see foreign (magic ≠ DefaultMagic 20260519) positions and close-all.

Fidelity: drives the REAL shipped handler over a loopback HTTPServer with the real shared-secret
token, exactly like test_quote_route.py. ONLY broker I/O is faked:
  - MT5DemoBackend.__init__       -> no-op (don't start the real :16275 bridge)
  - MT5DemoBackend.list_positions -> controlled list OR a raise (dead EA)
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

m.MT5DemoBackend.__init__ = fake_init

fake_bridge = SimpleNamespace(_submit_token=TOKEN)
handler_cls = m._make_handler(fake_bridge)
srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def post_positions(token=None, body=b"{}", extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Submit-Token"] = token
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/positions", data=body,
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

print("Driving the REAL /positions handler over loopback (broker I/O faked)\n")

# A — no token → 403
code, r = post_positions(token=None)
check("A no-token 403", code == 403 and r.get("reason") == "bad_or_missing_submit_token",
      f"http={code} reason={r.get('reason')}")

# B — wrong token → 403
code, r = post_positions(token="WRONG")
check("B wrong-token 403", code == 403, f"http={code}")

# C — browser Origin header → 403 (DNS-rebind guard holds on the read-only route too)
code, r = post_positions(token=TOKEN, extra_headers={"Origin": "http://evil.example"})
check("C origin 403", code == 403 and "browser_origin_refused" in r.get("reason", ""),
      f"http={code} reason={r.get('reason')}")

# D — good token + live EA → 200, positions passed through WITH magic (the foreign-fire signal)
POS = [{"broker_order_id": 274384711, "symbol": "US100.cash", "side": "long", "lots": 6.0,
        "entry_price": 29300.0, "current_price": 29310.0, "sl_price": 29250.0,
        "tp_price": 29600.0, "pnl": 60.0, "magic": 20260519, "comment": "gated"},
       {"broker_order_id": 274384999, "symbol": "US100.cash", "side": "long", "lots": 20.0,
        "entry_price": 29350.0, "current_price": 29318.0, "sl_price": 0.0,
        "tp_price": 0.0, "pnl": -640.0, "magic": 0, "comment": "mobile app"}]
m.MT5DemoBackend.list_positions = lambda self: POS
code, r = post_positions(token=TOKEN)
magics = [p.get("magic") for p in r.get("positions", [])]
check("D positions+magic passthrough", code == 200 and r.get("status") == "ok"
      and r.get("count") == 2 and magics == [20260519, 0],
      f"http={code} count={r.get('count')} magics={magics}")

# E — flat account → 200, count 0, positions []
m.MT5DemoBackend.list_positions = lambda self: []
code, r = post_positions(token=TOKEN)
check("E flat 200 count=0", code == 200 and r.get("status") == "ok"
      and r.get("count") == 0 and r.get("positions") == [],
      f"http={code} r={r}")

# F — dead EA (list_positions RAISES per the 2026-07-02 UNKNOWN≠FLAT doctrine) → status=error,
#     never a fabricated flat
def dead_ea(self):
    raise RuntimeError("position_list refused: ea_not_live — broker positions UNKNOWN, do NOT assume flat")
m.MT5DemoBackend.list_positions = dead_ea
code, r = post_positions(token=TOKEN)
check("F dead-EA → error, not flat", code == 200 and r.get("status") == "error"
      and "UNKNOWN" in r.get("reason", ""),
      f"http={code} status={r.get('status')} reason={r.get('reason', '')[:80]}")

srv.shutdown()
n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n=== {n_pass}/{len(results)} checks passed ===")
sys.exit(0 if n_pass == len(results) else 1)
