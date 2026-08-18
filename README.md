# oas-execute-mcp

An MCP server that puts a **safety layer between a language model and a broker order**.

A model calls `order_submit`. Before anything reaches the broker, the request passes eight
independent checks, any one of which can refuse it. Every attempt — allowed or refused — is
appended to an audit log with the reason. The broker itself sits behind a swappable backend, so the
same tool surface runs against an on-disk simulator or a live MetaTrader 5 terminal without the
model knowing which.

Built for an agent that ran unattended against a $100k prop-firm evaluation account, where a
mis-sized order was not a failed test.

---

## Why it exists

The interesting part of giving an agent a tool that spends money is not the happy path. It is:

- what the tool returns when it **half-succeeds** — the order filled but the stop did not attach;
- where a **timeout** lands — the broker never answered, and you do not know whether it filled;
- how a failure is **stopped from being reported as a success** by the layer above it.

So the design rule here is that the model's own account of what happened is never trusted. Position
state is re-read from the broker on every call. The refusals are explicit, few, and sit directly on
the execution path rather than beside it in a prompt.

## The safety layer

Eight checks, each able to refuse independently, all logged with a machine-readable reason:

| Check | Refuses when |
|---|---|
| `check_killswitch` | A kill-switch file exists on disk. Trips instantly, no restart needed. |
| `check_rate_limit` | Orders are arriving faster than the configured ceiling. |
| `check_gate_pass_freshness` | The upstream decision record is stale — no order may ride an old approval. |
| `check_position_sanity` | Size, side or symbol disagree with what the broker reports. |
| `check_sl_widen_block` | A stop-loss modification would move the stop *away* from price. |
| `check_session_override_kill` | Manual overrides this session exceed the allowed count. |
| `check_live_equity_kill` | Account equity has crossed the configured floor. |
| Audit append | Always. A refusal that leaves no record is treated as a failure. |

`safety.py` groups these into `run_entry_safety_gates`, `run_modify_safety_gates` and
`run_close_safety_gates`, so a close is never blocked by a rule that should only govern an open —
the failure mode where a safety system traps you in a position it was meant to protect.

## Tool surface

Seven tools: `order_submit` · `order_modify` · `order_close` · `close_all` · `position_list` ·
`account_info` · `health_check`.

## Architecture

```
    model tool call
          │
          ▼
   server.py  ── MCP stdio server, tool schemas + dispatch
          │
          ▼
   safety.py  ── 8 gates, audit log, kill switch     ◄── refuses here, before the broker
          │
          ▼
   backends/  ── swappable
      ├── stub.py      on-disk simulation, no broker, deterministic
      └── mt5_demo.py  file bridge → MetaTrader 5 (Wine/macOS) via an MQL5 EA
                       localhost HTTP listener, shared-secret token on the firing route
```

The MQL5 expert advisor (`oas_execute_mcp/mql5/OAS_Bridge.mq5`) polls a localhost endpoint and
executes on the terminal side. `backends/base.py` is the interface; adding a broker means
implementing it, and nothing above the backend changes.

## Install

```bash
pip install -e ".[dev]"
```

Point your MCP client at it:

```json
{
  "mcpServers": {
    "oas-execute": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "oas_execute_mcp.server"],
      "cwd": "/path/to/parent-of-this-package"
    }
  }
}
```

Backend selection and paths are environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `OAS_EXECUTE_BACKEND` | `stub` | `stub` or `mt5_demo` |
| `OAS_DATA_DIR` | `./oas_execute_mcp/data` | State, audit log, account record |
| `OAS_MT5_COMMON_FILES` | MT5-under-Wine default | MT5 `Common/Files` for the bridge |

It ships defaulting to `stub`. You have to choose to point it at a broker.

**No credentials live in this repository, and none should.** The MT5 backend follows whatever
terminal is already logged in; the account record it reads is documentary (login and server name),
carries no password, and belongs outside version control.

## Tests

```bash
PYTHONPATH=. python3 tests/test_smoke.py     # full submit → modify → close cycle + failure modes
for t in tests/test_*.py; do PYTHONPATH=. python3 "$t"; done   # the whole suite
python3 -m pytest tests/ -q                  # same suite, if you would rather type this
```

Nine test files, all passing on a clean checkout. Each one is a self-contained
checker: it runs its assertions at module scope and exits non-zero on failure, so
the suite needs nothing but an interpreter. `tests/suite_test.py` drives the same
scripts as subprocesses so `pytest` is a working entry point too — it is a bridge,
not a second suite, and there is no version of the tests that only one runner sees. They cover the submit/modify/close cycle against
the stub, stop-loss anchoring and re-derivation after a charged fill, the bridge client-mode
handshake, quote and position routes, and the gate that stops an error-shaped payload from being
consumed as an empty position list — a real incident, where a timeout returned `{"positions": []}`
and a downstream reader believed it.

**Two tests skip rather than pass when their subject is absent.** They exercise a *consumer* of this
server, so they need the host project (`OAS_HOST_REPO`, `OAS_RECONCILE_CMD`). A check that silently
turns green when the thing it checks is missing is worse than no check.

## Status and limits

- The `stub` backend is complete and deterministic. `mt5_demo` is in production use against a
  MetaTrader 5 terminal running under Wine on macOS.
- **`check_rate_limit` is implemented but is not exercised by the test bench** — the bench simulates
  broker timeouts and kill-switch fire, not rate-limit responses. Stated because the distinction
  between "implemented" and "tested" is the whole subject of this repository.
- The MT5 bridge is macOS/Wine-shaped. A native Windows install needs `OAS_MT5_COMMON_FILES` set and
  has not been tested by me.
- Single-terminal by design: the listener is a singleton on one port, and a second server process
  detects the first and forwards to it rather than competing for the bind.

## License

MIT — see [LICENSE](LICENSE).
