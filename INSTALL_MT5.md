# MT5 demo backend — install guide (socket transport, v2)

This is the EA bridge that lets the
`oas-execute` MCP server send orders to MT5 (Mac, Wine-wrapped) and receive
results. **Transport is TCP sockets** as of 2026-05-19 (replaces the original
file-IPC approach, which was empirically blocked on Wine).

**Wire diagram:**
```
Python MCP                                MT5 (Wine on Mac)
──────────                                ─────────────────
mt5_demo.py listens 127.0.0.1:16275  ◄──  EA SocketConnect every 100ms
queue.pop → write framed cmd          ──► OAS_Bridge.mq5 OnTimer
                                          → CTrade.PositionOpen / Modify / Close
                                          → broker (FTMO-Demo)
parse + resolve Future                ◄── EA sends framed result on same socket
heartbeat probe (read-only)           ◄── EA writes Common/Files/oas_bridge/heartbeat.txt
```

MQL5 sockets are client-only, so Python is the listener and the EA polls
outbound. Framing: key=value lines terminated by a literal `<<END>>` line.

Files involved:
- `oas_execute_mcp/mql5/OAS_Bridge.mq5` v2.00 — MQL5 source
- `oas_execute_mcp/backends/mt5_demo.py` — Mac-side backend
- a JSON credentials file pointed at by `OAS_MT5_CREDS` — MT5 login (never committed)

## One-time setup

### 1. Install + compile the EA

Copy `oas_execute_mcp/mql5/OAS_Bridge.mq5` into the terminal's `MQL5/Experts`
directory inside the Wine prefix (both paths are exported below as
`$D0E_EXPERTS` and `$PORTABLE_EXPERTS`). To compile headlessly:

```bash
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
export WINEPREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"
D0E_EXPERTS="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/D0E8209F77C8CF37AD8BF550E51FF075/MQL5/Experts"
PORTABLE_EXPERTS="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Experts"
"$WINE" "C:\\Program Files\\MetaTrader 5\\MetaEditor64.exe" \
  /compile:"C:\\users\\user\\AppData\\Roaming\\MetaQuotes\\Terminal\\D0E8209F77C8CF37AD8BF550E51FF075\\MQL5\\Experts\\OAS_Bridge.mq5" /log
iconv -f UTF-16LE -t UTF-8 "$D0E_EXPERTS/OAS_Bridge.log" | grep Result
cp "$D0E_EXPERTS/OAS_Bridge.ex5" "$PORTABLE_EXPERTS/OAS_Bridge.ex5"
```

Expect: `Result: 0 errors, 0 warnings`.

### 2. Enable algo trading + 127.0.0.1 socket allowlist

In MT5 main window:

1. **Tools → Options → Expert Advisors**:
   - ✓ Allow algorithmic trading
   - ✓ Allow WebRequest for listed URL → add `http://127.0.0.1:16275`
     (MQL5 socket permissions may require the listener URL here on some builds.)
2. Click OK.
3. Toolbar **Algo Trading** button must be **GREEN**.

### 3. Attach EA to a chart

1. Open any chart (the EA is symbol-agnostic — the chart symbol becomes the
   default if a command doesn't specify one).
2. **Navigator (Ctrl+N) → Expert Advisors → OAS_Bridge** — drag onto the chart.
3. EA properties dialog:
   - **Common**: ✓ Allow Algo Trading
   - **Inputs**: defaults are fine. The relevant ones:
     - `ListenerHost = 127.0.0.1`
     - `ListenerPort = 16275`
     - `PollIntervalMs = 100`
4. Click OK.
5. Smiley face in top-right of chart = EA running.

### 4. Verify the bridge is live

The Python listener has to be running BEFORE the EA can connect, but
the EA will retry every 100ms so the order doesn't strictly matter — once both
sides are up, they find each other.

Start the Python listener via the smoke-test script (Step 5).

You should also see the heartbeat file ticking:

```bash
ls "$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/oas_bridge/"
cat "$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/oas_bridge/heartbeat.txt"
```

Expect `state=alive` with a fresh `ts_iso`.

### 5. Round-trip smoke test

```bash
PYTHONPATH=. python3 -c "
import os, sys, json
os.environ['OAS_EXECUTE_BACKEND'] = 'mt5_demo'
for m in list(sys.modules):
    if 'oas_execute_mcp' in m: del sys.modules[m]
from oas_execute_mcp.backends import get_backend
b = get_backend()
print('HEALTH:', json.dumps(b.health(), indent=2, default=str))
print('ACCOUNT:', json.dumps(b.get_account_info(), indent=2, default=str))
print('POSITIONS:', json.dumps(b.list_positions(), indent=2, default=str))
"
```

Expected: `health.ok=true`, `account_info` returns `broker=FTMO-Demo`,
`account_login=1513387057`, `currency=USD`.

### 6. Flip MCP backend stub → mt5_demo

```bash
claude mcp remove oas-execute
claude mcp add oas-execute -s local -e OAS_EXECUTE_BACKEND=mt5_demo \
  -- /path/to/oas_execute_mcp_launcher.sh
```

**Restart Claude Code** so the new env loads. Verify with `claude mcp list`.

## Operating notes

- **The EA must be attached + running.** If MT5 is closed or the EA is detached,
  the heartbeat goes stale within 5s and the backend refuses writes
  (returns `ea_not_live: heartbeat_stale: …`).
- **The Python listener owns port 16275.** If you start two MCP processes,
  the second one's `bind()` will fail. One MCP instance is enough.
- **One EA, one chart.** Multiple OAS_Bridge instances all polling the same
  socket would race for queued commands. Attach to exactly one chart.
- **Localhost only.** The listener binds 127.0.0.1, so no external network
  exposure. macOS firewall does not need to allow incoming connections from
  outside the loopback interface.
- **Symbol naming on FTMO-Demo.** First submit will fail with
  `symbol_not_available` if the name is wrong. Confirm via MT5 Market Watch
  (commonly `US100.cash` but could be `USTEC` etc.).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `heartbeat_missing` | EA not attached / not compiled | Re-attach to chart; check MT5 Experts tab for compile errors |
| `heartbeat_stale: N.Ns old` | EA running but MT5 frozen, OR `Algo Trading` button red | Click Algo Trading to green; reset chart if frozen |
| Backend times out on ping | Python listener not bound, or EA can't connect | `lsof -iTCP:16275 -sTCP:LISTEN` to confirm listener up; check EA log for "SocketConnect failed" |
| `SocketConnect failed (listener down?)` in EA log | Python listener not running, port blocked | Start Python side; check WebRequest URL whitelist has `http://127.0.0.1:16275` |
| `symbol_not_available:US100.cash` | FTMO uses a different ticker | Market Watch → Symbols → find correct name |
| `mt5_retcode=10018 Market is closed` | Outside market hours | Wait for RTH |
| `mt5_retcode=10027 Autotrading disabled by client` | Algo Trading button red | Click to green |

## Why this design (socket, not file)

File IPC (Common/Files inbox/outbox) is the docs-recommended pattern but it
**does not work** on Wine Mac MT5 — the EA cannot enumerate or open files
created by external (non-Wine) processes. Empirically proven 2026-05-19 with
2900+ failed `FileFindFirst`/`FileOpen` calls. The Wine→Mac direction (EA
writes, Mac reads) is fine, which is why heartbeat still uses a file.

TCP localhost sockets are the industry-standard fix (DWX-ZeroMQ-Connector and
most retail MT5↔Python bridges). Localhost RTT is ~1ms. MQL5 has had
`SocketCreate/Connect/Send/Read` since build 1845.

## Architecture invariants

- Wire format: key=value, one per line, UTF-8.
- Framing: messages terminate with a literal `<<END>>` line + trailing newline.
  Symmetric — both EA and Python use it.
- Concurrency: single accept loop on Python side; one in-flight command at a
  time per connection. The EA opens, asks, optionally executes + responds,
  closes. Connections are short-lived (~100ms idle, longer on real ops).
- Correlation IDs: Python generates a UUID per call, stamps it into the
  command. The EA echoes it back in the response. Mismatch is treated as a
  protocol bug (no auto-discard logic — fail loudly instead).
- Dead-man's switch: if the future on the Python side times out, the
  `_PendingCmd` is marked `expired` and the listener skips it on the next
  EA poll. No replay of stale commands.
