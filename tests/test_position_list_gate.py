#!/usr/bin/env python3
"""
PROOF: list_positions NEVER fabricates a well-formed "flat" from a dead/unresponsive EA
(2026-07-02 fix — the morning's timeout printed {"positions": []} and reconcile consumed it
as broker-truth-flat; UNKNOWN must never read as FLAT).

Three worlds:
  A. EA heartbeat dead      -> list_positions RAISES (no silent [])
  B. EA alive, op times out -> list_positions RAISES (no silent [])
  C. EA alive, op ok        -> real list returned (flat [] only from an actual ok answer)
Plus the consumer chain: the raised error surfaces as a dict WITHOUT a 'positions' list,
which reconcile_from_broker.py's PF-6 guard REFUSES (exit 3, ledger untouched).
"""
import sys, os, json, subprocess

_EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _EXT)
import oas_execute_mcp.backends.mt5_demo as m

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")

m.MT5DemoBackend.__init__ = lambda self: None   # don't start the real bridge

# A — heartbeat dead
m.MT5DemoBackend._require_live_ea = lambda self: {"status": "rejected", "reason": "ea_not_live: heartbeat_stale: 4559s"}
try:
    m.MT5DemoBackend().list_positions()
    check("A dead-EA raises", False, "returned instead of raising")
except RuntimeError as e:
    check("A dead-EA raises", "UNKNOWN" in str(e) and "ea_not_live" in str(e), str(e)[:80])

# B — alive but the op times out
m.MT5DemoBackend._require_live_ea = lambda self: None
m._send_and_wait = lambda fields, timeout_sec=10.0: {"status": "timeout", "reason": "no EA response within 10.0s"}
try:
    m.MT5DemoBackend().list_positions()
    check("B timeout raises", False, "returned instead of raising")
except RuntimeError as e:
    check("B timeout raises", "UNKNOWN" in str(e) and "timeout" in str(e), str(e)[:80])

# C — genuine ok answer (flat and non-flat)
m._send_and_wait = lambda fields, timeout_sec=10.0: {"status": "ok", "count": 0}
check("C1 genuine flat -> []", m.MT5DemoBackend().list_positions() == [])
m._send_and_wait = lambda fields, timeout_sec=10.0: {
    "status": "ok", "count": 1, "position.0.broker_order_id": "42", "position.0.side": "long",
    "position.0.lots": 2.0, "position.0.entry_price": 29900.0, "position.0.sl_price": 29850.0}
pos = m.MT5DemoBackend().list_positions()
check("C2 genuine position returned", len(pos) == 1 and pos[0]["broker_order_id"] == "42")

# D — consumer chain: an error-shaped payload must be REFUSED downstream, exit 3.
#
# This check exercises the CONSUMER of position_list, not the server. It needs the host
# project's reconcile script (path via OAS_RECONCILE_CMD). Where that is absent it SKIPS
# with a reason rather than passing — a check that silently turns green when its subject
# is missing is worse than no check at all.
reconcile = os.environ.get("OAS_RECONCILE_CMD")
if reconcile and os.path.exists(reconcile):
    err_payload = json.dumps({"status": "error",
                              "reason": "unhandled_exception: RuntimeError: position_list refused: ea_not_live"})
    r = subprocess.run(["python3", reconcile, err_payload], capture_output=True, text=True)
    check("D reconcile REFUSES error payload (exit 3)",
          r.returncode == 3 and "REFUSED" in r.stderr,
          f"rc={r.returncode} stderr={r.stderr.strip()[:90]}")
else:
    print("  [SKIP] D consumer-chain check — set OAS_RECONCILE_CMD to the host project's "
          "reconcile script to run it. Not counted as a pass.")

n = sum(results)
print(f"\n=== {n}/{len(results)} checks passed ===")
sys.exit(0 if n == len(results) else 1)
