"""Safety layer — runs BEFORE every write to backend.

This is the last line of defense between Claude's decision loop and the broker. If Claude
has a bug, the safety layer must catch it. Mirrors the calling system's own pre-trade rule set
logic for entries; adds connector-only checks (kill switch, rate limit, position sanity).

Every check returns (passed: bool, reason: str). The MCP server tool dispatcher composes
checks and short-circuits on first failure. Every check + outcome is audit-logged.
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from . import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_et_date() -> str:
    # Audit log groups by ET date for FTMO daily counters
    # Simple approx: UTC-4 (EDT). Use proper ET handling in production.
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    return et.strftime("%Y-%m-%d")


def append_audit(op: str, intent: dict, checks: dict, result: dict) -> None:
    """Atomic audit log. Append BEFORE the actual backend call where possible, so
    a crash mid-submission leaves a recoverable trace."""
    config.AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_iso": _now_iso(),
        "et_date": _today_et_date(),
        "op": op,
        "intent": intent,
        "safety_checks": checks,
        "result": result,
    }
    with config.AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def check_killswitch() -> tuple[bool, str]:
    """Kill switch is a file. If it exists, refuse all writes.
    Settable by Claude (close_all tool), trader (touch), heartbeat watchdog, cron."""
    if config.KILL_SWITCH_PATH.exists():
        try:
            reason = config.KILL_SWITCH_PATH.read_text().strip() or "<empty file>"
        except Exception:
            reason = "<unreadable>"
        return False, f"kill_switch_active: {reason}"
    return True, "killswitch_clear"


def check_rate_limit() -> tuple[bool, str]:
    """Count today's write operations in audit log. FTMO ToS: <2000 server requests/day."""
    if not config.AUDIT_LOG_PATH.exists():
        return True, "rate_limit_ok: 0/daily"
    today = _today_et_date()
    count = 0
    write_ops = {"submit", "modify", "close", "close_all"}
    with config.AUDIT_LOG_PATH.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("et_date") == today and rec.get("op") in write_ops:
                count += 1
    if count >= config.RATE_LIMIT_DAILY:
        return False, f"rate_limit_exceeded: {count}/{config.RATE_LIMIT_DAILY}"
    return True, f"rate_limit_ok: {count}/{config.RATE_LIMIT_DAILY}"


def check_gate_pass_freshness(intent: dict) -> tuple[bool, str]:
    """For entries only: require a fresh PASS from the calling system's pre-trade gate.
    Modify/close are NOT gate-gated — only fresh entries are."""
    if not config.GATE_PASS_PATH.exists():
        return False, "gate_pass_missing: no fresh gate-pass record at $OAS_DATA_DIR/state/last_gate_pass.json"
    try:
        gate = json.loads(config.GATE_PASS_PATH.read_text())
    except Exception as e:
        return False, f"gate_pass_unreadable: {e}"
    ts_str = gate.get("ts_iso") or gate.get("timestamp") or gate.get("ts")
    if not ts_str:
        return False, "gate_pass_no_timestamp"
    try:
        gate_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception as e:
        return False, f"gate_pass_bad_timestamp: {e}"
    age_sec = (datetime.now(timezone.utc) - gate_ts).total_seconds()
    if age_sec > config.GATE_PASS_TTL_SEC:
        return False, f"gate_pass_stale: {age_sec:.0f}s old (max {config.GATE_PASS_TTL_SEC}s)"
    if gate.get("verdict") != "PASS":
        return False, f"gate_pass_not_pass: verdict={gate.get('verdict')!r}"
    # Param match (best-effort): symbol/side/conf must align
    for key in ("symbol", "side"):
        if key in gate and intent.get(key) is not None:
            if str(gate[key]).lower() != str(intent[key]).lower():
                return False, f"gate_pass_param_mismatch: {key} gate={gate[key]!r} intent={intent[key]!r}"
    # DESIGN_F B10 (J3 finding f — bug-fix class: the param-match was supposed to validate
    # the order but checked only symbol+side, so a fire could submit MORE lots or a FARTHER
    # stop than the gate passed — silently over-tier, the $588→$838 class). Enforced only
    # when the gate artifact carries the keys (backward compatible with older artifacts).
    if gate.get("size_lots") is not None and intent.get("lots") is not None:
        try:
            if float(intent["lots"]) > float(gate["size_lots"]) + 1e-6:
                return False, (f"gate_pass_size_exceeded: intent lots {intent['lots']} > gated "
                               f"{gate['size_lots']} (C2 — re-run the gate at the larger size)")
        except (TypeError, ValueError):
            pass
    if (gate.get("sl_pts") is not None and intent.get("sl_price") is not None):
        try:
            # C2 silent-risk: the submitted SL must not be WIDER than 2× the gated SL distance.
            # ANCHOR (TASK-008 fix, 2026-06-30 — re-anchor, supersedes the old "AUDIT-2 MINOR,
            # accepted"): measure off the ACTUAL entry basis the SL was re-derived from — the broker
            # spot in intent["drift_check"] for a charged fire — NOT the STALE gate["entry"]. The old
            # gate["entry"] anchor read a re-derived SL as sl_pts+drift, so a charge that drifted
            # > sl_pts was REFUSED even though true risk (|fill − SL|) was UNCHANGED — and worse, it
            # perversely rejected BETTER (discount) fills (proven live: shoot 2, 48.8pt drift down →
            # cheaper long entry → false block). Measuring off the re-derive basis = the true risk;
            # an ACTUAL widening beyond 2× gated still blocks (protection preserved). Raw/uncharged
            # fires carry no drift_check → fall back to gate["entry"] (behavior unchanged for them).
            drift_check = intent.get("drift_check") or {}
            anchor = drift_check.get("broker_spot")
            if anchor is None:
                anchor = gate.get("entry")
            if anchor is not None:
                implied = abs(float(anchor) - float(intent["sl_price"]))
                if implied > 2.0 * float(gate["sl_pts"]):
                    basis = "fill" if drift_check.get("broker_spot") is not None else "gate-entry"
                    return False, (f"gate_pass_sl_distance_exceeded: implied SL distance {implied:.0f}pt "
                                   f"(off {basis}) > 2× gated {gate['sl_pts']}pt (C2 silent-risk; a "
                                   f"TIGHTER SL is always allowed — only widening-beyond-gated blocks)")
        except (TypeError, ValueError):
            pass
    return True, f"gate_pass_fresh: {age_sec:.0f}s old"


def check_position_sanity(intent: dict, backend) -> tuple[bool, str]:
    """Cross-check broker state. Refuse contradictory entries (long + short same symbol);
    refuse modify/close on non-existent IDs."""
    symbol = intent.get("symbol")
    side = intent.get("side")
    positions = backend.list_positions()
    if not symbol or not side:
        return True, "position_sanity_skipped: missing symbol/side in intent"
    for p in positions:
        if p["symbol"] == symbol and p["side"] != side:
            return False, f"position_contradiction: existing {p['side']} {p['symbol']} #{p['broker_order_id']}"
    return True, f"position_sanity_ok: {len(positions)} existing"


def check_sl_widen_block(intent: dict, backend) -> tuple[bool, str]:
    """Constitution C5: a stop may only move TOWARD PROFIT, never widened.
    Long: new SL must be >= current SL. Short: new SL must be <= current SL.
    Setting an initial stop (no prior SL) is allowed; a TP-only modify is allowed.
    Mirrors the direction-sanity in the calling system's trail command, ported here so the LIVE mcp__oas-execute__order_modify path is guarded too
    (wired 2026-05-30 deep-review C5; closes hope-driven widening: small loss -> large loss)."""
    new_sl = intent.get("sl_price")
    if new_sl is None:
        return True, "widen_block_skip: no SL change (TP-only modify)"
    boid = intent.get("broker_order_id")
    pos = None
    for p in backend.list_positions():
        if str(p.get("broker_order_id")) == str(boid):
            pos = p
            break
    if pos is None:
        return True, f"widen_block_skip: position {boid} not found (position_sanity covers existence)"
    cur_sl = pos.get("sl_price")
    side = (pos.get("side") or "").lower()
    if cur_sl in (None, 0, 0.0, ""):
        return True, "widen_block_ok: setting initial stop (no prior SL to widen)"
    try:
        new_sl_f, cur_sl_f = float(new_sl), float(cur_sl)
    except (TypeError, ValueError):
        return True, f"widen_block_skip: non-numeric SL (new={new_sl!r} cur={cur_sl!r})"
    if side == "long" and new_sl_f < cur_sl_f:
        return False, f"sl_widen_blocked: long SL may only move UP (toward profit) — current {cur_sl_f} -> requested {new_sl_f} (C5)"
    if side == "short" and new_sl_f > cur_sl_f:
        return False, f"sl_widen_blocked: short SL may only move DOWN (toward profit) — current {cur_sl_f} -> requested {new_sl_f} (C5)"
    return True, f"widen_block_ok: {side} SL {cur_sl_f} -> {new_sl_f} (toward profit)"


# L0 — explicit prefix that marks an override as infra-validation
# (probes / audits / tests) rather than a real OAS decision divergence.
# Matched case-insensitively at the START of the `reason` field. Old substring
# match (probe / verify / audit / etc.) was retired 2026-05-19 because real
# trader reasons like "want to verify the SL is structural" got silently
# exempted. The prefix convention is unambiguous: operators MUST consciously
# tag an override as infra by writing `infra:` at the start.
_INFRA_OVERRIDE_PREFIX = "infra:"

# Amendment 2026-05-19: differentiate override
# rule types. Only DISCIPLINE rules (psychology-protection: R2 daily P&L cap,
# R3 trade-count cap, R5 consec-loss) trigger session-kill — those are explicit
# self-protection rules whose override is a "I'm overriding my own discipline"
# signal. WORKFLOW / THESIS-QUALITY rules (R10 corpus, R11 econ, R14 dual-frame,
# R19 post-win, R20 arm3-consensus, R21 emergency, R23 SL-thesis-consistency,
# R24 MTF-freshness) log but DO NOT kill — overriding a workflow rule is a
# context choice (e.g., dev-test, MTF unavailable, no econ calendar), not a
# discipline failure.
#
# Rationale: T-SIM-008 PM showed dev-test smoke-tests of R23/R24 cascaded into
# session-kill, locking out a legitimate would-be entry. The user's correction:
# "wouldn't the solution be a different SL and not stop the trade altogether?"
# This amendment implements that — workflow overrides require the trader to
# adjust (re-specify SL, re-run MTF, etc.) but don't terminate the session.
_KILL_RULES = frozenset({"R2", "R3", "R5"})


def check_session_override_kill() -> tuple[bool, str]:
    """L0 (amended 2026-05-19): override entries on DISCIPLINE rules
    (R2/R3/R5) terminate further entry/modify writes for the session. Override
    entries on WORKFLOW rules (R10/R11/R14/R19/R20/R21/R23/R24) are logged
    informationally but do NOT kill the session — the trader must re-specify
    the trade to satisfy the rule (e.g., widen SL for R23, re-run MTF for R24).

    Closes remain allowed regardless (flattening is the safety action). Kill
    resets at next-day ET rollover (the scan filters by session date).

    The infra-prefix filter still exempts overrides whose `reason` STARTS WITH
    `infra:` (case-insensitive)."""
    override_log = config.DATA_STATE / "override_log.jsonl"
    if not override_log.exists():
        return True, "override_kill_clear: no override log"
    today = _today_et_date()
    kill_entries = []
    workflow_overrides = []
    try:
        with override_log.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("session") != today:
                    continue
                reason = str(rec.get("reason", "")).lower().lstrip()
                if reason.startswith(_INFRA_OVERRIDE_PREFIX):
                    continue
                rule = str(rec.get("rule", "")).upper()
                if rule in _KILL_RULES:
                    kill_entries.append(rec)
                else:
                    workflow_overrides.append(rec)
    except Exception as e:
        return False, f"override_log_unreadable: {e}"
    if kill_entries:
        last = kill_entries[-1]
        return False, (
            f"session_override_killed: DISCIPLINE-rule override at {last.get('timestamp','?')} "
            f"(rule={last.get('rule','?')}, reason={last.get('reason','?')!r}); "
            f"session terminated; entries refused until ET rollover"
        )
    # Workflow overrides logged but not killing; surface count as informational
    if workflow_overrides:
        rules_str = ",".join(sorted({str(r.get("rule","?")) for r in workflow_overrides}))
        return True, f"override_kill_clear: {len(workflow_overrides)} workflow-rule override(s) today ({rules_str}) — informational, not session-killing"
    return True, "override_kill_clear: 0 non-infra overrides today"


def _et_date_zoneinfo() -> str:
    """Proper ET date (zoneinfo). Used by the live-equity kill for day_start matching so it
    agrees with survival_eval's survival_day_start.json (which is written with zoneinfo ET).
    Lazy import so a missing tzdata can never break the module at import time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return _today_et_date()  # UTC-4 approx fallback (correct during EDT)


def check_live_equity_kill(backend) -> tuple[bool, str]:
    """MUST-FIX #1 (FATAL hole, refutation §7.7) — enforce the Constitution C1 daily-kill on
    LIVE equity, not stale running_pnl and not the override log.

    The prior entry-gate chain (killswitch/override_kill/rate_limit/gate_pass/position_sanity)
    NEVER read P&L: check_session_override_kill only trips on an explicitly-written override row,
    and running_pnl only reflects CLOSED trades — open drawdown was invisible. So the −$2,000
    daily kill was FAIL-OPEN on EVERY entry path (MCP + the new /submit route). This closes it.

    session P&L (incl. open) = current_equity − day_start_balance.  Block iff that is at/below the
    effective kill line (bin/effective_kill_line.py = the single source, scalar-scaled, D035 item 2).
    Equivalently: block iff equity <= day_start_balance + kill_line_delta.

    Day-start balance is the SAME survival_day_start.json that survival_eval.py captures (shared
    SSOT — captured here on the first entry of the day if survival hasn't ticked yet, same format).

    FAIL-CLOSED: if equity is unreadable (EA down / non-numeric / account_info error) we REFUSE the
    entry — a capital kill gate must never fire blind. (_require_live_ea already blocks an EA-down
    submit; this makes the *gate* honest rather than silently passing.)"""
    # 1) live equity
    try:
        acct = backend.get_account_info()
        equity = acct.get("equity") if isinstance(acct, dict) else None
        equity = float(equity)
    except Exception as e:
        return False, f"equity_unreadable: {type(e).__name__}: {e} (fail-closed — kill gate refuses blind)"
    if equity != equity:  # NaN guard
        return False, "equity_unreadable: account_info returned NaN equity (fail-closed)"

    # 2) day-start balance (shared survival_day_start.json; capture on first entry of the day)
    daystart_path = config.DATA_STATE / "survival_day_start.json"
    today = _et_date_zoneinfo()
    day_start_balance = None
    try:
        if daystart_path.exists():
            rec = json.loads(daystart_path.read_text())
            if rec.get("date_et") == today and "day_start_balance" in rec:
                day_start_balance = float(rec["day_start_balance"])
    except Exception:
        day_start_balance = None
    if day_start_balance is None:
        # A4 (review 2026-06-26) — lazily self-capturing the baseline at FIRST FIRE is unsafe if the
        # account is already in drawdown (open float or a prior realized loss): it would anchor the
        # −$2,000 kill to a drawn-down zero and grant extra room. Only self-capture when FLAT
        # (equity ≈ balance — no floating P&L); otherwise FAIL-CLOSED and demand a session-start
        # baseline. (survival_eval / a presession writer should capture it pre-bell while flat.)
        try:
            balance = float(acct.get("balance", equity))
        except Exception:
            balance = equity
        if abs(equity - balance) > 5.0:  # open float present → not a clean zero
            return False, (f"no_trusted_daystart_baseline: open float {equity - balance:+.2f} present "
                           f"(equity {equity:.2f} vs balance {balance:.2f}) and no same-day baseline — "
                           f"refusing to anchor the C1 kill to a mid-position zero. Capture day_start at "
                           f"session start while FLAT (survival_eval/presession).")
        day_start_balance = balance
        try:
            config.DATA_STATE.mkdir(parents=True, exist_ok=True)
            daystart_path.write_text(json.dumps({
                "date_et": today, "day_start_balance": day_start_balance,
                "recorded_at": _now_iso(), "recorded_by": "safety.check_live_equity_kill (flat-capture)",
            }))
        except Exception:
            pass  # best-effort write; the in-memory value still gates this entry

    # 3) effective kill line (negative delta $; scalar-scaled — the single source)
    kill_delta = None
    try:
        import subprocess
        out = subprocess.run(
            ["python3", str(config.REPO_ROOT / "bin" / "effective_kill_line.py")],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            kill_delta = float(out.stdout.strip())
    except Exception:
        kill_delta = None
    if kill_delta is None:
        kill_delta = -2000.0  # CONSTITUTION C1 base (fail to the WIDER line; never trip earlier on a read miss)

    floor = day_start_balance + kill_delta
    session_pnl = equity - day_start_balance
    if equity <= floor:
        return False, (f"daily_kill_tripped: live equity {equity:.2f} <= floor {floor:.2f} "
                       f"(day_start {day_start_balance:.2f} {kill_delta:+.0f}); session P&L {session_pnl:+.2f} "
                       f"at/below C1 daily kill — entries refused (close-only)")
    return True, (f"equity_kill_clear: equity {equity:.2f} > floor {floor:.2f} "
                  f"(session P&L {session_pnl:+.2f}, kill at {kill_delta:+.0f})")


def run_entry_safety_gates(intent: dict, backend) -> tuple[bool, dict]:
    """Compose all entry-gate checks. Short-circuit on first failure.
    Returns (passed, checks_dict_for_audit)."""
    checks = {}
    for name, fn, args in [
        ("killswitch", check_killswitch, ()),
        ("override_kill", check_session_override_kill, ()),
        ("live_equity_kill", check_live_equity_kill, (backend,)),
        ("rate_limit", check_rate_limit, ()),
        ("gate_pass", check_gate_pass_freshness, (intent,)),
        ("position_sanity", check_position_sanity, (intent, backend)),
    ]:
        ok, reason = fn(*args)
        checks[name] = {"ok": ok, "reason": reason}
        if not ok:
            return False, checks
    return True, checks


def run_modify_safety_gates(intent: dict, backend) -> tuple[bool, dict]:
    """Modify path: kill-switch + override-kill + rate-limit. No gate-pass required
    (we're already in trade). Override-kill blocks SL/TP changes post-divergence —
    only close/close_all may run after a session-kill."""
    checks = {}
    for name, fn, args in [
        ("killswitch", check_killswitch, ()),
        ("override_kill", check_session_override_kill, ()),
        ("rate_limit", check_rate_limit, ()),
        ("sl_widen_block", check_sl_widen_block, (intent, backend)),
    ]:
        ok, reason = fn(*args)
        checks[name] = {"ok": ok, "reason": reason}
        if not ok:
            return False, checks
    return True, checks


def run_close_safety_gates(intent: dict, backend) -> tuple[bool, dict]:
    """Close path: rate-limit only. Kill-switch DOES NOT block closes (kill switch must allow flatten).
    Closing is the safety action, not the dangerous one."""
    checks = {}
    ok, reason = check_rate_limit()
    checks["rate_limit"] = {"ok": ok, "reason": reason}
    if not ok:
        return False, checks
    return True, checks
