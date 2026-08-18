"""Abstract Backend interface.

Concrete implementations (stub, ctrader, mt5_vps) must satisfy this contract.
The MCP server + safety layer call only these methods — they never know which backend is live.
This is the boundary that lets the stub→real swap happen without touching tool or safety code.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class Backend(ABC):
    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: str,           # "long" or "short"
        lots: float,
        sl_price: float,
        tp_price: float,
        decision_ref: str,   # e.g. "OAS-2026-05-19-T-SIM-006"
    ) -> dict[str, Any]:
        """Open a new position. Returns dict with at minimum:
            { "status": "submitted" | "rejected",
              "broker_order_id": str,
              "fill_price": float,
              "fill_time_iso": str,
              "latency_ms": int,
              "reason": str (only if rejected) }
        """

    @abstractmethod
    def modify_order(
        self,
        broker_order_id: str,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        decision_ref: str = "",
    ) -> dict[str, Any]:
        """Update SL and/or TP on existing position. None = leave unchanged."""

    @abstractmethod
    def close_order(
        self,
        broker_order_id: str,
        lots: Optional[float] = None,  # None = full close
        decision_ref: str = "",
    ) -> dict[str, Any]:
        """Close full or partial position. Returns realized pnl + close price."""

    @abstractmethod
    def list_positions(self) -> list[dict[str, Any]]:
        """All currently-open positions. Empty list if flat."""

    @abstractmethod
    def get_account_info(self) -> dict[str, Any]:
        """Balance, equity, margin, daily P&L."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Backend connectivity + state. Returns {ok: bool, detail: str, latency_ms: int}."""

    def close_all(self, reason: str) -> dict[str, Any]:
        """Default implementation iterates list_positions + close_order each.
        Real backends should override with a single batched call when supported."""
        results = []
        for pos in self.list_positions():
            results.append(self.close_order(pos["broker_order_id"], decision_ref=f"close_all:{reason}"))
        return {"status": "closed_all", "reason": reason, "count": len(results), "details": results}
