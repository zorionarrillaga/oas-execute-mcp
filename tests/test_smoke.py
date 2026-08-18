"""End-to-end smoke test against stub backend.

Exercises the full submit → modify → close cycle + safety failure modes. Does NOT touch
the real audit/state files — uses tmp paths via monkey-patched config.

Run: PYTHONPATH=. python3 -m pytest tests/ -v
Or:  PYTHONPATH=. python3 tests/test_smoke.py
"""

import importlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def _setup_tmp_state():
    """Redirect all state paths to a fresh tmp dir + reload modules."""
    tmp = Path(tempfile.mkdtemp(prefix="oas_execute_smoke_"))
    # Set BEFORE importing the package, so config picks them up
    os.environ["OAS_EXECUTE_BACKEND"] = "stub"
    # Drop modules so config + downstream pick up env + path overrides cleanly
    for mod_name in list(sys.modules):
        if mod_name.startswith("oas_execute_mcp"):
            del sys.modules[mod_name]
    return tmp


def _write_fresh_gate_pass(gate_pass_path: Path, symbol="US100.cash", side="long", verdict="PASS"):
    gate_pass_path.parent.mkdir(parents=True, exist_ok=True)
    gate_pass_path.write_text(json.dumps({
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "symbol": symbol,
        "side": side,
    }))


def main():
    tmp = _setup_tmp_state()
    print(f"[setup] tmp state dir: {tmp}")

    # Import and patch config to tmp
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Importing as a package with hyphens isn't directly possible; use importlib
    import importlib.util
    pkg_dir = Path(__file__).resolve().parents[1] / "oas_execute_mcp"
    spec_cfg = importlib.util.spec_from_file_location("oas_exec_config", pkg_dir / "config.py")
    cfg = importlib.util.module_from_spec(spec_cfg)
    sys.modules["oas_exec_config"] = cfg
    spec_cfg.loader.exec_module(cfg)

    # Redirect paths to tmp
    cfg.DATA_STATE = tmp
    cfg.KILL_SWITCH_PATH = tmp / "oas_kill_switch"
    cfg.AUDIT_LOG_PATH = tmp / "oas_execute_audit.jsonl"
    cfg.GATE_PASS_PATH = tmp / "last_gate_pass.json"
    cfg.STUB_POSITIONS_PATH = tmp / "oas_stub_positions.json"
    cfg.STUB_PENDING_ORDERS_PATH = tmp / "oas_pending_orders.jsonl"
    cfg.STUB_ACCOUNT_PATH = tmp / "oas_stub_account.json"

    # Load backend module pointing at the patched config
    spec_base = importlib.util.spec_from_file_location("oas_exec_base", pkg_dir / "backends" / "base.py")
    base_mod = importlib.util.module_from_spec(spec_base)
    sys.modules["oas_exec_base"] = base_mod
    spec_base.loader.exec_module(base_mod)

    # Patch stub.py to import from our patched config
    stub_src = (pkg_dir / "backends" / "stub.py").read_text()
    stub_src = stub_src.replace("from .. import config", "import oas_exec_config as config")
    stub_src = stub_src.replace("from .base import Backend", "from oas_exec_base import Backend")
    spec_stub = importlib.util.spec_from_loader("oas_exec_stub", loader=None)
    stub_mod = importlib.util.module_from_spec(spec_stub)
    exec(compile(stub_src, str(pkg_dir / "backends" / "stub.py"), "exec"), stub_mod.__dict__)
    sys.modules["oas_exec_stub"] = stub_mod

    # Patch safety.py similarly
    safety_src = (pkg_dir / "safety.py").read_text()
    safety_src = safety_src.replace("from . import config", "import oas_exec_config as config")
    spec_safety = importlib.util.spec_from_loader("oas_exec_safety", loader=None)
    safety_mod = importlib.util.module_from_spec(spec_safety)
    exec(compile(safety_src, str(pkg_dir / "safety.py"), "exec"), safety_mod.__dict__)
    sys.modules["oas_exec_safety"] = safety_mod

    backend = stub_mod.StubBackend()
    safety = safety_mod

    failures = []
    def check(name, cond, detail=""):
        status = "OK " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f"  -- {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # ── Test 1: health check ───────────────────────────────────────────────
    print("\nT1 — health check")
    h = backend.health()
    check("health_ok", h["ok"] is True)
    check("health_backend_stub", h["backend"] == "stub")

    # ── Test 2: submit without gate-pass → must reject ──────────────────────
    print("\nT2 — submit without gate-pass: must reject")
    intent = {"symbol": "US100.cash", "side": "long", "lots": 5, "sl_price": 28810,
              "tp_price": 28950, "decision_ref": "OAS-TEST-001"}
    ok, checks = safety.run_entry_safety_gates(intent, backend)
    check("rejected_no_gate", not ok)
    check("rejected_reason_gate", checks.get("gate_pass", {}).get("ok") is False)

    # ── Test 3: fresh gate-pass → submit succeeds ───────────────────────────
    print("\nT3 — submit with fresh gate-pass")
    _write_fresh_gate_pass(cfg.GATE_PASS_PATH, symbol="US100.cash", side="long")
    ok, checks = safety.run_entry_safety_gates(intent, backend)
    check("entry_gates_pass", ok, detail=str(checks))
    if ok:
        result = backend.submit_order(**intent)
        check("submit_status", result["status"] == "submitted", detail=str(result))
        check("submit_has_id", "broker_order_id" in result)
        broker_id = result["broker_order_id"]

        # Test 3b — position visible
        positions = backend.list_positions()
        check("position_visible", any(p["broker_order_id"] == broker_id for p in positions))

        # ── Test 4: modify SL ──────────────────────────────────────────────
        print("\nT4 — modify SL")
        mod_intent = {"broker_order_id": broker_id, "sl_price": 28820, "tp_price": None,
                      "decision_ref": "OAS-TEST-001-trail"}
        ok, checks = safety.run_modify_safety_gates(mod_intent, backend)
        check("modify_gates_pass", ok)
        if ok:
            mr = backend.modify_order(**mod_intent)
            check("modify_status", mr["status"] == "modified")
            check("modify_sl_applied", mr["sl_price"] == 28820)

        # ── Test 5: close ──────────────────────────────────────────────────
        print("\nT5 — close position")
        close_intent = {"broker_order_id": broker_id, "lots": None,
                        "decision_ref": "OAS-TEST-001-close"}
        ok, checks = safety.run_close_safety_gates(close_intent, backend)
        check("close_gates_pass", ok)
        if ok:
            cr = backend.close_order(**close_intent)
            check("close_status", cr["status"] == "closed")
            check("close_position_gone", not any(p["broker_order_id"] == broker_id
                                                 for p in backend.list_positions()))

    # ── Test 6: kill switch blocks new entries ─────────────────────────────
    print("\nT6 — kill switch blocks entries")
    cfg.KILL_SWITCH_PATH.write_text("smoke test trigger")
    _write_fresh_gate_pass(cfg.GATE_PASS_PATH)
    intent2 = {"symbol": "US100.cash", "side": "long", "lots": 3, "sl_price": 28800,
               "tp_price": 28900, "decision_ref": "OAS-TEST-002"}
    ok, checks = safety.run_entry_safety_gates(intent2, backend)
    check("killswitch_blocks", not ok)
    check("killswitch_first_check", checks.get("killswitch", {}).get("ok") is False)
    cfg.KILL_SWITCH_PATH.unlink()

    # ── Test 7: stale gate-pass blocks ─────────────────────────────────────
    print("\nT7 — stale gate-pass blocks")
    stale = {"ts_iso": "2020-01-01T00:00:00+00:00", "verdict": "PASS",
             "symbol": "US100.cash", "side": "long"}
    cfg.GATE_PASS_PATH.write_text(json.dumps(stale))
    ok, checks = safety.run_entry_safety_gates(intent2, backend)
    check("stale_gate_blocks", not ok)
    check("stale_reason_includes_stale",
          "stale" in checks.get("gate_pass", {}).get("reason", "").lower())

    # ── Test 8: position contradiction blocks ──────────────────────────────
    print("\nT8 — opposite-side contradiction blocks")
    _write_fresh_gate_pass(cfg.GATE_PASS_PATH, side="long")
    long_intent = {"symbol": "US100.cash", "side": "long", "lots": 2, "sl_price": 28800,
                   "tp_price": 28900, "decision_ref": "OAS-TEST-003-long"}
    ok, _ = safety.run_entry_safety_gates(long_intent, backend)
    if ok:
        backend.submit_order(**long_intent)
    _write_fresh_gate_pass(cfg.GATE_PASS_PATH, side="short")
    short_intent = {"symbol": "US100.cash", "side": "short", "lots": 2, "sl_price": 28900,
                    "tp_price": 28800, "decision_ref": "OAS-TEST-003-short"}
    ok, checks = safety.run_entry_safety_gates(short_intent, backend)
    check("contradiction_blocks", not ok)
    check("contradiction_named", "position_contradiction" in
                                 checks.get("position_sanity", {}).get("reason", ""))

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures:
        print(f"FAILURES ({len(failures)}): {failures}")
        sys.exit(1)
    print("ALL GREEN — Phase A.0 stub + safety layer wired end-to-end.")
    sys.exit(0)


if __name__ == "__main__":
    main()
