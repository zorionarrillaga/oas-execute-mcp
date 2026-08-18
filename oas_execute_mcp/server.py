"""MCP server entry — stdio transport, 7 tools.

Wired into Claude Code as mcp__oas_execute__* tools. Every write op goes through the safety
layer in safety.py before reaching the backend. Audit log is written even on rejection.

Run standalone: python -m oas_execute_mcp.server
(An MCP client spawns it over stdio; see README for the client config block.)
"""

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from . import config, safety
from .backends import get_backend


server = Server(config.SERVER_NAME)
_backend = None


def backend():
    """Lazy-init so import-time errors don't crash the MCP handshake."""
    global _backend
    if _backend is None:
        _backend = get_backend()
    return _backend


# ─── Tool schemas ────────────────────────────────────────────────────────────────────

TOOLS = [
    types.Tool(
        name="order_submit",
        description=(
            "Open a new position. Goes through entry safety gates (killswitch + rate_limit + "
            "gate_pass_freshness + position_sanity). Requires a fresh PASS in "
            "a fresh PASS record written by the calling system's pre-trade gate. Path: $OAS_DATA_DIR/state/last_gate_pass.json."
        ),
        inputSchema={
            "type": "object",
            "required": ["symbol", "side", "lots", "sl_price", "tp_price", "decision_ref"],
            "properties": {
                "symbol": {"type": "string", "description": "Broker symbol name. FTMO-Demo uses 'US100.cash' for NASDAQ-100. Do NOT pass bare 'US100' — broker will reject with symbol_not_available."},
                "side": {"type": "string", "enum": ["long", "short"]},
                "lots": {"type": "number"},
                "sl_price": {"type": "number"},
                "tp_price": {"type": "number"},
                "decision_ref": {"type": "string",
                    "description": "OAS decision ID e.g. OAS-2026-05-19-T-SIM-006"},
            },
        },
    ),
    types.Tool(
        name="order_modify",
        description="Update SL and/or TP on an existing position. Null = leave unchanged.",
        inputSchema={
            "type": "object",
            "required": ["broker_order_id", "decision_ref"],
            "properties": {
                "broker_order_id": {"type": "string"},
                "sl_price": {"type": ["number", "null"]},
                "tp_price": {"type": ["number", "null"]},
                "decision_ref": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="order_close",
        description="Close full or partial position. lots=null means full close.",
        inputSchema={
            "type": "object",
            "required": ["broker_order_id", "decision_ref"],
            "properties": {
                "broker_order_id": {"type": "string"},
                "lots": {"type": ["number", "null"]},
                "decision_ref": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="close_all",
        description=(
            "Emergency flatten: close every open position. Bypasses kill-switch check "
            "(closing must always be possible). Audit-logged with reason."
        ),
        inputSchema={
            "type": "object",
            "required": ["reason"],
            "properties": {"reason": {"type": "string"}},
        },
    ),
    types.Tool(
        name="account_info",
        description="Read account balance, equity, daily P&L, open position count.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="position_list",
        description="List all currently-open positions with entry, SL, TP, lots.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="health_check",
        description="Backend health + connectivity + version.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


# ─── Tool dispatch ───────────────────────────────────────────────────────────────────

def _text_result(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def _handle_submit(args: dict) -> dict:
    intent = {k: args.get(k) for k in ("symbol", "side", "lots", "sl_price", "tp_price", "decision_ref")}
    ok, checks = safety.run_entry_safety_gates(intent, backend())
    if not ok:
        result = {"status": "rejected_by_safety", "checks": checks}
        safety.append_audit("submit", intent, checks, result)
        return result
    result = backend().submit_order(**intent)
    safety.append_audit("submit", intent, checks, result)
    return result


# Price params that order_modify actually honours. Anything else that LOOKS like a price is a
# caller mistake, not an extra: the intent comprehension below silently drops unknown keys.
_CANONICAL_MODIFY_PRICES = ("sl_price", "tp_price")
_PRICE_ALIASES = ("sl", "tp", "stop", "stop_loss", "stoploss", "take_profit", "takeprofit",
                  "sl_px", "tp_px", "price", "new_sl", "new_tp")


def _validate_modify_args(args: dict):
    """Refuse a modify that CANNOT change anything. Returns None if actionable, else a reject dict.

    THE DEFECT THIS KILLS (live 2026-07-28 09:44-09:45 ET, position 171039332):
      the caller sent {broker_order_id, sl: 27675.0, decision_ref} — `sl`, not `sl_price`. The intent
      comprehension below keys on a FIXED tuple, so `sl` was DROPPED and sl_price became None. The
      backend forwards "" for a None price; OAS_Bridge.mq5 HandleModify reads "" as KEEP CURRENT, so
      it asked MT5 to set SL=current/TP=current and MT5 correctly answered retcode 10025 "no changes".
      THE STOP NEVER MOVED, and the only signal was an error that reads like "it was already there"
      — misread exactly that way at 13:38Z the same session.

    A dropped key must therefore be LOUD at the door, not silent in a comprehension: this is capital
    protection, so an unactionable modify is refused by name rather than sent to bounce off the EA.
    """
    offenders = [k for k in _PRICE_ALIASES if k in args and args.get(k) is not None]
    if offenders:
        return {
            "status": "rejected_bad_args",
            "reason": (
                f"non-canonical price argument(s) {offenders!r} — order_modify honours only "
                f"{list(_CANONICAL_MODIFY_PRICES)}. These keys are DROPPED silently, the EA reads an "
                f"empty price as 'keep current', and MT5 returns 10025 'no changes' — which reads "
                f"like success. Resend using sl_price=/tp_price=. THE STOP DID NOT MOVE."
            ),
            "offending_keys": offenders,
        }
    if all(args.get(k) is None for k in _CANONICAL_MODIFY_PRICES):
        return {
            "status": "rejected_bad_args",
            "reason": (
                "modify carries neither sl_price nor tp_price — it cannot change anything. Sending "
                "it would return mt5_retcode=10025 'no changes', which reads like the move was "
                "already applied. Pass at least one canonical price."
            ),
            "offending_keys": [],
        }
    return None


def _handle_modify(args: dict) -> dict:
    # Door check FIRST: an unactionable modify never reaches the safety gates or the broker.
    bad = _validate_modify_args(args)
    if bad is not None:
        safety.append_audit("modify", dict(args), {"arg_validation": bad}, bad)
        return bad
    intent = {k: args.get(k) for k in ("broker_order_id", "sl_price", "tp_price", "decision_ref")}
    ok, checks = safety.run_modify_safety_gates(intent, backend())
    if not ok:
        result = {"status": "rejected_by_safety", "checks": checks}
        safety.append_audit("modify", intent, checks, result)
        return result
    result = backend().modify_order(**intent)
    safety.append_audit("modify", intent, checks, result)
    return result


def _handle_close(args: dict) -> dict:
    intent = {k: args.get(k) for k in ("broker_order_id", "lots", "decision_ref")}
    ok, checks = safety.run_close_safety_gates(intent, backend())
    if not ok:
        result = {"status": "rejected_by_safety", "checks": checks}
        safety.append_audit("close", intent, checks, result)
        return result
    result = backend().close_order(**intent)
    safety.append_audit("close", intent, checks, result)
    return result


def _handle_close_all(args: dict) -> dict:
    reason = args.get("reason", "<unspecified>")
    intent = {"reason": reason}
    # close_all bypasses kill-switch (it IS the kill action) but still rate-limited
    ok, reason_str = safety.check_rate_limit()
    checks = {"rate_limit": {"ok": ok, "reason": reason_str}}
    if not ok:
        result = {"status": "rejected_by_safety", "checks": checks}
        safety.append_audit("close_all", intent, checks, result)
        return result
    result = backend().close_all(reason)
    safety.append_audit("close_all", intent, checks, result)
    return result


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}
    try:
        if name == "order_submit":
            return _text_result(_handle_submit(args))
        if name == "order_modify":
            return _text_result(_handle_modify(args))
        if name == "order_close":
            return _text_result(_handle_close(args))
        if name == "close_all":
            return _text_result(_handle_close_all(args))
        if name == "account_info":
            return _text_result(backend().get_account_info())
        if name == "position_list":
            return _text_result({"positions": backend().list_positions()})
        if name == "health_check":
            return _text_result(backend().health())
        return _text_result({"status": "error", "reason": f"unknown_tool: {name}"})
    except Exception as e:
        return _text_result({"status": "error", "reason": f"unhandled_exception: {type(e).__name__}: {e}"})


# ─── Entry ───────────────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
