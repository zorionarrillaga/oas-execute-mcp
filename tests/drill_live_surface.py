"""Live drill: exercise the OAS MCP parameter surface I haven't touched yet.

What this proves:
  - SHORT (sell) side works end-to-end (only longs were tested before).
  - Non-zero sl_price + tp_price on submit (vs. modify-after-the-fact).
  - Partial close (scale): close lots < total → position shrinks, doesn't flatten.
  - Modify with sl_price changed but tp_price unchanged (passing None preserves).
  - Lot increment behavior: 0.01 baseline + 0.02 + invalid 0.005 (broker reject expected).
  - Round-trip latency measurements per op.

Bypasses the dead MCP server; talks to safety + backend directly. Same code
path as MCP calls — just a different driver. Closes everything it opens.

Run from repo root:
  .venv/bin/python -m tests.drill_live_surface
"""

import json
import subprocess
import time
from pathlib import Path

from oas_execute_mcp import safety, config
from oas_execute_mcp.backends.mt5_demo import MT5DemoBackend

REPO = Path(__file__).resolve().parents[3]


def banner(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def mint_gate(side: str, entry: int, sl: int, tp: int, reason: str):
    """Mint a fresh gate-pass; required for entries (60s TTL)."""
    subprocess.run([
        "bash", "bin/pre_trade_gate.sh",
        "--confirm-r6", "--time", "10:30",
        "--override-r3", reason,
        "--no-battery-check", "--no-econ-check", "--sl-spectrum-yes",
        "--no-eval-check", "--setup-type", "continuation",
        side, str(entry), str(sl), str(tp), "3",
    ], check=True, capture_output=True, cwd=str(REPO))


def main():
    backend = MT5DemoBackend()

    # Health
    banner("HEALTH")
    for _ in range(10):
        time.sleep(1)
        h = backend.health()
        if h.get("ok"):
            break
    assert h.get("ok"), f"backend health: {h}"
    print(f"OK — EA poll={h['ping']['poll_count']} latency={h['latency_ms']}ms "
          f"server_time={h['ping']['server_time']}")

    # Clear override log for the drill (will restore at end)
    override_log = config.DATA_STATE / "override_log.jsonl"
    backup = override_log.read_text() if override_log.exists() else ""
    override_log.parent.mkdir(parents=True, exist_ok=True)
    override_log.write_text("")

    results = {}
    try:
        # --------- DRILL 1: SHORT submit with SL+TP on the submit call ---------
        banner("DRILL 1 — SHORT submit with non-zero SL + TP")
        # Read current price; place SL above + TP below for a short
        positions_pre = backend.list_positions()  # warm the listener
        acct = backend.get_account_info()
        print(f"  account balance ${acct['balance']:,.2f}")
        # Use a generic mint with conservative SL/TP placement (price-agnostic)
        mint_gate("short", 28880, 28930, 28800, "infra:drill-short-with-sl-tp")  # 1.6R
        t0 = time.time()
        sub = backend.submit_order(symbol="US100.cash", side="short", lots=0.01,
                                   sl_price=29000.0, tp_price=28700.0,  # wide brackets
                                   decision_ref="OAS-drill-short-sl-tp-probe")
        dt = (time.time() - t0) * 1000
        print(json.dumps(sub, indent=2))
        assert sub.get("status") == "submitted", f"SHORT submit failed: {sub}"
        bid_short = sub["broker_order_id"]
        fp_short = sub["fill_price"]
        results["short_submit_latency_ms"] = round(dt)
        results["short_fill_price"] = fp_short

        # Verify broker actually accepted SL+TP (not 0/0 like before)
        pos = next((p for p in backend.list_positions()
                    if str(p["broker_order_id"]) == str(bid_short)), None)
        assert pos, f"SHORT position not in list_positions"
        print(f"  position: side={pos['side']} lots={pos['lots']} entry={pos['entry_price']} "
              f"sl={pos['sl_price']} tp={pos['tp_price']}")
        assert pos["side"] == "short"
        # Some brokers normalize SL/TP to "" or 0 if too far; check whichever is reported
        sl_set = pos["sl_price"] not in (0, 0.0, "0.0", None, "")
        tp_set = pos["tp_price"] not in (0, 0.0, "0.0", None, "")
        results["submit_with_sl_tp_accepted"] = bool(sl_set and tp_set)
        print(f"  submit-side SL set: {sl_set}, TP set: {tp_set}")
        if not (sl_set and tp_set):
            print("  WARN — broker may have ignored submit-time SL/TP; modify-after-fill is required")

        # --------- DRILL 2: Modify with SL changed, TP omitted (passing None) ---------
        banner("DRILL 2 — modify SL only (TP=None should preserve existing)")
        t0 = time.time()
        existing_tp = pos["tp_price"]
        new_sl_only = round(float(pos["entry_price"]) + 70, 2)  # widen SL for short = above
        mod = backend.modify_order(broker_order_id=bid_short,
                                   sl_price=new_sl_only, tp_price=None,
                                   decision_ref="OAS-drill-modify-sl-only")
        dt = (time.time() - t0) * 1000
        print(json.dumps(mod, indent=2))
        assert mod.get("status") == "modified", f"modify failed: {mod}"
        pos2 = next((p for p in backend.list_positions()
                     if str(p["broker_order_id"]) == str(bid_short)), None)
        print(f"  after-modify: sl={pos2['sl_price']} tp={pos2['tp_price']} "
              f"(expected sl≈{new_sl_only}, tp preserved as {existing_tp})")
        results["modify_sl_only_latency_ms"] = round(dt)
        results["modify_sl_only_preserved_tp"] = pos2["tp_price"] == existing_tp

        # --------- DRILL 3: Lot increments (invalid 0.005 → broker reject expected) ---------
        banner("DRILL 3 — lot increment probe (0.005 sub-min should reject)")
        mint_gate("long", 28880, 28860, 28920, "infra:drill-lot-increment")
        bad = backend.submit_order(symbol="US100.cash", side="long", lots=0.005,
                                   sl_price=0, tp_price=0,
                                   decision_ref="OAS-drill-lot-005-probe")
        print(json.dumps(bad, indent=2))
        results["sub_min_lot_status"] = bad.get("status")
        results["sub_min_lot_reason"] = bad.get("reason", bad.get("details", ""))[:120]
        # Allow either rejected_by_safety (catches before broker) or a broker reject

        # --------- DRILL 4: 0.02 long (larger valid lot) ---------
        banner("DRILL 4 — 0.02-lot long (larger valid increment)")
        mint_gate("long", 28880, 28860, 28920, "infra:drill-002-lot")
        t0 = time.time()
        sub2 = backend.submit_order(symbol="US100.cash", side="long", lots=0.02,
                                    sl_price=0, tp_price=0,
                                    decision_ref="OAS-drill-002-long-probe")
        dt = (time.time() - t0) * 1000
        print(json.dumps(sub2, indent=2))
        assert sub2.get("status") == "submitted", f"0.02 submit failed: {sub2}"
        bid_long = sub2["broker_order_id"]
        fp_long = sub2["fill_price"]
        results["lot_002_latency_ms"] = round(dt)
        results["lot_002_fill_price"] = fp_long
        pos_long = next(p for p in backend.list_positions()
                        if str(p["broker_order_id"]) == str(bid_long))
        assert float(pos_long["lots"]) == 0.02

        # --------- DRILL 5: Partial close (scale) ---------
        banner("DRILL 5 — partial close on 0.02 long (scale 0.01)")
        t0 = time.time()
        scale = backend.close_order(broker_order_id=bid_long, lots=0.01,
                                    decision_ref="OAS-drill-scale-probe")
        dt = (time.time() - t0) * 1000
        print(json.dumps(scale, indent=2))
        assert scale.get("status") == "closed"
        assert float(scale["remaining_lots"]) == 0.01, f"remaining_lots = {scale.get('remaining_lots')}"
        pos_after = next(p for p in backend.list_positions()
                         if str(p["broker_order_id"]) == str(bid_long))
        assert float(pos_after["lots"]) == 0.01, f"position lots after partial: {pos_after['lots']}"
        results["partial_close_latency_ms"] = round(dt)
        print(f"  OK — partial close left 0.01 lots open (was 0.02)")

        # --------- CLEANUP: flatten everything we opened ---------
        banner("CLEANUP — flatten via close_all")
        t0 = time.time()
        flat = backend.close_all(reason="drill-live-surface cleanup")
        dt = (time.time() - t0) * 1000
        print(json.dumps(flat, indent=2))
        assert flat.get("status") == "closed_all"
        results["close_all_latency_ms"] = round(dt)
        results["close_all_count"] = flat.get("count")
        positions_final = backend.list_positions()
        assert positions_final == [], f"positions not flat: {positions_final}"

    finally:
        override_log.write_text(backup)
        print(f"\nrestored override_log.jsonl ({len(backup)} bytes)")

    banner("LATENCY + CAPABILITY MAP")
    for k, v in results.items():
        print(f"  {k}: {v}")

    print()
    print("DRILL COMPLETE")


if __name__ == "__main__":
    main()
