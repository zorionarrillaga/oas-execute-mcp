"""OAS-execute MCP server config.

Single source of truth for paths + backend selection. All other modules import from here.
"""

import os
from pathlib import Path

# Data directory. Runtime state, audit log and the account-identity record live here.
# Override with OAS_DATA_DIR; defaults to ./data alongside this package so the server
# is self-contained and needs no host repository.
DATA_DIR = Path(os.environ.get("OAS_DATA_DIR", Path(__file__).resolve().parent / "data"))
DATA_STATE = DATA_DIR / "state"

# The server owns its state directory — create it on import rather than failing on first
# write. Idempotent; safe when several processes start at once.
DATA_STATE.mkdir(parents=True, exist_ok=True)

# Backend selection. Env var OAS_EXECUTE_BACKEND overrides; default "stub".
# Valid values: stub | mt5_demo | ctrader | mt5_vps
#   stub      → on-disk simulation, no broker (Phase A.0)
#   mt5_demo  → file-bridge to MT5 Mac (Wine) via Common/Files + OAS_Bridge EA (Phase A.1)
#   ctrader   → pending (cTrader OpenAPI; only if FTMO 1-step retake opens on cTrader)
#   mt5_vps   → pending (Windows VPS fallback if Mac-native proves unreliable)
BACKEND_NAME = os.environ.get("OAS_EXECUTE_BACKEND", "stub")

# Safety + audit paths
KILL_SWITCH_PATH = DATA_STATE / "oas_kill_switch"
AUDIT_LOG_PATH = DATA_STATE / "oas_execute_audit.jsonl"
GATE_PASS_PATH = DATA_STATE / "last_gate_pass.json"
# Per-process shared secret for the localhost /submit route (S1a, refutation MINOR #7).
# Minted by the bridge listener on start; read by whatever process fires an order. chmod 0600.
SUBMIT_TOKEN_PATH = DATA_STATE / "submit_token.txt"

# Account-identity record. NEVER holds a password — the bridge follows whatever the MT5
# terminal is already logged into, so this file is documentary (login + server name only).
# Keep it outside version control and chmod 600.
DATA_SECRETS = DATA_DIR / "secrets"
FTMO_CREDS_PATH = DATA_SECRETS / "ftmo_account.json"
FTMO_DEMO_CREDS_PATH = FTMO_CREDS_PATH  # legacy alias

# Canonical symbol names per broker. OAS sim has been using "US100"; FTMO-Demo broker
# names it "US100.cash" (NASDAQ 100 Index Spot CFD). Always pass this exact name on
# order_submit for the mt5_demo backend.
FTMO_DEMO_PRIMARY_SYMBOL = "US100.cash"

# Stub-backend paths
STUB_POSITIONS_PATH = DATA_STATE / "oas_stub_positions.json"
STUB_PENDING_ORDERS_PATH = DATA_STATE / "oas_pending_orders.jsonl"
STUB_ACCOUNT_PATH = DATA_STATE / "oas_stub_account.json"

# FTMO ToS bound — daily request budget
RATE_LIMIT_DAILY = 2000

# Gate-pass freshness window (seconds). pre_trade_gate.sh writes timestamps;
# connector refuses orders if last PASS is older than this.
GATE_PASS_TTL_SEC = 60

# Stub-backend sim parameters
STUB_SLIPPAGE_PT = 0.5  # half-point slippage on entry/exit
STUB_STARTING_BALANCE = 100_000.0
STUB_PT_VALUE_USD_PER_LOT = 1.0  # OAS imaginary; real CFD likely $0.20-0.40

# Server identity
SERVER_NAME = "oas-execute"
SERVER_VERSION = "0.1.0-phaseA0"
