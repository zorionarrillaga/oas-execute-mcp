"""Stub backend — simulates a broker on-disk.

Phase A.0 only. Real backends (cTrader, MT5-VPS) replace this without changing tool or
safety code. Useful for validating the OAS decision→execution contract end-to-end before
broker creds + connectivity work.

State files:
- oas_stub_positions.json: list of currently-open simulated positions
- oas_stub_account.json: balance, equity, realized P&L
- oas_pending_orders.jsonl: append-only ledger of every submit/modify/close intent + result
"""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .. import config
from .base import Backend


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


class StubBackend(Backend):
    def __init__(self):
        self._positions_path = config.STUB_POSITIONS_PATH
        self._account_path = config.STUB_ACCOUNT_PATH
        self._ledger_path = config.STUB_PENDING_ORDERS_PATH
        self._slippage = config.STUB_SLIPPAGE_PT
        self._pt_value = config.STUB_PT_VALUE_USD_PER_LOT
        self._ensure_account_initialized()

    def _ensure_account_initialized(self):
        if not self._account_path.exists():
            _save_json(self._account_path, {
                "balance": config.STUB_STARTING_BALANCE,
                "equity": config.STUB_STARTING_BALANCE,
                "realized_pnl_session": 0.0,
                "initialized_iso": _now_iso(),
            })

    def _read_positions(self) -> list[dict]:
        return _load_json(self._positions_path, [])

    def _write_positions(self, positions: list[dict]):
        _save_json(self._positions_path, positions)

    def _read_account(self) -> dict:
        return _load_json(self._account_path, {"balance": config.STUB_STARTING_BALANCE,
                                                "equity": config.STUB_STARTING_BALANCE,
                                                "realized_pnl_session": 0.0})

    def _write_account(self, acct: dict):
        _save_json(self._account_path, acct)

    def submit_order(self, symbol, side, lots, sl_price, tp_price, decision_ref):
        t0 = time.perf_counter()
        broker_id = f"STUB-{uuid.uuid4().hex[:8].upper()}"
        # Stub fill price: requested entry from decision_ref isn't passed; we sim a fill at SL midpoint
        # using sl/tp to approximate. Real backend gets real fill from broker.
        fill_price = (sl_price + tp_price) / 2.0
        # Apply slippage (worse direction for trader)
        if side == "long":
            fill_price += self._slippage
        else:
            fill_price -= self._slippage

        position = {
            "broker_order_id": broker_id,
            "symbol": symbol,
            "side": side,
            "lots": lots,
            "entry_price": fill_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "decision_ref": decision_ref,
            "opened_iso": _now_iso(),
        }
        positions = self._read_positions()
        positions.append(position)
        self._write_positions(positions)

        result = {
            "status": "submitted",
            "broker_order_id": broker_id,
            "fill_price": fill_price,
            "fill_time_iso": position["opened_iso"],
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
        _append_jsonl(self._ledger_path, {
            "op": "submit", "intent": {"symbol": symbol, "side": side, "lots": lots,
                                        "sl_price": sl_price, "tp_price": tp_price,
                                        "decision_ref": decision_ref},
            "result": result, "ts_iso": _now_iso(),
        })
        return result

    def modify_order(self, broker_order_id, sl_price=None, tp_price=None, decision_ref=""):
        t0 = time.perf_counter()
        positions = self._read_positions()
        target = next((p for p in positions if p["broker_order_id"] == broker_order_id), None)
        if not target:
            result = {"status": "rejected", "reason": "broker_order_id_not_found",
                      "broker_order_id": broker_order_id,
                      "latency_ms": int((time.perf_counter() - t0) * 1000)}
        else:
            if sl_price is not None:
                target["sl_price"] = sl_price
            if tp_price is not None:
                target["tp_price"] = tp_price
            target["last_modified_iso"] = _now_iso()
            self._write_positions(positions)
            result = {"status": "modified", "broker_order_id": broker_order_id,
                      "sl_price": target["sl_price"], "tp_price": target["tp_price"],
                      "latency_ms": int((time.perf_counter() - t0) * 1000)}
        _append_jsonl(self._ledger_path, {
            "op": "modify",
            "intent": {"broker_order_id": broker_order_id, "sl_price": sl_price,
                       "tp_price": tp_price, "decision_ref": decision_ref},
            "result": result, "ts_iso": _now_iso(),
        })
        return result

    def close_order(self, broker_order_id, lots=None, decision_ref=""):
        t0 = time.perf_counter()
        positions = self._read_positions()
        target_idx = next((i for i, p in enumerate(positions) if p["broker_order_id"] == broker_order_id), None)
        if target_idx is None:
            result = {"status": "rejected", "reason": "broker_order_id_not_found",
                      "broker_order_id": broker_order_id,
                      "latency_ms": int((time.perf_counter() - t0) * 1000)}
        else:
            target = positions[target_idx]
            close_lots = lots if lots is not None else target["lots"]
            # Sim close at midpoint of (entry, tp) with adverse slippage. Real backend gets real price.
            close_price = (target["entry_price"] + target["tp_price"]) / 2.0
            if target["side"] == "long":
                close_price -= self._slippage
            else:
                close_price += self._slippage
            pnl_pt = (close_price - target["entry_price"]) if target["side"] == "long" \
                     else (target["entry_price"] - close_price)
            pnl_usd = pnl_pt * close_lots * self._pt_value

            if close_lots >= target["lots"]:
                positions.pop(target_idx)
            else:
                target["lots"] -= close_lots

            self._write_positions(positions)
            acct = self._read_account()
            acct["realized_pnl_session"] += pnl_usd
            acct["balance"] += pnl_usd
            acct["equity"] = acct["balance"]
            self._write_account(acct)

            result = {"status": "closed", "broker_order_id": broker_order_id,
                      "close_price": close_price, "close_lots": close_lots,
                      "pnl_usd": pnl_usd, "pnl_pt": pnl_pt,
                      "remaining_lots": target["lots"] if close_lots < target["lots"] else 0,
                      "latency_ms": int((time.perf_counter() - t0) * 1000)}
        _append_jsonl(self._ledger_path, {
            "op": "close",
            "intent": {"broker_order_id": broker_order_id, "lots": lots, "decision_ref": decision_ref},
            "result": result, "ts_iso": _now_iso(),
        })
        return result

    def list_positions(self):
        return self._read_positions()

    def get_account_info(self):
        acct = self._read_account()
        positions = self._read_positions()
        return {**acct, "open_positions_count": len(positions),
                "backend": "stub", "ts_iso": _now_iso()}

    def health(self):
        t0 = time.perf_counter()
        # Stub is always healthy if files writable
        try:
            self._ensure_account_initialized()
            ok, detail = True, "stub backend on-disk; no broker connectivity"
        except Exception as e:
            ok, detail = False, f"stub backend write failed: {e}"
        return {"ok": ok, "detail": detail,
                "backend": "stub", "version": config.SERVER_VERSION,
                "latency_ms": int((time.perf_counter() - t0) * 1000)}
