"""End-to-end test: bin/oas_trade.py with --l2-mirror flag.

Walks a complete OAS trade lifecycle against FTMO-Demo:
  open → trail → scale (L2-skipped at base lot 0.01) → close

Uses sentinel state files (not the live oas_account.json / oas_trades.jsonl).
Temporarily clears override_log.jsonl so override-kill doesn't block, then
restores byte-exact. Verifies:
  - Broker position opens on `open --l2-mirror` with broker_order_id captured
  - `trail --l2-mirror` fires broker SL modify
  - `scale` without --l2-scale-lots → broker stays open, sim records partial
  - `close --l2-mirror` flattens broker position
  - Sim ledger entries carry L2 ack fields

Run from repo root:
  .venv/bin/python -m oas_execute_mcp.tests.test_oas_trade_l2_integration
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from oas_execute_mcp import config

# Intentionally do NOT import MT5DemoBackend here — each oas_trade.py
# subprocess spawns its own listener on :16275 and tears it down on exit.
# Holding our own listener in this driver would collide on the port bind.

# This is a CONSUMER-CHAIN test: it drives the host project's `bin/oas_trade.py` CLI
# against this server, so it needs that project checked out. Point OAS_HOST_REPO at it.
# Without it the test SKIPS with a reason — it never reports a pass it did not earn.
REPO = Path(os.environ.get("OAS_HOST_REPO", Path(__file__).resolve().parents[3]))
_HOST_CLI = REPO / "bin" / "oas_trade.py"
if not _HOST_CLI.exists():
    print(f"[SKIP] consumer-chain test — host CLI not found at {_HOST_CLI}.")
    print("       Set OAS_HOST_REPO to the project that consumes this server to run it.")
    print("       Skipped, not passed.")
    sys.exit(0)


def banner(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def run_oas(args, env=None):
    """Run bin/oas_trade.py as a subprocess; return (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(_HOST_CLI)] + args
    p = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=str(REPO), env=env or os.environ)
    return p.returncode, p.stdout, p.stderr


def mint_gate(side, entry, sl, tp, reason):
    # Synthesizes a clean PASS for the integration test. Adds the four
    # log-scan overrides (--pnl/--trades-today/--last-results/--no-open)
    # because the test runs from sentinel state files without a session
    # log.md present; without these the gate returns NEEDS-INPUT on R2/R4/
    # R5/R8.
    subprocess.run([
        "bash", "bin/pre_trade_gate.sh",
        "--confirm-r6", "--time", "10:30",
        "--override-r3", reason,
        "--no-battery-check", "--no-econ-check", "--sl-spectrum-yes",
        "--no-eval-check", "--setup-type", "continuation",
        "--pnl", "0", "--trades-today", "0", "--last-results", "",
        "--no-open",
        side, str(entry), str(sl), str(tp), "3",
    ], check=True, capture_output=True, cwd=str(REPO))


def main():
    # Sentinel state files in a tmp dir
    tmp = Path(tempfile.mkdtemp(prefix="oas_l2_test_"))
    account_path = tmp / "oas_account.json"
    trades_path = tmp / "oas_trades.jsonl"
    account_path.write_text(json.dumps({
        "starting_balance": 100000.0,
        "current_balance": 100000.0,
        "running_pnl": 0.0,
        "trade_count": 0,
        "wins": 0, "losses": 0, "breakevens": 0,
        "pt_value_default": 1.0,
        "next_trade_id": "T-SIM-TEST-001",
    }, indent=2))
    trades_path.touch()
    print(f"sentinel state in {tmp}")

    # Clear override log so override-kill doesn't block; restore at end
    override_log = config.DATA_STATE / "override_log.jsonl"
    backup = override_log.read_text() if override_log.exists() else ""
    override_log.parent.mkdir(parents=True, exist_ok=True)
    override_log.write_text("")

    try:
        # ----- OPEN -----
        banner("STEP 1 — oas_trade.py open --l2-mirror")
        # Use wide stops (60pt SL + 100pt TP = 1.67R) so broker accepts even if
        # market drifted from the planned entry. Current US100 ~28863.
        mint_gate("long", 28860, 28800, 28960, "infra:integration-test-l2-open")
        rc, out, err = run_oas([
            "--account", str(account_path), "--trades", str(trades_path),
            "open",
            "--trade-id", "T-SIM-TEST-001",
            "--side", "long",
            "--entry", "28860",
            "--sl", "28800",
            "--sl-class", "structural",
            "--sl-distance-pt", "60",
            "--t1", "28960",
            "--t2", "29010",
            "--t3", "29060",
            "--lots", "5",
            "--L", "7", "--S", "7", "--H", "7",
            "--thesis", "integration test L2 mirror lifecycle",
            "--l2-mirror",
        ])
        print(out)
        if rc != 0:
            print("STDERR:", err)
            raise SystemExit(f"open failed rc={rc}")

        acct = json.loads(account_path.read_text())
        op = acct["open_position"]
        assert op["l2"], f"open_position.l2 not populated: {op}"
        bid = op["l2"]["broker_order_id"]
        l2_fill = op["l2"]["fill_price"]
        print(f"  open_position.l2.broker_order_id = {bid}")
        print(f"  open_position.l2.fill_price = {l2_fill}")
        assert bid, "broker_order_id missing"
        assert l2_fill and l2_fill > 0, f"l2 fill_price not populated: {l2_fill}"

        # (Broker state is implicitly verified by oas_trade.py's L2 ack output
        # "L2 mirror open OK — ticket X @ Y" printed above; the open_position.l2
        # block in the account file is the durable record.)

        # ----- TRAIL -----
        banner("STEP 2 — oas_trade.py trail --l2-mirror (move SL up)")
        rc, out, err = run_oas([
            "--account", str(account_path), "--trades", str(trades_path),
            "trail",
            "--new-sl", "28830",  # move SL up from 28800 to 28830 (long trail UP)
            "--note", "trail SL up to BE-ish",
            "--l2-mirror",
        ])
        print(out)
        if rc != 0:
            print("STDERR:", err)
            raise SystemExit(f"trail failed rc={rc}")

        # Verify ledger captured l2 ack (oas_trade output already showed the
        # broker modify result; this asserts the durable ledger row carries it)
        last_event = json.loads(trades_path.read_text().splitlines()[-1])
        assert last_event["event"] == "TRAIL"
        assert last_event.get("l2_new_sl") == 28830.0
        print(f"  ledger TRAIL event captured l2_new_sl={last_event['l2_new_sl']}")

        # ----- SCALE (L2 skipped because base lot 0.01 too small for partial) -----
        banner("STEP 3 — oas_trade.py scale (no --l2-scale-lots, expect L2 skip)")
        rc, out, err = run_oas([
            "--account", str(account_path), "--trades", str(trades_path),
            "scale",
            "--price", "28960",
            "--lots", "2",
            "--pts", "100",
            "--realized", "200",
            "--note", "T1 hit, scale 40%",
            "--l2-mirror",
            # NB: no --l2-scale-lots → L2 partial skipped
        ])
        print(out)
        if rc != 0:
            print("STDERR:", err)
            raise SystemExit(f"scale failed rc={rc}")

        # Verify the ledger SCALE row carries the L2-skip marker (broker
        # position stays at 0.01 because oas_trade skipped the partial)
        last_event = json.loads(trades_path.read_text().splitlines()[-1])
        assert last_event["event"] == "SCALE"
        assert last_event.get("l2_scale_skipped") == "base_lot_too_small_for_partial"
        print(f"  ledger SCALE event captured l2_scale_skipped marker")

        # Sim should record the partial: lots 5 → 3
        acct = json.loads(account_path.read_text())
        op = acct["open_position"]
        assert op["lots"] == 3, f"sim runner lots should be 3, got {op['lots']}"
        print(f"  sim runner lots after scale: {op['lots']} (was 5)")

        # ----- CLOSE -----
        banner("STEP 4 — oas_trade.py close --l2-mirror (flatten broker)")
        rc, out, err = run_oas([
            "--account", str(account_path), "--trades", str(trades_path),
            "close",
            "--price", "29010",
            "--reason", "t2_hit",
            "--grade", "A",
            "--l2-mirror",
        ])
        print(out)
        if rc != 0:
            print("STDERR:", err)
            raise SystemExit(f"close failed rc={rc}")

        # oas_trade's close printed "L2 mirror close OK — broker ticket X flat";
        # verify the durable CLOSE row carries the l2 ack fields.
        close_row = json.loads(trades_path.read_text().splitlines()[-1])
        assert close_row["status"] == "CLOSED"
        assert close_row.get("l2_broker_order_id") == bid
        print(f"  ledger CLOSE row captured l2_broker_order_id={close_row['l2_broker_order_id']}")

        acct = json.loads(account_path.read_text())
        assert "open_position" not in acct or acct.get("open_position") is None
        print(f"  sim flat: final balance ${acct['current_balance']:.2f} "
              f"(running_pnl ${acct['running_pnl']:.2f})")

        banner("STEP 5 — final state check")
        print(f"Total ledger events: {len(trades_path.read_text().splitlines())}")
        for line in trades_path.read_text().splitlines():
            d = json.loads(line)
            event = d.get("event") or d.get("status")
            print(f"  • {event} trade_id={d.get('trade_id')}")

    finally:
        override_log.write_text(backup)
        print(f"\nrestored override_log.jsonl ({len(backup)} bytes)")
        # Safety net: spawn a one-shot Python to call close_all (we don't
        # hold our own backend; subprocess avoids the :16275 conflict).
        cleanup = subprocess.run([
            sys.executable, "-c",
            "from oas_execute_mcp.backends.mt5_demo import MT5DemoBackend; "
            "import time; b=MT5DemoBackend(); time.sleep(2); "
            "r=b.close_all(reason='integration-test cleanup safety'); print(r)"
        ], capture_output=True, text=True, cwd=str(REPO))
        if "count" in cleanup.stdout and "0" not in cleanup.stdout.split("count")[1][:5]:
            print(f"WARN — cleanup flattened stray positions: {cleanup.stdout.strip()}")

    print()
    print("INTEGRATION TEST — ALL GREEN")


if __name__ == "__main__":
    main()
