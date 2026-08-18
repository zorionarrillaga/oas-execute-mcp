"""MT5 demo backend — HTTP bridge to OAS_Bridge.mq5 EA (v3, WebRequest transport).

Architecture, post-2026-05-19 pivots:
  - File-IPC failed (Wine inode-cache)
  - Raw socket IPC failed too (MQL5 SocketConnect returns ERR_FUNCTION_NOT_ALLOWED
    on Wine even with the URL in the WebRequest allowlist)
  - HTTP via MQL5 WebRequest() is the canonical Wine-compatible pattern. URL is
    already in the allowlist (entered manually via Options→Expert Advisors).

Flow:

    Python (this file)                    MT5 EA (Wine)
    ──────────────────                    ──────────────
    HTTP server 127.0.0.1:16275      ◄──  WebRequest POST /poll  (every 100ms)
    pop queued cmd (or "op=empty")    ──► response body
                                                              EA executes
                                                              via CTrade
    HTTP POST /result with result    ◄──  WebRequest POST /result
    resolve Future by correlation_id

Wire format: key=value lines, UTF-8, no envelope. Correlation IDs match
requests to results.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as _FutTimeoutError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .. import config
from .base import Backend

LISTENER_HOST = "127.0.0.1"
# Env seam (FTMO_BRIDGE_PORT, shared with the calling system's health checks) so the client-mode
# regression test can drive a real owner+client pair on a scratch port. UNSET = 16275, i.e. every
# production path is byte-identical; the EA's WebRequest allowlist entry is hard-coded to 16275 and
# is NOT affected. Tests must never bind the live port — a running session owns it.
LISTENER_PORT = int(os.environ.get("OAS_BRIDGE_PORT", "16275"))

DEFAULT_TIMEOUT_SEC = 10.0
HEARTBEAT_TTL_SEC = 5.0
EA_RESULT_TIMEOUT_SEC = 12.0

# MT5's Common/Files directory — the file-bridge transport the EA polls. This is the
# default location for MetaTrader 5 running under Wine on macOS; override with
# OAS_MT5_COMMON_FILES for a native Windows install or a non-standard prefix.
MT5_COMMON_FILES = Path(os.environ.get(
    "OAS_MT5_COMMON_FILES",
    str(Path.home() / "Library" / "Application Support" /
        "net.metaquotes.wine.metatrader5" / "drive_c" / "users" / "user" /
        "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / "Common" / "Files"),
))
BRIDGE_DIR = MT5_COMMON_FILES / "oas_bridge"
HEARTBEAT_PATH = BRIDGE_DIR / "heartbeat.txt"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_command(fields: dict[str, Any]) -> str:
    lines = []
    for k, v in fields.items():
        if v is None:
            continue
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def _parse_kv(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
            continue
        try:
            if "." in val:
                out[key] = float(val)
            else:
                out[key] = int(val)
        except ValueError:
            out[key] = val
    return out


class _PendingCmd:
    __slots__ = ("correlation_id", "body", "future", "expired")

    def __init__(self, correlation_id: str, body: str) -> None:
        self.correlation_id = correlation_id
        self.body = body
        self.future: Future = Future()
        self.expired = threading.Event()


class _Bridge:
    """Singleton holding the queue of pending commands + correlation-id table.

    The HTTP server (separate thread) reads/writes this state."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[_PendingCmd]" = queue.Queue()
        self._pending: dict[str, _PendingCmd] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._started = False
        # MUST-FIX #2 (FATAL, refutation §7.7) — server-side single-consume of an ENTRY by
        # decision_ref. The two fire paths (agent MCP submit_order AND the trader's /submit POST)
        # run in different call sites but BOTH funnel through this one process's submit(), so this
        # is the shared chokepoint where double-fire is killed: a decision_ref is claimed exactly
        # once. The EA itself has no dedup (HandleSubmit fires unconditionally), so the claim must
        # live here. Cleared/leaked claims are harmless (worst case: a legit re-fire of the SAME
        # decision_ref is refused — re-charge with a new ref, never a hot-moment shortcut).
        self._consumed_refs: set[str] = set()
        # MINOR #7 (refutation §7.7) — localhost-bind is the ONLY transport auth, so a DNS-rebind
        # local POST could fire. The /submit route additionally requires a per-process shared-secret
        # token (minted on start, written 0600 where charge.sh/go.sh read it). /poll + /result (the
        # EA transport) are unchanged — only the new capital-firing route is token-gated.
        self._submit_token: Optional[str] = None
        # ── CLIENT MODE (2026-07-28) — the fix for the Errno 48 collision ────────────────────
        # The listener is a SINGLETON by nature: the EA polls exactly one 127.0.0.1:16275, and
        # the calling system's fire path and health checks already treat it as a shared service.
        # But it was hosted INSIDE a per-session MCP process, so a second Claude session's server
        # raised OSError(EADDRINUSE) on start() and EVERY mcp__oas-execute__* call failed — while
        # oas_smoke reported 0 FAIL, because the bridge itself was healthy. The standing rule
        # "ONE Claude session owns the bridge — quit the other window" was a NOUN working around
        # an architectural defect.
        # Now: losing the bind is not fatal. We verify a HEALTHY OAS owner holds the port, then run
        # as its CLIENT, forwarding commands over the same localhost+token transport go.sh uses.
        # Asymmetric by design (first process owns, rest proxy) rather than a standalone daemon:
        # strictly additive, cannot break the working single-session path, and needs no new process
        # lifecycle in front of the fire path. The daemon is the cleaner end-state; it is NOT worth
        # putting a new failure mode between the trader and the broker to get there.
        self._client_mode = False
        self._owner_token: Optional[str] = None
        self._attach_hint: Optional[str] = None  # set when we can name WHY an attach was refused

    # ── client-mode plumbing ────────────────────────────────────────────────────────────────
    def _owner_is_healthy(self) -> bool:
        """Is the process holding :16275 actually an OAS bridge, and alive?

        Probes /cmd with NO token and expects the bridge's own 403 `bad_or_missing_submit_token`.
        That single response proves three things at once: something is listening, it speaks the OAS
        route table, and it is enforcing auth. A foreign service holding the port answers 404 /
        garbage / nothing, so we fail closed and let the original bind error surface — attaching to
        an unknown process would point capital commands somewhere unknown.

        ⚠ DELIBERATELY NOT /poll, the obvious liveness route. /poll is CONSUMING: it POPS the next
        queued command. A health probe on it can STEAL a command destined for the EA — a silent
        dropped order or modify. This probe is non-consuming, needs no valid token, and does not
        require the EA to be alive (so a bridge whose MT5 is briefly down is still attachable
        rather than being wrongly rejected back into the Errno 48 failure).
        """
        import urllib.error
        import urllib.request
        try:
            req = urllib.request.Request(
                f"http://{LISTENER_HOST}:{LISTENER_PORT}/cmd", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2.0)
            return False  # a 200 here means it is NOT enforcing auth — do not trust it
        except urllib.error.HTTPError as e:
            try:
                body = e.read(512).decode(errors="replace")
            except Exception:
                return False
            if e.code == 403 and "submit_token" in body:
                return True
            # 404 = something IS speaking HTTP here but has no /cmd route. If it also answers
            # /submit it is one of OUR bridges running pre-2026-07-28 code, which cannot host a
            # client. Name that precisely — the bare "Errno 48" it would otherwise raise is the
            # exact misdiagnosis this whole fix exists to end.
            if e.code == 404:
                self._attach_hint = (
                    "an OAS bridge IS running on :16275 but predates the client-mode fix "
                    "(no /cmd route), so this session cannot attach to it. Restart the OWNING "
                    "session's MCP server to pick up the new code; until then the old "
                    "one-session-owns-the-bridge rule still applies."
                ) if self._probe_is_oas_bridge() else None
            return False
        except Exception:
            return False

    def _probe_is_oas_bridge(self) -> bool:
        """Does the port holder answer /submit like our bridge (403, token-gated)? Used only to
        sharpen the failure message — never to authorise an attach."""
        import urllib.error
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://{LISTENER_HOST}:{LISTENER_PORT}/submit", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"}), timeout=2.0)
            return False
        except urllib.error.HTTPError as e:
            return e.code == 403
        except Exception:
            return False

    def _enter_client_mode(self) -> bool:
        """Attach to the running owner. Returns False if we must not (caller re-raises the bind
        error). NEVER mints or writes submit_token.txt — that file belongs to the process that owns
        the port, and overwriting it is exactly the 2026-07-02 token-clobber that orphaned
        go.sh/charge.sh mid-session and forced the trader onto the terminal by hand."""
        if not self._owner_is_healthy():
            return False
        try:
            self._owner_token = config.SUBMIT_TOKEN_PATH.read_text().strip()
        except Exception:
            return False
        if not self._owner_token:
            return False
        self._client_mode = True
        self._started = True
        return True

    def _submit_via_owner(self, fields: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """Forward one command to the owner process and return the EA's result.

        ROUTING IS DELIBERATELY SPLIT BY RISK:
          · op=submit (OPENS CAPITAL) → POST /submit, the gated route, so the OWNER runs the full
            entry safety layer (run_entry_safety_gates) exactly as it does for any other caller.
            It is never forwarded raw. This is the same cross-process entry path the trader's
            keystroke has used in production since 2026-07-01 — not a new one.
          · everything else (modify · close · close_all · list_positions · account_info · quote ·
            ping) → POST /cmd, which refuses op=submit server-side as a second, independent guard.
        Single-consume also CENTRALISES here: the owner's _consumed_refs becomes the one authority
        for every session, closing a real (if unexercised) cross-window double-fire hole in which
        two processes each held their own claim set.
        """
        import json as _json
        import urllib.request

        op = str(fields.get("op", ""))
        if op == "submit":
            path, payload = "/submit", {
                "symbol": fields.get("symbol"), "side": fields.get("side"),
                "lots": fields.get("lots"), "sl_price": fields.get("sl_price"),
                "tp_price": fields.get("tp_price"),
                "decision_ref": fields.get("decision_ref", ""),
            }
            # ★ FORWARD THE DRIFT FIELDS (fix 2026-08-04, trader-authorized).
            # This whitelist SILENTLY DROPPED entry_ref / max_slip_pt, so the owner's own guard —
            # "an AUTO submit MUST carry the drift fields, server-side so a direct POST cannot
            # bypass it" — could never see them on the client-forwarded path. Any caller routed
            # through client mode was therefore rejected `auto_submit_missing_drift_fields` no
            # matter what it sent. That is what made bin/exec_permission_probe.py structurally
            # unpassable and left LD-1's pre-bell execution proof dead since 2026-07-30, failing
            # IDENTICALLY to real execution-dead.
            # STRICTLY ADDITIVE: only forwarded when the caller actually supplied them, so a caller
            # that sends neither produces a BYTE-IDENTICAL body and the guard's fail-closed
            # behaviour on the auto channel is unchanged. This lets a caller COMPLY with the cap;
            # it does not weaken or bypass it.
            for _k in ("entry_ref", "max_slip_pt", "channel"):
                if fields.get(_k) is not None:
                    payload[_k] = fields.get(_k)
        else:
            path, payload = "/cmd", {"fields": fields, "timeout_sec": timeout_sec}

        req = urllib.request.Request(
            f"http://{LISTENER_HOST}:{LISTENER_PORT}{path}",
            data=_json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Submit-Token": self._owner_token or ""})
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec + 5.0) as r:
                out = _json.loads(r.read().decode() or "{}")
        except Exception as e:
            # Fail LOUD and name the mode. A silent degrade here would be the worst outcome:
            # the caller must never read a transport failure as "no position" / "flat".
            return {"status": "error",
                    "reason": f"client_mode_forward_failed ({type(e).__name__}: {e}) — this MCP "
                              f"server does not own :16275 and could not reach the owner. The "
                              f"owner process may have exited; restart this session.",
                    "_bridge_mode": "client"}
        if isinstance(out, dict):
            out["_bridge_mode"] = "client"
        return out

    def _seed_consumed_refs(self) -> None:
        """A5 (review 2026-06-26) — single-consume must survive a process restart (the MCP server
        MUST restart to load new code, which would otherwise wipe the in-RAM claim set and re-open
        double-fire for an already-FILLED ref). Seed from today's audit log: any submit row whose
        result FILLED (status=submitted) is re-claimed so it cannot fire twice across a restart.
        REJECTED rows are NOT seeded (a clean reject is retryable, per A3). Best-effort + today-only
        (FTMO daily counters reset at ET rollover; an old fill is a different session)."""
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            today = (_dt.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")  # ET approx (matches audit grouping)
            path = config.AUDIT_LOG_PATH
            if not path.exists():
                return
            for line in path.read_text().splitlines():
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                if rec.get("op") != "submit" or rec.get("et_date") != today:
                    continue
                if str((rec.get("result") or {}).get("status")) != "submitted":
                    continue
                ref = str((rec.get("intent") or {}).get("decision_ref", "")).strip()
                if ref:
                    self._consumed_refs.add(ref)
        except Exception:
            pass  # seeding is best-effort; a fresh process with an empty set is the prior (weaker) behavior

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._seed_consumed_refs()
            handler = _make_handler(self)
            # Bind the port BEFORE minting/writing the submit token. A second server
            # instance (a stray double-launch) raises OSError here and dies WITHOUT ever
            # overwriting submit_token.txt — so the on-disk token always belongs to the
            # process that actually owns :16275. Fixes the 2026-07-02 token-clobber: a
            # losing second-start had rewritten the file, orphaning go.sh/charge.sh from
            # the live listener → every /submit & /flatten POST 403'd mid-session, forcing
            # the trader onto the MT5 terminal by hand.
            try:
                self._server = ThreadingHTTPServer((LISTENER_HOST, LISTENER_PORT), handler)
            except OSError as e:
                # EADDRINUSE (errno 48 on macOS / 98 on Linux) is the EXPECTED state for every
                # session after the first — not an error. Attach as a client instead of dying.
                # Any OTHER OSError is a genuine fault and still propagates.
                if e.errno not in (48, 98) or not self._enter_client_mode():
                    if self._attach_hint:
                        raise OSError(e.errno, f"{e.strerror} — {self._attach_hint}") from e
                    raise
                return
            self._mint_submit_token()
            t = threading.Thread(target=self._server.serve_forever,
                                 daemon=True, name="OASHttpBridge")
            t.start()
            self._started = True

    def _mint_submit_token(self) -> None:
        """Mint a per-process shared secret for the /submit route and persist it 0600 so go.sh
        (a separate process) can read it. Best-effort: a write failure still sets the in-memory
        token (the route stays gated; go.sh just can't authenticate until the file exists)."""
        import secrets as _secrets
        self._submit_token = _secrets.token_hex(24)
        try:
            config.SUBMIT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.SUBMIT_TOKEN_PATH.write_text(self._submit_token)
            os.chmod(config.SUBMIT_TOKEN_PATH, 0o600)
        except Exception:
            pass

    def submit(self, body_fields: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        if not self._started:
            self.start()
        # CLIENT MODE — we do not own the queue the EA polls, so forward to the process that does.
        # Deliberately BEFORE the local single-consume claim: in client mode the OWNER's claim set
        # is the single authority for every session (see _submit_via_owner), and claiming locally
        # too would let one window's bookkeeping refuse a ref the owner never actually consumed.
        if self._client_mode:
            return self._submit_via_owner(body_fields, timeout_sec)
        # MUST-FIX #2 — atomic claim-by-decision_ref for ENTRIES only (op==submit). Non-entry ops
        # (poll/result/list_positions/account_info/ping/modify/close) are never deduped here.
        op = str(body_fields.get("op", ""))
        dref = body_fields.get("decision_ref")
        dref = str(dref).strip() if dref is not None else ""
        if op == "submit" and dref:
            with self._lock:
                if dref in self._consumed_refs:
                    return {"status": "rejected",
                            "reason": (f"double_fire_blocked: decision_ref {dref!r} already consumed "
                                       f"this process — single-consume (MUST-FIX #2). Re-charge with a "
                                       f"fresh decision_ref if a new entry is intended."),
                            "decision_ref": dref}
                self._consumed_refs.add(dref)
        correlation_id = body_fields.get("correlation_id") or uuid.uuid4().hex
        body_fields["correlation_id"] = correlation_id
        body_str = _format_command(body_fields)
        pending = _PendingCmd(correlation_id, body_str)
        with self._lock:
            self._pending[correlation_id] = pending
        self._queue.put(pending)
        try:
            result = pending.future.result(timeout=timeout_sec)
        except _FutTimeoutError:
            pending.expired.set()
            with self._lock:
                self._pending.pop(correlation_id, None)
            # TIMEOUT = INDETERMINATE: the EA MAY have filled after the future expired. KEEP the
            # claim (refutation #8) — never release it on timeout, or a retry could double-fire.
            return {"status": "timeout",
                    "reason": f"no EA response within {timeout_sec}s",
                    "correlation_id": correlation_id}
        except Exception as e:
            with self._lock:
                self._pending.pop(correlation_id, None)
            # bridge-internal failure with no definitive EA answer → also indeterminate → KEEP claim.
            return {"status": "error",
                    "reason": f"bridge.submit failed: {e}",
                    "correlation_id": correlation_id}
        # RESOLVED — the EA answered definitively. A3 (review 2026-06-26): if the entry did NOT fill
        # (status != "submitted" — e.g. AlgoTrading-off retcode 10027, the documented recurring case),
        # RELEASE the claim so a legit retry can reuse the same decision_ref. Only a real FILL or an
        # indeterminate timeout keeps the ref consumed. This stops a clean reject from dead-ending the
        # charged round (the prior build burned the ref on any enqueue).
        if op == "submit" and dref and isinstance(result, dict) and result.get("status") != "submitted":
            with self._lock:
                self._consumed_refs.discard(dref)
        return result

    # Called by HTTP handler thread
    def pop_next(self) -> Optional[_PendingCmd]:
        while True:
            try:
                cand = self._queue.get_nowait()
            except queue.Empty:
                return None
            if cand.expired.is_set():
                continue
            return cand

    def resolve(self, correlation_id: str, parsed: dict[str, Any]) -> bool:
        with self._lock:
            pending = self._pending.pop(correlation_id, None)
        if pending is None:
            return False
        if pending.future.done():
            return False
        pending.future.set_result(parsed)
        return True


def _make_handler(bridge: _Bridge):
    class _Handler(BaseHTTPRequestHandler):
        # MT5/Wine's WebRequest client is happiest with HTTP/1.0 + explicit close.
        # Empirically: without `Connection: close` the EA's /result POST sometimes
        # comes back with WebRequest err=5203 (REQUEST_FAILED) on Wine.
        protocol_version = "HTTP/1.0"

        # Silence the default access log — EA polls 10x/sec, would flood stderr
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _read_body(self) -> str:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return ""
            return self.rfile.read(length).decode("utf-8", errors="replace")

        def _respond(self, code: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_POST(self) -> None:
            if self.path.rstrip("/") == "/poll":
                # EA asks for next pending command
                pending = bridge.pop_next()
                if pending is None:
                    self._respond(200, "op=empty\n")
                else:
                    self._respond(200, pending.body)
                return
            if self.path.rstrip("/") == "/result":
                body = self._read_body()
                parsed = _parse_kv(body)
                corr = parsed.get("correlation_id")
                if not corr or not isinstance(corr, str):
                    # Coerce numeric corr to str (parser may have intified)
                    if isinstance(corr, int):
                        corr = str(corr)
                    else:
                        self._respond(400, "missing correlation_id\n")
                        return
                if bridge.resolve(corr, parsed):
                    self._respond(200, "ok=true\n")
                else:
                    self._respond(404, "no pending cmd for corr\n")
                return
            if self.path.rstrip("/") == "/submit":
                self._handle_submit()
                return
            if self.path.rstrip("/") == "/close":
                self._handle_close()
                return
            if self.path.rstrip("/") == "/quote":
                self._handle_quote()
                return
            if self.path.rstrip("/") == "/account":
                self._handle_account()
                return
            if self.path.rstrip("/") == "/positions":
                self._handle_positions()
                return
            if self.path.rstrip("/") == "/cmd":
                self._handle_cmd()
                return
            self._respond(404, "unknown path\n")

        # ── /cmd — cross-process command forwarding for CLIENT-MODE MCP servers (2026-07-28) ────
        def _handle_cmd(self) -> None:
            """POST /cmd — enqueue one already-gated command on behalf of a second Claude session's
            MCP server, which lost the :16275 bind and runs as our client.

            ⛔ REFUSES op=submit. Opening capital keeps its own route (/submit), where the OWNER
            runs run_entry_safety_gates. That is not belt-and-braces: the client forwards a command
            whose gates ran in the CLIENT process, so if /cmd accepted op=submit it would be a
            gate-bypass reachable by any caller holding the token. The client-side router already
            sends op=submit to /submit; this check is the INDEPENDENT server-side half, so a buggy
            or future client cannot open capital through here even by accident.

            Same transport auth as /submit|/close|/quote (localhost-bind + no-Origin DNS-rebind
            guard + shared-secret token) — no new trust level is introduced; anything able to read
            the 0600 token can already POST /submit."""
            import json as _json

            def _reply(code: int, obj: dict) -> None:
                self._respond(code, _json.dumps(obj) + "\n")

            client = (self.client_address[0] if self.client_address else "")
            if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                _reply(403, {"status": "rejected", "reason": f"non_localhost_client: {client!r}"})
                return
            if self.headers.get("Origin") or self.headers.get("Referer"):
                _reply(403, {"status": "rejected", "reason": "browser_origin_refused (DNS-rebind guard)"})
                return
            if not bridge._submit_token or self.headers.get("X-Submit-Token", "") != bridge._submit_token:
                _reply(403, {"status": "rejected", "reason": "bad_or_missing_submit_token"})
                return
            try:
                payload = _json.loads(self._read_body() or "{}")
                fields = payload.get("fields") or {}
                timeout_sec = float(payload.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
            except Exception as e:
                _reply(400, {"status": "rejected", "reason": f"bad_json: {e}"})
                return
            if not isinstance(fields, dict) or not fields.get("op"):
                _reply(400, {"status": "rejected", "reason": "missing fields.op"})
                return
            if str(fields.get("op")) == "submit":
                _reply(403, {"status": "rejected",
                             "reason": "op_submit_refused_on_cmd: opening capital must POST /submit "
                                       "so the owner runs the entry safety gates"})
                return
            _reply(200, bridge.submit(fields, timeout_sec=timeout_sec))

        # ── F3 manual FLATTEN route (TASK-016, 2026-07-01) — the symmetric partner to /submit ──────
        def _handle_close(self) -> None:
            """POST /close — the trader's `!bin/close` FLATTEN. Closes ALL open positions (op=close_all),
            the symmetric partner to /submit's open. Closing is the SAFETY action: run_close_safety_gates
            is rate-limit ONLY (the kill-switch must NEVER block a flatten). Same transport auth as /submit
            (localhost-bind + no-Origin DNS-rebind guard + shared-secret token). Always replies JSON."""
            import json as _json

            def _reply(code: int, obj: dict) -> None:
                self._respond(code, _json.dumps(obj) + "\n")

            client = (self.client_address[0] if self.client_address else "")
            if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                _reply(403, {"status": "rejected", "reason": f"non_localhost_client: {client!r}"})
                return
            if self.headers.get("Origin") or self.headers.get("Referer"):
                _reply(403, {"status": "rejected", "reason": "browser_origin_refused (DNS-rebind guard)"})
                return
            tok = self.headers.get("X-Submit-Token", "")
            expected = bridge._submit_token
            if not expected or tok != expected:
                _reply(403, {"status": "rejected", "reason": "bad_or_missing_submit_token"})
                return
            try:
                payload = _json.loads(self._read_body() or "{}")
            except Exception as e:
                _reply(400, {"status": "rejected", "reason": f"bad_json: {e}"})
                return

            reason = str(payload.get("reason", "manual_flatten"))
            decision_ref = str(payload.get("decision_ref", "")).strip()
            from .. import safety
            backend = MT5DemoBackend()  # reuses the running singleton _bridge (same process, no rebind)
            intent = {"op": "close_all", "reason": reason, "decision_ref": decision_ref}
            ok, checks = safety.run_close_safety_gates(intent, backend)
            if not ok:
                safety.append_audit("close_all", intent, checks, {"status": "rejected_by_safety"})
                _reply(409, {"status": "rejected_by_safety", "checks": checks})
                return
            resp = backend.close_all(reason)
            safety.append_audit("close_all", intent, checks, resp)
            _reply(200, resp if isinstance(resp, dict) else {"status": "?", "raw": str(resp)})

        # ── F3 no-price fire support (spec: project_simpler_manual_shoot_no_number, 2026-07-02) ────
        def _handle_quote(self) -> None:
            """POST /quote — READ-ONLY broker bid/ask for the no-price manual fire (bin/long|short →
            bin/shoot with no price). Same transport auth as /submit|/close (localhost + no-Origin +
            shared-secret token), so probing this route also certs the keystroke AUTH layer without
            firing anything — a pre-fire health check. No safety gates (it's a quote, not capital);
            the EA-alive gate inside get_quote() fail-closes (status=rejected/ea_not_live) when MT5 is
            down — which is exactly the client's abort signal (never a blind fire)."""
            import json as _json

            def _reply(code: int, obj: dict) -> None:
                self._respond(code, _json.dumps(obj) + "\n")

            client = (self.client_address[0] if self.client_address else "")
            if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                _reply(403, {"status": "rejected", "reason": f"non_localhost_client: {client!r}"})
                return
            if self.headers.get("Origin") or self.headers.get("Referer"):
                _reply(403, {"status": "rejected", "reason": "browser_origin_refused (DNS-rebind guard)"})
                return
            tok = self.headers.get("X-Submit-Token", "")
            expected = bridge._submit_token
            if not expected or tok != expected:
                _reply(403, {"status": "rejected", "reason": "bad_or_missing_submit_token"})
                return
            try:
                payload = _json.loads(self._read_body() or "{}")
            except Exception as e:
                _reply(400, {"status": "rejected", "reason": f"bad_json: {e}"})
                return
            symbol = payload.get("symbol") or config.FTMO_DEMO_PRIMARY_SYMBOL
            backend = MT5DemoBackend()  # reuses the running singleton _bridge (same process, no rebind)
            q = backend.get_quote(symbol)
            _reply(200, q if isinstance(q, dict) else {"status": "error", "reason": str(q)})

        # ── D037 S3 — read-only account route for the lockdown fail-safe watcher ────────────
        def _handle_account(self) -> None:
            """POST /account — READ-ONLY balance/equity/open-count for the lockdown watcher
            (bin/lockdown/watcher.sh, TASK-026 Stage 3). Exists so the fail-safe daily-floor
            read does NOT depend on a Claude MCP turn (the 07-06 blind spot). Same transport
            auth as /quote|/close (localhost + no-Origin + shared-secret token). No safety
            gates (read-only). EA down → the account_info round-trip times out → the caller
            counts a BLIND poll and trips fail-safe. Never returns fabricated numbers."""
            import json as _json

            def _reply(code: int, obj: dict) -> None:
                self._respond(code, _json.dumps(obj) + "\n")

            client = (self.client_address[0] if self.client_address else "")
            if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                _reply(403, {"status": "rejected", "reason": f"non_localhost_client: {client!r}"})
                return
            if self.headers.get("Origin") or self.headers.get("Referer"):
                _reply(403, {"status": "rejected", "reason": "browser_origin_refused (DNS-rebind guard)"})
                return
            tok = self.headers.get("X-Submit-Token", "")
            expected = bridge._submit_token
            if not expected or tok != expected:
                _reply(403, {"status": "rejected", "reason": "bad_or_missing_submit_token"})
                return
            backend = MT5DemoBackend()  # reuses the running singleton _bridge (same process, no rebind)
            try:
                info = backend.get_account_info()
            except Exception as e:  # never let the watcher's reader crash the listener
                _reply(200, {"status": "error", "reason": f"account_info_failed: {e}"})
                return
            _reply(200, info if isinstance(info, dict) else {"status": "error", "reason": str(info)})

        # ── D037 foreign-fire guard — read-only positions route for the lockdown watcher ────
        def _handle_positions(self) -> None:
            """POST /positions — READ-ONLY open-position list (incl. per-position magic) for the
            lockdown watcher's FOREIGN-FIRE check (2026-07-07: a trade fired from the MT5 mobile
            app — magic 0, no gate — ended the account; the watcher only read equity and never saw
            the position). Same transport auth as /account (localhost + no-Origin + shared-secret
            token). No safety gates (read-only). EA dead → list_positions raises (UNKNOWN ≠ FLAT,
            2026-07-02 doctrine) → status=error; the caller must NOT treat that as flat."""
            import json as _json

            def _reply(code: int, obj: dict) -> None:
                self._respond(code, _json.dumps(obj) + "\n")

            client = (self.client_address[0] if self.client_address else "")
            if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                _reply(403, {"status": "rejected", "reason": f"non_localhost_client: {client!r}"})
                return
            if self.headers.get("Origin") or self.headers.get("Referer"):
                _reply(403, {"status": "rejected", "reason": "browser_origin_refused (DNS-rebind guard)"})
                return
            tok = self.headers.get("X-Submit-Token", "")
            expected = bridge._submit_token
            if not expected or tok != expected:
                _reply(403, {"status": "rejected", "reason": "bad_or_missing_submit_token"})
                return
            backend = MT5DemoBackend()  # reuses the running singleton _bridge (same process, no rebind)
            try:
                positions = backend.list_positions()
            except Exception as e:  # dead EA / no definitive answer — NEVER fabricate flat
                _reply(200, {"status": "error", "reason": f"list_positions_failed: {e}"})
                return
            _reply(200, {"status": "ok", "count": len(positions), "positions": positions})

        # ── S1a — the keystroke-charged-gun fire route (refutation §7.7) ──────────────
        def _handle_submit(self) -> None:
            """POST /submit — the trader's go.sh / charge-released fire path. Runs the FULL entry
            safety layer (incl. MUST-FIX #1 live-equity kill) then enqueues to the SAME EA queue the
            agent's MCP path uses; single-consume (#2) lives in bridge.submit(). localhost-bind +
            shared-secret token + no-Origin (#7) are the only transport auth. Always replies JSON."""
            import json as _json

            def _reply(code: int, obj: dict) -> None:
                self._respond(code, _json.dumps(obj) + "\n")

            # (#7) localhost-bind: only loopback clients may fire
            client = (self.client_address[0] if self.client_address else "")
            if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                _reply(403, {"status": "rejected", "reason": f"non_localhost_client: {client!r}"})
                return
            # (#7) DNS-rebind defense: a browser sets Origin/Referer; go.sh (curl) does not
            if self.headers.get("Origin") or self.headers.get("Referer"):
                _reply(403, {"status": "rejected", "reason": "browser_origin_refused (DNS-rebind guard)"})
                return
            # (#7) shared-secret token
            tok = self.headers.get("X-Submit-Token", "")
            expected = bridge._submit_token
            if not expected or tok != expected:
                _reply(403, {"status": "rejected", "reason": "bad_or_missing_submit_token"})
                return

            try:
                payload = _json.loads(self._read_body() or "{}")
            except Exception as e:
                _reply(400, {"status": "rejected", "reason": f"bad_json: {e}"})
                return

            symbol = payload.get("symbol") or config.FTMO_DEMO_PRIMARY_SYMBOL
            side = str(payload.get("side", "")).lower()
            decision_ref = str(payload.get("decision_ref", "")).strip()
            if side not in ("long", "short"):
                _reply(400, {"status": "rejected", "reason": f"bad_side: {side!r} (long|short)"})
                return
            if not decision_ref:
                _reply(400, {"status": "rejected",
                             "reason": "missing_decision_ref (required for single-consume #2)"})
                return
            try:
                lots = float(payload["lots"])
                sl_price = float(payload["sl_price"])
                tp_price = float(payload["tp_price"])
            except (KeyError, TypeError, ValueError) as e:
                _reply(400, {"status": "rejected",
                             "reason": f"bad_or_missing numeric field (lots/sl_price/tp_price): {e}"})
                return
            if lots <= 0:
                _reply(400, {"status": "rejected", "reason": f"non_positive_lots: {lots}"})
                return

            from .. import safety
            backend = MT5DemoBackend()  # reuses the running singleton _bridge (same process, no rebind)

            # ── MUST-FIX #3 (refutation §7.7) — stale-charge drift HARD-REFUSE + SL re-derive ──
            # A charged round computed minutes ago fires market with NO price re-validation; the
            # gate-pass TTL bounds AUTHORIZATION age, not spot drift. Without this, the gun is a
            # FASTER way to commit the exact 06-22 T-DEMO-002 adverse-latency leak it exists to kill.
            # The drift check is against the BROKER's OWN bid/ask (the EA `quote` op) — the price THIS
            # broker will fill at — NOT the Skilling watcher feed, which has a basis offset (review
            # 2026-06-26 FATAL: drift-checking a broker fill against a different feed is wrong-basis).
            # CHARGED fires carry entry_ref + max_slip_pt; a raw S1a fire omits them (no charge to
            # drift). Server-side (the chokepoint) so a direct POST cannot bypass it. FAIL-CLOSED if
            # the broker quote is unavailable (old .ex5 without the quote op → recompile required).
            entry_ref = payload.get("entry_ref")
            max_slip_pt = payload.get("max_slip_pt")
            # ── CHANNEL RESOLUTION (adversarial-review fix #1, 2026-07-23): the SENDER's payload
            # stamp is authoritative (auto_shoot stamps "auto", go.sh stamps "manual"). The shared
            # last_gate_pass.json is a RACY slot — charge_keeper rewrites it every ~30s and BOTH
            # channels' gates write it while cross-channel simultaneous fire is doctrine — so a
            # gate-pass-only read could hard-refuse the TRADER'S GUN (a 2026-06-30 directive
            # violation) or hand an auto fire a "manual" read (cap dodge). Gate-pass channel is
            # only the fallback for legacy senders that don't stamp.
            _chan = payload.get("channel")
            if _chan is None:
                try:
                    import json as _cj
                    _chan = _cj.loads(config.GATE_PASS_PATH.read_text()).get("channel")
                except Exception:
                    _chan = None
            # ── review fix #4 (narrowed): an AUTO submit MUST carry the drift fields — otherwise
            # stripping entry_ref/max_slip_pt silently disables the binding cap and the "server-
            # side so a direct POST cannot bypass it" claim is false. auto_shoot always sends
            # both, so nothing legitimate refuses here. (The MCP submit path having NO drift
            # check at all is a PRE-EXISTING separate gap — recorded in
            # PROPOSAL_watcher_event_fire_T3 §14, deliberately not half-fixed here.)
            if _chan == "auto" and (entry_ref is None or max_slip_pt is None):
                _rej = {"status": "rejected",
                        "reason": ("auto_submit_missing_drift_fields: channel=auto requires "
                                   "entry_ref + max_slip_pt — the binding slip cap is not "
                                   "optional on the machine channel")}
                safety.append_audit("submit", {"symbol": symbol, "side": side, "lots": lots,
                                               "decision_ref": decision_ref,
                                               "drift_check": {"channel": "auto", "missing_fields": True}},
                                    {}, _rej)
                _reply(200, _rej)
                return
            drift_note = None
            if entry_ref is not None and max_slip_pt is not None:
                try:
                    entry_ref = float(entry_ref); max_slip_pt = float(max_slip_pt)
                except (TypeError, ValueError) as e:
                    _reply(400, {"status": "rejected", "reason": f"bad entry_ref/max_slip_pt: {e}"})
                    return
                q = backend.get_quote(symbol)
                # side-correct spot = the price WE fill at (long→ask, short→bid); mid as fallback
                spot = None
                if isinstance(q, dict) and q.get("status") == "ok":
                    spot = q.get("ask") if side == "long" else q.get("bid")
                    spot = spot if isinstance(spot, (int, float)) else q.get("mid")
                # Minimal intent for auditing a PRE-gate charged refusal (the full intent isn't built
                # until after this block). System-H observability: without these append_audit calls the
                # #3 drift HARD-REFUSE + broker-quote fail-close are invisible to oas_execute_audit.jsonl,
                # so the gun report's drift-refuse measurement (a headline gun safety property) reads a
                # false zero. Audit BEFORE the reply; never blocks (append_audit is a plain file append).
                _drift_intent = {"symbol": symbol, "side": side, "lots": lots, "sl_price": sl_price,
                                 "tp_price": tp_price, "decision_ref": decision_ref,
                                 "drift_check": {"entry_ref": entry_ref, "max_slip_pt": max_slip_pt}}
                if not isinstance(spot, (int, float)) or spot <= 0:
                    _rej = {"status": "rejected",
                            "reason": (f"broker_quote_unavailable: {q.get('reason', q.get('status')) if isinstance(q, dict) else q!r} "
                                       f"— a charged fire HARD-REFUSES without a BROKER price to drift-check "
                                       f"(#3 fail-closed). If the EA lacks the 'quote' op, recompile/reattach "
                                       f"OAS_Bridge.ex5 (the watcher/Skilling feed is wrong-basis, not a fallback).")}
                    safety.append_audit("submit", _drift_intent, {}, _rej)
                    _reply(200, _rej)
                    return
                drift = abs(float(spot) - entry_ref)
                # SLIP CAP IS ADVISORY (2026-06-30 trader directive): the gun is the trader's MANUAL
                # channel — at the `!bin/g` keystroke the trader is watching the tape and OWNS the slip
                # decision, so a machine MUST NOT refuse the deliberate shoot on drift. The cap is now a
                # LOUD, AUDITED warning (`slip_advisory_exceeded`), NEVER a block. NOTE what is DELIBERATELY
                # KEPT as a hard refuse: the broker_quote_unavailable fail-close ABOVE (no price = can't
                # re-derive the SL + truly blind ≠ "don't cap my slip"). max_slip_pt is now the WARN
                # threshold, not a gate. Risk stays bounded by the SL re-derive below (SL = fill ∓ sl_dist).
                slip_exceeded = drift > max_slip_pt
                # ── AUTO channel: the cap is BINDING (2026-07-23 trader-directed, refuter-#3-narrowed;
                # PROPOSAL_watcher_event_fire_T3 §13.7(b), tightened by the same-night adversarial
                # code review §14). The 06-30 advisory demotion's rationale is explicitly manual-only
                # ("the trader is watching the tape") — no human watches an AUTO submit, and the
                # T-DEMO-288 54pt chase passed with only an audit note. This RESTORES the original
                # MUST-FIX #3 hard-refuse for channel=auto only, inside this already-registered
                # chokepoint (enforcement_registry: oas_execute_mcp/safety.py — H40 net-zero, 8/8).
                # Review fix #3: bind on ADVERSE drift ONLY — abs() would refuse DISCOUNT fills, the
                # live-proven false-block class (safety.py:121-127: "48.8pt drift down → cheaper long
                # entry → false block"). A better-than-anchor fill FILLS; the advisory flag keeps
                # abs() semantics for the manual channel's loud warning.
                # Cap VALUE = the payload's max_slip_pt (staged line carries the trigger's own armed
                # tolerance; else config/auto_shoot.json per-class max_slip_pt — a knob, never a
                # buried constant). NOT a timer replacement: the 90s staleness guard is UNTOUCHED.
                if _chan == "auto":
                    _adverse = (float(spot) - entry_ref) if side == "long" else (entry_ref - float(spot))
                    if _adverse > max_slip_pt:
                        _rej = {"status": "rejected",
                                "reason": (f"slip_cap_exceeded_auto: broker spot {float(spot):.1f} is "
                                           f"{_adverse:.1f}pt ADVERSE of entry_ref {entry_ref:.1f} "
                                           f"(> max_slip {max_slip_pt:g}pt) — an AUTO fire must fill AT "
                                           f"the level, never the extension. Re-read the tape and re-arm "
                                           f"(the manual channel is unaffected).")}
                        _drift_intent["drift_check"].update(
                            {"broker_spot": float(spot), "drift_pt": round(drift, 1),
                             "adverse_pt": round(_adverse, 1), "channel": "auto", "binding": True})
                        safety.append_audit("submit", _drift_intent, {}, _rej)
                        _reply(200, _rej)
                        return
                # re-derive SL off the LIVE broker price, preserving the charged SL distance (always)
                sl_distance_pt = payload.get("sl_distance_pt")
                try:
                    sl_distance_pt = float(sl_distance_pt) if sl_distance_pt is not None else abs(entry_ref - sl_price)
                except (TypeError, ValueError):
                    sl_distance_pt = abs(entry_ref - sl_price)
                new_sl = (float(spot) - sl_distance_pt) if side == "long" else (float(spot) + sl_distance_pt)
                drift_note = {"broker_spot": float(spot), "entry_ref": entry_ref, "drift_pt": round(drift, 1),
                              "sl_orig": sl_price, "sl_rederived": round(new_sl, 1),
                              "sl_distance_pt": sl_distance_pt, "max_slip_pt": max_slip_pt,
                              "slip_advisory_exceeded": slip_exceeded,
                              # review fix #9: fills carry the channel too, so gun_session_report can
                              # exclude auto rows instead of miscounting them as charged gun fills
                              "channel": _chan}
                sl_price = round(new_sl, 1)

            # Run the FULL entry safety layer against the same singleton bridge, then fire.
            intent = {"symbol": symbol, "side": side, "lots": lots,
                      "sl_price": sl_price, "tp_price": tp_price, "decision_ref": decision_ref}
            if drift_note:
                intent["drift_check"] = drift_note
            ok, checks = safety.run_entry_safety_gates(intent, backend)
            if not ok:
                # gates failed → NOT enqueued → decision_ref NOT consumed (claim happens in submit())
                safety.append_audit("submit", intent, checks, {"status": "rejected_by_safety"})
                _reply(200, {"status": "rejected_by_safety", "checks": checks})
                return
            resp = backend.submit_order(symbol=symbol, side=side, lots=lots,
                                        sl_price=sl_price, tp_price=tp_price,
                                        decision_ref=decision_ref)
            safety.append_audit("submit", intent, checks, resp)
            _reply(200, resp if isinstance(resp, dict) else {"status": "error", "reason": str(resp)})
    return _Handler


_bridge = _Bridge()


def _send_and_wait(fields: dict[str, Any], timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    t0 = time.perf_counter()
    resp = _bridge.submit(fields, timeout_sec=timeout_sec)
    if not isinstance(resp, dict):
        resp = {"status": "error", "reason": f"non-dict response: {resp!r}"}
    resp["latency_total_ms"] = int((time.perf_counter() - t0) * 1000)
    return resp


def _heartbeat_check() -> tuple[bool, str, float]:
    if not HEARTBEAT_PATH.exists():
        return False, "heartbeat_missing: EA not started yet (no heartbeat.txt in Common/Files/oas_bridge/)", -1.0
    try:
        text = HEARTBEAT_PATH.read_text()
        parsed: dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                parsed[k.strip()] = v.strip()
        ts_str = parsed.get("ts_iso")
        if not ts_str:
            return False, "heartbeat_no_timestamp", -1.0
        norm = ts_str.replace(".", "-", 2).replace("Z", "+00:00")
        ts = datetime.fromisoformat(norm)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > HEARTBEAT_TTL_SEC:
            return False, f"heartbeat_stale: {age:.1f}s old (max {HEARTBEAT_TTL_SEC}s)", age
        return True, f"heartbeat_fresh: {age:.1f}s old", age
    except Exception as e:
        return False, f"heartbeat_unreadable: {e}", -1.0


def _overlay_close_price(resp, pre_close_mark):
    """LD-EXIT-6: fill in close_price from the pre-close mark when the EA returns 0/missing.

    The running OAS_Bridge .ex5 does not read the closed deal's price back (HistoryDealGetDouble),
    so a close returns close_price=0.0. Unlike the submit fill_price overlay we CANNOT recover it
    from list_positions after the fact — the position is GONE once closed — so close_order captures
    the position's live mark BEFORE sending the close and we overlay it here. A market close fills
    near that mark (± spread/slippage), so it is a faithful exit price, never a silent 0.0 that
    would corrupt every exit-side gauge downstream (exit_quality realized_R / cf_replay realized-vs-CF
    / source_attribution). Never overrides a real (non-zero) close_price the EA does report.
    Mutates + returns resp.
    """
    try:
        if (isinstance(resp, dict) and resp.get("status") == "closed"
                and not resp.get("close_price") and pre_close_mark):
            resp["close_price"] = pre_close_mark
            resp["_close_price_source"] = "backend_overlay_pre_close_mark"
    except Exception:
        pass
    return resp


# ── SYSTEM-G capability-lock tripwire (red-team CAP-1/CAP-2, 2026-06-23) ──────────────────────
# The System-G background driver runs a headless `claude -p` BURST whose ONLY advertised capital guard
# was "--strict-mcp-config → no MCP → cannot reach mcp__oas-execute__*". That closes the MCP route ONLY.
# THIS backend is a pure-Python path to the live FTMO-Demo EA (no MCP, no agent tool): a burst's Bash
# tool could drive the calling system's order CLI, or import this class and place a real order
# (~/.claude/settings.json runs Bash in bypassPermissions with no deny-list). So the burst subprocess is
# launched with G_BURST_NO_CAPITAL=1 (bin/g_burst.py / bin/g_driver.sh) and the ACTUATOR itself refuses
# under that marker — making C8 ("capital stays on the interactive thread") true BY CONSTRUCTION, not by
# the burst's (defeatable) persona text. The marker is set ONLY in the burst env; the interactive thread,
# the oas-execute MCP server process, and every other system (F2 …) never set it, so they are untouched.
def _g_burst_capital_locked() -> Optional[str]:
    if os.environ.get("G_BURST_NO_CAPITAL") == "1":
        return ("capital_locked: G_BURST_NO_CAPITAL=1 — the System-G background burst is capability-locked "
                "and MUST NOT place/modify/close orders (C8: capital is the interactive thread's only)")
    return None


class MT5DemoBackend(Backend):
    """HTTP-bridge backend talking to OAS_Bridge.mq5 v3 (WebRequest)."""

    def __init__(self) -> None:
        _lock = _g_burst_capital_locked()
        if _lock:
            # refuse to even construct (and thus to bind the EA bridge) inside a capability-locked burst
            raise PermissionError(_lock)
        _bridge.start()

    def _require_live_ea(self) -> Optional[dict]:
        ok, detail, age = _heartbeat_check()
        if not ok:
            return {"status": "rejected", "reason": f"ea_not_live: {detail}",
                    "heartbeat_age_sec": age}
        return None

    def submit_order(self, symbol, side, lots, sl_price, tp_price, decision_ref,
                     entry_ref=None, max_slip_pt=None):
        """★ entry_ref / max_slip_pt added 2026-08-04 (fault exec-probe-gatepass-deadlock, trader-authorized).

        The server-side guard above REFUSES any channel=auto submit that omits these two fields —
        correctly: stripping them silently disables the binding slip cap, and that cap exists to stop
        the 06-22 adverse-latency leak. But this method built a FIXED payload with no way to supply
        them, so every caller on this path (bin/exec_permission_probe.py, oas_calibrate_pt_value.py)
        was structurally unable to satisfy a guard it could not see. LD-1's pre-bell execution proof
        was therefore DEAD, and it failed in a way indistinguishable from real execution-dead — the
        worst possible failure mode for a liveness probe. Filed 2026-07-30, re-filed 2026-08-04.

        ⚠ ADDITIVE AND DEFAULT-OFF: both default to None and are omitted from the payload unless
        supplied, so every existing caller sends a BYTE-IDENTICAL payload and no cap is weakened.
        The guard itself is untouched — this lets a caller COMPLY with it, it does not bypass it.
        """
        _lock = _g_burst_capital_locked()   # CAP-1/CAP-2 belt (defense-in-depth)
        if _lock:
            return {"status": "rejected", "reason": _lock}
        gate = self._require_live_ea()
        if gate:
            return gate
        _payload = {
            "op": "submit",
            "symbol": symbol,
            "side": side,
            "lots": lots,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "decision_ref": decision_ref,
        }
        if entry_ref is not None:
            _payload["entry_ref"] = entry_ref
        if max_slip_pt is not None:
            _payload["max_slip_pt"] = max_slip_pt
        resp = _send_and_wait(_payload)
        # EA-side fix uses POSITION_PRICE_OPEN/HistoryDealGetDouble but the running
        # EA may not yet have the new .ex5 loaded. Overlay the real entry price
        # from list_positions when the EA reports fill_price=0.
        if resp.get("status") == "submitted" and not resp.get("fill_price"):
            try:
                order_id = str(resp.get("broker_order_id", ""))
                if order_id:
                    for pos in self.list_positions():
                        if str(pos.get("broker_order_id", "")) == order_id:
                            real_fill = pos.get("entry_price")
                            if real_fill:
                                resp["fill_price"] = real_fill
                                resp["_fill_price_source"] = "backend_overlay_position_list"
                            break
            except Exception:
                pass
        return resp

    def modify_order(self, broker_order_id, sl_price=None, tp_price=None, decision_ref=""):
        _lock = _g_burst_capital_locked()   # CAP-1/CAP-2 belt (defense-in-depth)
        if _lock:
            return {"status": "rejected", "reason": _lock}
        gate = self._require_live_ea()
        if gate:
            return gate
        return _send_and_wait({
            "op": "modify",
            "broker_order_id": broker_order_id,
            "sl_price": sl_price if sl_price is not None else "",
            "tp_price": tp_price if tp_price is not None else "",
            "decision_ref": decision_ref,
        })

    def close_order(self, broker_order_id, lots=None, decision_ref=""):
        _lock = _g_burst_capital_locked()   # CAP-1/CAP-2 belt (defense-in-depth)
        if _lock:
            return {"status": "rejected", "reason": _lock}
        gate = self._require_live_ea()
        if gate:
            return gate
        # LD-EXIT-6: read the position's live mark BEFORE the close — after the close it is GONE,
        # so (unlike the submit overlay) we cannot recover the price from list_positions post-hoc.
        # A market close fills near this mark; _overlay_close_price uses it iff the EA returns 0.
        pre_close_mark = None
        try:
            oid = str(broker_order_id)
            for pos in self.list_positions():
                if str(pos.get("broker_order_id", "")) == oid:
                    pre_close_mark = pos.get("current_price")
                    break
        except Exception:
            pass
        resp = _send_and_wait({
            "op": "close",
            "broker_order_id": broker_order_id,
            "lots": lots if lots is not None else "",
            "decision_ref": decision_ref,
        })
        return _overlay_close_price(resp, pre_close_mark)

    def close_all(self, reason):
        return _send_and_wait({"op": "close_all", "reason": reason}, timeout_sec=15.0)

    def list_positions(self):
        # 2026-07-02 fix (verify-EFFECT class): NEVER fabricate a well-formed "flat" from a dead EA.
        # The old behavior returned [] on heartbeat-stale/timeout/error — position_list then printed
        # {"positions": []}, which reconcile_from_broker consumed as broker-truth-flat (its PF-6
        # false-flat guard only catches MALFORMED returns, not a fabricated empty list). Raising here
        # surfaces as {"status":"error",...} at the MCP layer → PF-6 REFUSES loud. UNKNOWN ≠ FLAT.
        gate = self._require_live_ea()
        if gate:
            raise RuntimeError(f"position_list refused: {gate['reason']} — broker positions UNKNOWN, do NOT assume flat")
        resp = _send_and_wait({"op": "list_positions"})
        if resp.get("status") != "ok":
            raise RuntimeError(f"position_list got no definitive EA answer (status={resp.get('status')!r}"
                               f" reason={resp.get('reason', '')!r}) — broker positions UNKNOWN, do NOT assume flat")
        count = int(resp.get("count", 0))
        positions = []
        for i in range(count):
            p = {}
            for key in ("broker_order_id", "symbol", "side", "lots", "entry_price",
                        "current_price", "sl_price", "tp_price", "pnl", "magic", "comment"):
                p[key] = resp.get(f"position.{i}.{key}")
            positions.append(p)
        return positions

    def get_account_info(self):
        resp = _send_and_wait({"op": "account_info"})
        return {**resp, "backend": "mt5_demo", "ts_iso": _now_iso()}

    def get_quote(self, symbol):
        """BROKER's own live bid/ask (the #3 drift-guard spot source). Returns the EA's quote dict
        (status/bid/ask/mid/symbol) or {status:error,...}. An OLD .ex5 without the quote op returns
        status=error reason=unknown_op:quote → the caller fails closed. Cheap (one EA round-trip)."""
        gate = self._require_live_ea()
        if gate:
            return gate
        return _send_and_wait({"op": "quote", "symbol": symbol}, timeout_sec=4.0)

    def health(self):
        ok, detail, age = _heartbeat_check()
        result = {
            "ok": ok,
            "detail": detail,
            "backend": "mt5_demo",
            "version": config.SERVER_VERSION,
            "bridge_dir": str(BRIDGE_DIR),
            "bridge_dir_exists": BRIDGE_DIR.exists(),
            "heartbeat_age_sec": age,
            "listener_host": LISTENER_HOST,
            "listener_port": LISTENER_PORT,
            "transport": "http",
        }
        if ok:
            ping = _send_and_wait({"op": "ping"}, timeout_sec=3.0)
            result["ping"] = ping
            result["ok"] = ping.get("status") == "ok"
            result["latency_ms"] = ping.get("latency_total_ms", -1)
            # Stale-EA detection: backend expects v3.10+ (fill_price fallback +
            # fresh ea_version reporting). Older binaries still respond
            # "ea_version=3.00" (or just "3.0"), which is a hint that the EA
            # was not reattached after the source bump. Surface as a warning
            # so oas_smoke.sh can flag it.
            try:
                v_raw = str(ping.get("ea_version", "")).strip()
                v_num = float(v_raw) if v_raw else None
                result["ea_version"] = v_raw or None
                if v_num is not None and v_num < 3.10:
                    result["warn_stale_ea"] = True
                    result["warn_stale_ea_detail"] = (
                        f"EA reports ea_version={v_raw}; expected ≥ 3.10. "
                        f"Reattach OAS_Bridge in MT5 to load the new .ex5."
                    )
            except (TypeError, ValueError):
                # Unparseable version — leave as-is, don't warn.
                pass
        return result
