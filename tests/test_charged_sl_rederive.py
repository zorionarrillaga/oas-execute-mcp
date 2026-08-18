#!/usr/bin/env python3
"""
PROOF: System-H charged-gun SL re-derive behaves as the trader specified —
charge fixes the SL *distance* (risk); the actual SL *price* is re-anchored off the
LIVE broker fill, and a drift beyond max_slip HARD-REFUSES.

Fidelity: drives the REAL shipped handler `_handle_submit` (mt5_demo.py) over a loopback
HTTPServer with the real shared-secret token. ONLY broker I/O is faked:
  - MT5DemoBackend.__init__   -> no-op (don't start the real :16275 bridge)
  - MT5DemoBackend.get_quote  -> a controlled bid/ask (stand-in for the broker quote op)
  - MT5DemoBackend.submit_order -> CAPTURES the sl_price the route actually submits
  - safety.run_entry_safety_gates -> pass; safety.append_audit -> no-op
The drift-refuse + SL re-derive ARITHMETIC under test is the unmodified shipped code.
This proves the LOGIC; it is NOT the live broker round-trip (that remains the n=0 gap).
"""
import sys, os, json, threading, urllib.request, urllib.error
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

_EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _EXT)
import oas_execute_mcp.backends.mt5_demo as m
from oas_execute_mcp import safety, config

# Hermeticity (2026-07-23, auto-binding build): the drift block now reads the CHANNEL from
# config.GATE_PASS_PATH — point it at a path that never exists so this suite's advisory-fill
# scenarios can't flip if a live session leaves channel:auto in the real data/state file.
config.GATE_PASS_PATH = type(config.GATE_PASS_PATH)("/nonexistent/test_no_gate_pass.json")

TOKEN = "TESTTOKEN123"
CAPTURED = {}   # submit_order args land here

# ---- fake broker I/O (the only thing stubbed) ----
def fake_init(self):            # don't start the real bridge
    return None
def make_fake_quote(bid, ask, status="ok"):
    def _q(self, symbol):
        if status != "ok":
            return {"status": status, "reason": "feed_down"}
        return {"status": "ok", "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0}
    return _q
def fake_submit_order(self, symbol, side, lots, sl_price, tp_price, decision_ref):
    CAPTURED.clear()
    CAPTURED.update(dict(symbol=symbol, side=side, lots=lots, sl_price=sl_price,
                         tp_price=tp_price, decision_ref=decision_ref))
    return {"status": "submitted", "broker_order_id": 999, "fill_price": None,
            "sl_price": sl_price, "tp_price": tp_price}

m.MT5DemoBackend.__init__ = fake_init
m.MT5DemoBackend.submit_order = fake_submit_order
safety.run_entry_safety_gates = lambda intent, backend: (True, {"stub": "pass"})
AUDITED = []   # capture submit intents so we can assert the slip-advisory flag
safety.append_audit = lambda op, intent, checks, result: AUDITED.append(intent)

# ---- stand up the REAL handler on loopback ----
fake_bridge = SimpleNamespace(_submit_token=TOKEN)
handler_cls = m._make_handler(fake_bridge)
srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def post_submit(payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit",
        data=json.dumps(payload).encode(), method="POST",
        headers={"X-Submit-Token": TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())

# ---- scenarios ----
# Charge: LONG, entry_ref 29528, structural SL 29495 (distance 33pt), max_slip 15, tp 29700
BASE = dict(symbol="US100.cash", side="long", lots=2.0, tp_price=29700.0,
            decision_ref="TEST-SL", entry_ref=29528.0, max_slip_pt=15.0,
            sl_price=29495.0, sl_distance_pt=33.0)
DIST = 33.0
results = []
def check(name, ok, detail):
    results.append((name, ok, detail));
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

print("Driving the REAL _handle_submit over loopback (broker I/O faked)\n")

# A — no drift: fill at the charged entry → SL re-derives to the charged price; risk == 33
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29527.0, ask=29528.0)
r = post_submit(dict(BASE))
sl = CAPTURED.get("sl_price"); spot = 29528.0
check("A no-drift", r.get("status") == "submitted" and abs(spot - sl) == DIST and sl == 29495.0,
      f"status={r.get('status')} sl={sl} risk={abs(spot-sl)}pt (expect sl=29495, risk=33)")

# B — FAVORABLE drift +10 within slip: risk preserved (33), but structural SL WALKS UP +10
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29537.0, ask=29538.0)
r = post_submit(dict(BASE))
sl = CAPTURED.get("sl_price"); spot = 29538.0
risk_ok = abs(spot - sl) == DIST
walked = sl - 29495.0   # how far the stop moved off the charged structural level
check("B favorable-drift risk-preserved", r.get("status") == "submitted" and risk_ok,
      f"sl={sl} risk={abs(spot-sl)}pt (expect risk=33)")
check("B structural-walk DEMONSTRATED", abs(walked - 10.0) < 1e-6,
      f"stop walked +{walked}pt above the charged 29495 floor (the open-question exposure)")

# C — ADVERSE drift -10 within slip: risk preserved (33), SL moves down 10
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29517.0, ask=29518.0)
r = post_submit(dict(BASE))
sl = CAPTURED.get("sl_price"); spot = 29518.0
check("C adverse-drift risk-preserved", r.get("status") == "submitted" and abs(spot - sl) == DIST,
      f"sl={sl} risk={abs(spot-sl)}pt (expect risk=33)")

# D — drift +20 BEYOND max_slip(15): SLIP CAP IS ADVISORY (2026-06-30) — FILLS, re-derives,
#     flags slip_advisory_exceeded in the audited intent. NOT a refuse anymore (trader owns slip).
CAPTURED.clear(); AUDITED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29547.0, ask=29548.0)
r = post_submit(dict(BASE))
sl = CAPTURED.get("sl_price"); spot = 29548.0  # drift 20 > max_slip 15
flagged = any((i.get("drift_check") or {}).get("slip_advisory_exceeded") is True for i in AUDITED)
check("D drift-beyond-slip now FILLS (advisory, not block) + risk preserved",
      r.get("status") == "submitted" and abs(spot - sl) == DIST,
      f"status={r.get('status')} sl={sl} risk={abs(spot-sl)}pt (expect FILL, risk=33)")
check("D slip_advisory_exceeded flagged in audit", flagged,
      f"slip_advisory_exceeded={flagged} (expect True — the slip is recorded, not silently dropped)")

# E — SHORT side: entry 29528, SL 29561 (dist 33), bid drifts to 29520 (drift 8 ≤15)
short = dict(BASE); short.update(side="short", sl_price=29561.0, tp_price=29400.0, decision_ref="TEST-SL-S")
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29520.0, ask=29521.0)
r = post_submit(short)
sl = CAPTURED.get("sl_price"); spot = 29520.0  # short fills at bid
check("E short risk-preserved", r.get("status") == "submitted" and abs(sl - spot) == DIST and sl == 29553.0,
      f"sl={sl} risk={abs(sl-spot)}pt (expect sl=29553 = bid+33)")

# F — broker quote UNAVAILABLE: fail-closed HARD-REFUSE, no order
CAPTURED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=0, ask=0, status="error")
r = post_submit(dict(BASE))
check("F no-quote FAIL-CLOSED", r.get("status") == "rejected"
      and "broker_quote_unavailable" in r.get("reason", "") and not CAPTURED,
      f"status={r.get('status')} order_placed={bool(CAPTURED)}")

# G — raw S1a (no charge: omit entry_ref/max_slip) → re-derive SKIPPED, SL used as-is
CAPTURED.clear()
raw = dict(symbol="US100.cash", side="long", lots=2.0, sl_price=29400.0,
           tp_price=29700.0, decision_ref="TEST-RAW")
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29537.0, ask=29538.0)
r = post_submit(raw)
check("G uncharged passes SL through untouched", r.get("status") == "submitted"
      and CAPTURED.get("sl_price") == 29400.0,
      f"sl={CAPTURED.get('sl_price')} (expect 29400 unchanged — no charge, no re-derive)")

srv.shutdown()
n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n=== {n_pass}/{len(results)} checks passed ===")
sys.exit(0 if n_pass == len(results) else 1)
