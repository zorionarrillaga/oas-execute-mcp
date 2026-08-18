#!/usr/bin/env python3
"""
TASK-008 — PROVE the C2 SL-distance re-anchor (safety.py check_gate_pass_freshness).

Drives the REAL function (NOT stubbed — the gap that let the original C2 bug ship). Writes a
fixture gate-pass to a temp path (config.GATE_PASS_PATH monkeypatched) and asserts:
  1. Charged fire that DRIFTED past sl_pts, SL re-derived off the fill → NOW PASSES (true risk
     |fill−SL| == sl_pts). This is the bug fix: before, it false-blocked (gate_pass_sl_distance).
  2. The perverse case proven live (shoot 2): long, price DROPPED into a better discount → PASSES.
  3. GENUINELY widened SL on a charged fire (SL much farther than 2× gated from the actual spot)
     → STILL BLOCKS. The silent-risk protection is preserved.
  4. Raw/uncharged fire (no drift_check) keeps the gate["entry"] anchor: normal SL passes, a
     genuinely-wide SL still blocks (behavior unchanged for the agent MCP path).
"""
import sys, os, json, datetime, tempfile
_EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _EXT)
from oas_execute_mcp import safety, config
from pathlib import Path

# --- fixture gate-pass: entry 29819, gated SL distance 30pt, fresh PASS ---
def write_gate(entry=29819.0, sl_pts=30.0):
    fresh_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    gate = {"verdict": "PASS", "ts_iso": fresh_ts, "symbol": "US100.cash",
            "side": "long", "entry": entry, "sl_pts": sl_pts, "size_lots": 0.01}
    p = Path(tempfile.gettempdir()) / "test_c2_gate_pass.json"
    p.write_text(json.dumps(gate))
    return p

_orig_path = config.GATE_PASS_PATH
config.GATE_PASS_PATH = write_gate()

results = []
def check(name, ok, detail):
    results.append(ok); print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

print("Driving the REAL safety.check_gate_pass_freshness (gate: entry 29819, gated SL 30pt)\n")

# 1+2. CHARGED + DRIFTED long: price dropped 48.8pt to 29770.18 (better discount), SL re-derived
#      to fill−30 = 29740.2. True risk |29770.18 − 29740.2| = 30pt == gated. MUST PASS now.
intent_drift = {"symbol": "US100.cash", "side": "long", "lots": 0.01, "sl_price": 29740.2,
                "drift_check": {"broker_spot": 29770.18, "sl_distance_pt": 30.0}}
ok, reason = safety.check_gate_pass_freshness(intent_drift)
check("1/2 drifted-discount fill (was the live false-block) now PASSES", ok,
      f"ok={ok} reason={reason}")

# 3. GENUINELY widened SL on a charged fire: spot 29770 but SL slammed to 29650 (120pt from spot,
#    > 2×30=60). Real risk IS 120pt — silent-risk → MUST STILL BLOCK.
intent_wide = {"symbol": "US100.cash", "side": "long", "lots": 0.01, "sl_price": 29650.0,
               "drift_check": {"broker_spot": 29770.18, "sl_distance_pt": 30.0}}
ok, reason = safety.check_gate_pass_freshness(intent_wide)
check("3 genuinely-wide SL on charged fire STILL BLOCKS", (not ok) and "sl_distance_exceeded" in reason,
      f"ok={ok} reason={reason[:70]}")

# 4a. RAW fire (no drift_check), normal SL 30pt from gate entry → PASSES (gate-entry anchor).
intent_raw_ok = {"symbol": "US100.cash", "side": "long", "lots": 0.01, "sl_price": 29789.0}
ok, reason = safety.check_gate_pass_freshness(intent_raw_ok)
check("4a raw fire normal SL PASSES (gate-entry anchor unchanged)", ok, f"ok={ok} reason={reason}")

# 4b. RAW fire, SL genuinely wide (29650 = 169pt from gate entry 29819, > 60) → STILL BLOCKS.
intent_raw_wide = {"symbol": "US100.cash", "side": "long", "lots": 0.01, "sl_price": 29650.0}
ok, reason = safety.check_gate_pass_freshness(intent_raw_wide)
check("4b raw fire wide SL STILL BLOCKS (gate-entry anchor)", (not ok) and "sl_distance_exceeded" in reason,
      f"ok={ok} reason={reason[:70]}")

config.GATE_PASS_PATH = _orig_path
try: (Path(tempfile.gettempdir()) / "test_c2_gate_pass.json").unlink()
except FileNotFoundError: pass

n = sum(results)
print(f"\n=== {n}/{len(results)} checks passed ===")
sys.exit(0 if n == len(results) else 1)
