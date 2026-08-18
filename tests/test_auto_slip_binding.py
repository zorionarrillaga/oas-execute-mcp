#!/usr/bin/env python3
"""
PROOF: the fill-vs-entry_ref drift cap is BINDING for the AUTO channel and ADVISORY everywhere
else — with the adversarial-review hardenings (2026-07-23 §14): payload-channel authority
(the racy shared gate-pass file can neither bind the GUN nor unbind AUTO), ADVERSE-only
binding (discount fills always fill), and required drift fields on auto (no strip-the-payload
dodge).

WHY each pin exists:
  - payload channel authoritative: charge_keeper rewrites last_gate_pass.json every ~30s and
    both channels' gates write it — a gate-pass-only channel read could hard-refuse the
    trader's gun (2026-06-30 directive violation) or hand auto a "manual" read (cap dodge).
  - adverse-only: abs() drift refused BETTER-than-anchor fills — the live-proven 48.8pt
    discount false-block class (safety.py MUST-FIX #3 history).
  - required fields on auto: without it, omitting entry_ref/max_slip_pt silently disabled the
    "server-side, cannot bypass" cap.

Fidelity: drives the REAL shipped `_handle_submit` over loopback (test_charged_sl_rederive
technique — only broker I/O faked). GATE_PASS_PATH sandboxed (never the live file).
"""
import sys, os, json, tempfile, threading, urllib.request, urllib.error
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

_EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _EXT)
import oas_execute_mcp.backends.mt5_demo as m
from oas_execute_mcp import safety, config

TOKEN = "TESTTOKEN456"
CAPTURED = {}
AUDITED = []

def fake_init(self):
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
    return {"status": "submitted", "broker_order_id": 998, "fill_price": None,
            "sl_price": sl_price, "tp_price": tp_price}

m.MT5DemoBackend.__init__ = fake_init
m.MT5DemoBackend.submit_order = fake_submit_order
safety.run_entry_safety_gates = lambda intent, backend: (True, {"stub": "pass"})
safety.append_audit = lambda op, intent, checks, result: AUDITED.append((intent, result))

# sandbox the gate pass — the FALLBACK channel source (never the live file)
_GP_DIR = tempfile.mkdtemp()
GP = Path(_GP_DIR) / "last_gate_pass.json"
config.GATE_PASS_PATH = GP

def set_gate_channel(chan):
    if chan is None:
        GP.unlink(missing_ok=True)
    else:
        GP.write_text(json.dumps({"verdict": "PASS", "channel": chan}))

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

BASE = dict(symbol="US100.cash", side="long", lots=1.0, tp_price=29700.0,
            decision_ref="TEST-SLIP-AUTO", entry_ref=29528.0, max_slip_pt=15.0,
            sl_price=29495.0, sl_distance_pt=33.0)

results = []
def check(name, ok, detail):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

print("Driving the REAL _handle_submit over loopback — auto-binding drift cap (review-hardened)\n")

# 1 — payload auto + ADVERSE 20 > cap 15 → HARD REFUSE, nothing submitted, audited binding:true
set_gate_channel(None); CAPTURED.clear(); AUDITED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29547.0, ask=29548.0)  # long fills at ask: +20 adverse
r = post_submit(dict(BASE, channel="auto"))
bound = any((i.get("drift_check") or {}).get("binding") is True for i, _ in AUDITED)
check("1 auto adverse-beyond-cap REFUSED (payload channel, no gate pass needed)",
      r.get("status") == "rejected" and str(r.get("reason", "")).startswith("slip_cap_exceeded_auto")
      and not CAPTURED, f"status={r.get('status')} reason={str(r.get('reason'))[:60]}")
check("1 refusal audited binding:true", bound, f"binding-flagged={bound}")

# 2 — payload auto + adverse 8 ≤ cap → fills, risk preserved
CAPTURED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29535.0, ask=29536.0)
r = post_submit(dict(BASE, channel="auto"))
sl = CAPTURED.get("sl_price")
check("2 auto within-cap FILLS + risk preserved",
      r.get("status") == "submitted" and sl is not None and abs(29536.0 - sl) == 33.0,
      f"status={r.get('status')} sl={sl}")

# 3 — payload auto + FAVORABLE 20 (DISCOUNT fill) → FILLS (adverse-only pin; abs() would refuse)
CAPTURED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29507.0, ask=29508.0)  # long at ask 20 BELOW anchor
r = post_submit(dict(BASE, channel="auto"))
sl = CAPTURED.get("sl_price")
check("3 auto DISCOUNT fill beyond |cap| FILLS (adverse-only — the 48.8pt false-block class)",
      r.get("status") == "submitted" and sl is not None and abs(29508.0 - sl) == 33.0,
      f"status={r.get('status')} sl={sl}")

# 4 — THE RACE-B PIN: payload manual + gate pass says AUTO + adverse 20 → FILLS advisory.
#     The trader's gun must NEVER be bound, whatever the racy shared gate-pass file says.
set_gate_channel("auto"); CAPTURED.clear(); AUDITED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29547.0, ask=29548.0)
r = post_submit(dict(BASE, channel="manual"))
flagged = any((i.get("drift_check") or {}).get("slip_advisory_exceeded") is True for i, _ in AUDITED)
check("4 GUN payload=manual FILLS even when the gate pass races to auto (06-30 directive pin)",
      r.get("status") == "submitted" and bool(CAPTURED), f"status={r.get('status')}")
check("4 advisory flag still audited for the gun", flagged, f"slip_advisory_exceeded={flagged}")

# 5 — legacy fallback: payload has NO channel + gate pass auto + adverse 20 → refused
set_gate_channel("auto"); CAPTURED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29547.0, ask=29548.0)
r = post_submit(dict(BASE))
check("5 unstamped payload + gate-pass auto → binding still applies (legacy fallback)",
      r.get("status") == "rejected" and str(r.get("reason", "")).startswith("slip_cap_exceeded_auto"),
      f"status={r.get('status')}")

# 6 — auto MUST carry drift fields: strip entry_ref/max_slip_pt → refused, nothing submitted
set_gate_channel(None); CAPTURED.clear()
stripped = dict(BASE, channel="auto"); stripped.pop("entry_ref"); stripped.pop("max_slip_pt")
r = post_submit(stripped)
check("6 auto without entry_ref/max_slip_pt REFUSED (no strip-the-payload dodge)",
      r.get("status") == "rejected" and str(r.get("reason", "")).startswith("auto_submit_missing_drift_fields")
      and not CAPTURED, f"status={r.get('status')} reason={str(r.get('reason'))[:55]}")

# 7 — manual + no gate pass + adverse 20 → advisory fill (unchanged manual semantics)
set_gate_channel(None); CAPTURED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29547.0, ask=29548.0)
r = post_submit(dict(BASE, channel="manual"))
check("7 manual, no gate pass → advisory fill (manual semantics byte-identical)",
      r.get("status") == "submitted", f"status={r.get('status')}")

# 8 — fill-path drift_note carries the channel (gun-report exclusion substrate)
CAPTURED.clear(); AUDITED.clear()
m.MT5DemoBackend.get_quote = make_fake_quote(bid=29535.0, ask=29536.0)
r = post_submit(dict(BASE, channel="auto"))
ch_stamped = any((i.get("drift_check") or {}).get("channel") == "auto" for i, _ in AUDITED)
check("8 fill drift_note channel-stamped (gun report can exclude auto rows)",
      r.get("status") == "submitted" and ch_stamped, f"channel-stamped={ch_stamped}")

srv.shutdown()
print(f"\nRESULT: {sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
