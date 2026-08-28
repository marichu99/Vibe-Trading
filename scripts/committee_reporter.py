"""Runs Vibe-Trading swarm committees on a schedule and emails a report.

Runs one or more committee presets (bull/bear/risk/PM debates) against
configured targets and emails the result. A target with a `trade` entry also
has the agent check positions and conditionally place a demo order on the
PM's decision (see TARGETS below) — a target without one is research-only,
no trading. is_reportable() controls which runs are worth an email.

This is the single place that both decides and (optionally) trades — do not
also run the same instrument through the server's /scheduled-runs executor,
or two independent processes can both check-then-place around the same race
window.

Each committee run is a fresh, isolated `python -m cli run` subprocess (the
same path the `vibe-trading` CLI itself uses), so a stuck or crashed run never
takes the reporter down with it.

Usage:
    # One pass over all TARGETS, then exit (good for Windows Task Scheduler):
    .venv\\Scripts\\python.exe scripts\\committee_reporter.py --once

    # Keep running, repeating every --interval seconds:
    .venv\\Scripts\\python.exe scripts\\committee_reporter.py --loop --interval 3600

Required environment (e.g. in ~/.vibe-trading/.env, or your shell):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
Optional: EMAIL_CC (comma-separated)
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("committee_reporter")

# --------------------------------------------------------------------------- #
# What to run each pass — add/remove targets freely.
# --------------------------------------------------------------------------- #

TARGETS: list[dict[str, object]] = [
    {
        # LIVE — real money (mt5-live-trade), not demo. Gold only: this is the
        # sole symbol/asset-class the committed mt5 mandate authorizes (see
        # scripts/commit_mt5_mandate.py). max_stack=1 overrides the global
        # MAX_SAME_DIRECTION_POSITIONS: no pyramiding on a ~$30 live account.
        "committee": "investment_committee", "target": "XAUUSD", "market": "commodity/forex",
        "trade": {"symbol": "XAUUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1},
    },
    # PAUSED 2026-08-25: an MT5 terminal can only be signed into ONE account at
    # a time. The terminal is now signed into the LIVE account (needed for the
    # gold target above), so these mt5-demo-trade targets crash every pass with
    # MT5ProfileMismatchError (demo profile guard correctly refusing to run
    # against a non-demo account). Re-enable once either the terminal is back
    # on demo (breaking live gold) or a second terminal instance is running the
    # demo account with its own terminal_path (see mt5-demo-trade profile).
    # {
    #     "committee": "investment_committee", "target": "EURUSD", "market": "forex",
    #     "trade": {"symbol": "EURUSDm", "connection": "mt5-demo-trade", "lots": 0.01},
    # },
    # {
    #     # US Tech 100 (Nasdaq) index CFD. Exness's own minimum lot for this
    #     # symbol is 0.05 (not 0.01) - verified live via symbol_info.
    #     "committee": "investment_committee", "target": "USTEC (Nasdaq 100)", "market": "US index",
    #     "trade": {"symbol": "USTECm", "connection": "mt5-demo-trade", "lots": 0.05},
    # },
    # {
    #     # S&P 500 index CFD. Exness's own minimum lot for this symbol is 0.14.
    #     "committee": "investment_committee", "target": "US500 (S&P 500)", "market": "US index",
    #     "trade": {"symbol": "US500m", "connection": "mt5-demo-trade", "lots": 0.14},
    # },
    # {"committee": "risk_committee", "target": "XAUUSD", "market": "commodity/forex"},
]

# This caps the WRAPPING agent's own loop (run_swarm once, check positions,
# maybe place an order, write the report) — a handful of iterations is
# normally enough for that. It does NOT cap each swarm sub-agent's own
# iteration budget (bull/bear/risk/PM each have their own max_iterations: 50
# in the shared investment_committee.yaml preset, used app-wide) — that's
# the bulk of the real spend (one PM task alone used 170k+ tokens across 17
# iterations of independent verification) and would need a separate,
# bigger-blast-radius edit to the shared preset to reduce.
MAX_ITER = 15
RUN_TIMEOUT_SECONDS = 3600

# Cap on concurrent SAME-direction positions per symbol. Opposite-direction
# stacking is never allowed regardless of this cap (avoids a same-symbol
# long+short conflict on the hedging-mode MT5 account). A target's own trade
# dict may override this via "max_stack" (see TARGETS — the live gold target
# uses max_stack=1, no pyramiding).
MAX_SAME_DIRECTION_POSITIONS = 4

# Connection ids that place REAL orders against real money, as opposed to a
# broker's demo/paper sandbox. Gates the circuit breaker below — it never
# touches demo trading, which has no capital at risk.
LIVE_CONNECTIONS = {"mt5-live-trade"}

# Auto-halt live trading for the rest of the UTC day if equity drops this much
# from the balance recorded at the first live check of that day. Independent
# of (in addition to) the committed mandate's own hard caps (max order/total
# notional, max leverage, max trades/day) — this is a portfolio-level backstop
# those per-order caps don't cover. Trips the SAME kill-switch sentinel the
# mandate gate checks (src.live.halt) so it also blocks any other code path
# hitting this account, and flattens the open position itself (committee_
# reporter doesn't run through the live runtime scheduler that would
# otherwise do this automatically on a halt trip).
LIVE_DRAWDOWN_HALT_PCT = 0.5
LIVE_BASELINE_PATH = REPO_ROOT / "logs" / "live_baseline.json"

# Magic number stamped on every order committee_reporter/the mt5 connector
# places (MT5Config's default `magic`, agent/src/trading/connectors/mt5/sdk.py).
# Anything else on this account — a separately-running signal-service EA,
# manual trades — carries a different magic. Used both to keep the
# same-direction position cap from double-counting someone else's position as
# ours, and to summarize that other activity as context (see
# _signal_service_activity below).
OUR_MAGIC = 20260000
SIGNAL_LOOKBACK_DAYS = 7
# Hard cap on lines in the signal-service context block, regardless of how
# active that EA actually is — an unbounded block would silently grow (and
# grow prompt cost) in direct proportion to someone else's trade frequency.
SIGNAL_ACTIVITY_MAX_LINES = 5

# Trade journal: our own past decisions + verified outcomes, fed back into the
# prompt as a short stats line so the committee has situational awareness of
# its own recent track record — no persisted memory existed before this (see
# committee_reporter.py's own docstring: every run is a fresh, isolated
# subprocess). Deliberately terse (one line, not full reasoning replay/an
# extra LLM call) — the whole point was minimum added token cost, not a
# richer feedback mechanism.
TRADE_JOURNAL_PATH = REPO_ROOT / "logs" / "trade_journal.json"
JOURNAL_SUMMARY_WINDOW = 8

# Excursion analysis: for every closed trade, pull the real price bars between
# open and close and check whether it moved favorably before the outcome it
# ended with — distinguishing a "clean" loss (moved against the position
# almost immediately; the stop did its job, nothing to fix) from a
# "reversal" (moved meaningfully in profit, then gave it back — the kind of
# loss a breakeven-stop rule could actually have helped). Pure historical-bar
# reads, zero LLM cost — this is how "learn from every win and loss" happens
# without an extra committee run per trade.
EXCURSION_BAR_PERIOD = "15m"
REVERSAL_THRESHOLD_FRACTION = 0.5  # favorable move >= this fraction of the stop distance

# Worst-case planned loss per order, enforced deterministically at the mandate
# gate (see scripts/commit_mt5_mandate.py — MUST match MAX_LOSS_PER_ORDER_USD
# there, these are two independently-maintained numbers). The gate denies
# outright if breached; this constant lets the prompt tell the committee the
# actual price-distance budget up front, so its stops land WITHIN the cap
# instead of just getting denied more often for no smaller-risk benefit.
MAX_LOSS_PER_ORDER_USD = 10.0

# Milestone reminder: the user asked to be told once the live account hits
# this equity, to reconsider adding silver (XAGUSDm — the nearest cousin to
# gold, least new plumbing) to the live portfolio. Auto-clears once a silver
# target actually exists in TARGETS — no separate "already notified" state
# needed; if it keeps firing, silver genuinely hasn't been added yet.
MILESTONE_SILVER_EQUITY_USD = 100.0

# Singleton lock: refuse to start a second instance. 2026-08-25: a Task
# Scheduler AtLogOn relaunch and a manual startup.ps1 run overlapped, giving
# two independent loops that both reached a live-money trading decision on
# the same symbol at the same moment — exactly the race the module docstring
# above warns about (one process's "no open position" check can't see the
# other's about-to-be-placed order). See _acquire_singleton_lock().
LOCK_PATH = REPO_ROOT / "logs" / "committee_reporter.lock"

# Matches the redirect target startup.ps1 gives -RedirectStandardOutput —
# committee_reporter.py itself only logs to stdout (logging.basicConfig
# below); this constant exists purely so --status can read the same file
# back, not to configure logging output itself.
REPORTER_LOG_PATH = REPO_ROOT / "logs" / "reporter.log"


def _pid_is_alive(pid: int) -> bool:
    """Windows-only liveness check via OpenProcess (no extra dependency)."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _acquire_singleton_lock() -> bool:
    """Claim the lock, refusing to start if another instance already holds it.

    A stale lock (its recorded PID is no longer a live process) self-heals —
    reclaimed automatically rather than requiring manual cleanup, since an
    abrupt kill (Stop-Process, a crash) never runs an exit handler.

    Returns:
        True if the lock was acquired (safe to proceed). False if another
        instance is genuinely running — the caller must exit without doing
        anything: guessing "it's probably fine" is exactly the failure mode
        this guards against.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            existing_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = None
        if existing_pid and _pid_is_alive(existing_pid):
            return False
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _symbol_live_quote(symbol: str, connection: str) -> dict | None:
    """Live bid/ask for `symbol` from the broker actually being traded.

    A committee's own research tools (yfinance/tushare/etc.) have zero
    visibility into the broker feed an order will actually execute against,
    and routinely reference a DIFFERENT instrument entirely (e.g. GC=F
    COMEX gold futures vs. MT5's XAUUSDm CFD) that can diverge by a lot —
    discovered when a committee's stop/target levels sat $60+ away from the
    live tradable price because its research price was for a different
    instrument. Injecting this as ground truth lets the prompt tell the
    committee explicitly which price its executable levels must anchor to.

    Returns:
        {"bid": float, "ask": float} or None if the quote can't be read
        (fails open on the prompt side — the agent still has its own
        trading_quote tool to fall back on).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_quote

    try:
        result = get_quote(symbol, connection)
    except Exception:
        return None
    quote = result.get("quote") or {}
    bid, ask = quote.get("bid"), quote.get("ask")
    if not bid or not ask:
        return None
    return {"bid": float(bid), "ask": float(ask)}


def _max_stop_distance(symbol: str, lots: float) -> float | None:
    """Max stop-loss distance (price units) that stays within MAX_LOSS_PER_ORDER_USD.

    ``MAX_LOSS_PER_ORDER_USD / (contract_size * lots)`` — the same formula the
    mandate gate itself uses (in reverse) to compute planned loss. Returns
    None if the contract size can't be read (fails open on the prompt side —
    the gate still enforces the real cap regardless of what the prompt says).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        size = mt5_sdk.contract_size(symbol)
    except Exception:
        return None
    if not size or size <= 0 or lots <= 0:
        return None
    return MAX_LOSS_PER_ORDER_USD / (size * lots)


def _symbol_position_summary(symbol: str, connection: str) -> dict:
    """Verified (not LLM-reported) count/side of OUR OWN open positions in `symbol`.

    Queried directly via the connector service before the prompt is even
    built, so the cap/direction check the agent is given is ground truth,
    not something it has to count correctly itself from a tool result.
    Filtered to OUR_MAGIC — on an account also running a separate signal-
    service EA (or manual trades) on the same symbol, counting every position
    regardless of source would let someone else's position silently eat our
    own same-direction cap (or worse, look like a direction conflict that
    blocks us from ever trading). The signal service's own activity is
    reported separately, as context, by _signal_service_activity.

    Returns:
        {"count": int, "side": "buy" | "sell" | "mixed" | None}. ``"mixed"``
        only happens if positions predating this rule already conflict.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_positions

    result = get_positions(connection)
    rows = [
        p for p in result.get("positions", [])
        if p.get("symbol") == symbol and p.get("magic") == OUR_MAGIC
    ]
    if not rows:
        return {"count": 0, "side": None}
    sides = {row.get("side") for row in rows}
    side = sides.pop() if len(sides) == 1 else "mixed"
    return {"count": len(rows), "side": side}


def _signal_service_activity(symbol: str, connection: str) -> str | None:
    """Summarize a separately-running signal-service EA's activity on `symbol`.

    Anything on this MT5 account whose `magic` isn't OUR_MAGIC is presumed to
    be the user's own separately-running signal service (or a manual trade).
    Returns a plain-text summary of its currently open position(s) plus its
    most recent closed deals (capped — see SIGNAL_ACTIVITY_MAX_LINES), for the
    committee prompt — context only, per the user's own framing: the
    committee weighs it alongside its own research, it does not mirror or
    fade it automatically. Returns None when there's nothing to report (keeps
    a quiet pass's prompt clean) or if the reads fail (fails open — this is
    supplementary context, not a safety check, so a read failure should never
    block the pass).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_open_orders, get_positions

    try:
        positions = get_positions(connection).get("positions", [])
    except Exception:
        positions = []
    open_lines = [
        f"  OPEN {p.get('side', '?').upper()} {p.get('volume')} lots @ {p.get('price_open')} "
        f"(current P&L {p.get('profit')}, comment={p.get('comment') or 'none'})"
        for p in positions
        if p.get("symbol") == symbol and p.get("magic") != OUR_MAGIC
    ][:SIGNAL_ACTIVITY_MAX_LINES]

    try:
        executions = get_open_orders(connection, include_executions=True).get("executions", [])
    except Exception:
        executions = []
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=SIGNAL_LOOKBACK_DAYS)).isoformat()
    candidates = [
        d for d in executions
        if d.get("symbol") == symbol and d.get("magic") != OUR_MAGIC and (d.get("time") or "") >= cutoff_iso
    ]
    candidates.sort(key=lambda d: d.get("time") or "", reverse=True)  # most recent first
    remaining = max(0, SIGNAL_ACTIVITY_MAX_LINES - len(open_lines))
    closed_lines = [
        f"  CLOSED {d.get('side', '?').upper()} {d.get('volume')} lots @ {d.get('price')}, "
        f"P&L {d.get('profit')} on {d.get('time')} (comment={d.get('comment') or 'none'})"
        for d in candidates[:remaining]
    ]

    lines = open_lines + closed_lines
    if not lines:
        return None
    return (
        f'A separate signal service/EA is also active on this account. Its activity in "{symbol}" '
        f"(informational only — weigh it as one more data point alongside your own research; do not "
        f"simply mirror or fade it; most recent {len(lines)} shown):\n" + "\n".join(lines)
    )


def _read_journal() -> list[dict]:
    try:
        return json.loads(TRADE_JOURNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _write_journal(entries: list[dict]) -> None:
    TRADE_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADE_JOURNAL_PATH.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def _extract_placed_order(run_id: str) -> dict | None:
    """Pull the verified trading_place_order RESULT (not the LLM's own report).

    Reads the trace's real tool_result record — the connector's own response
    (ticket/side/quantity/fill_price/stop_loss/take_profit) — rather than
    trusting the committee's free-text summary of what it did.

    Returns:
        The connector's place_order response dict on a successful placement,
        or None if this run didn't place one.
    """
    for e in _trace_entries(run_id):
        if e.get("type") != "tool_result" or e.get("tool") != "trading_place_order":
            continue
        raw = e.get("result")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and parsed.get("status") == "ok":
            return parsed
    return None


def _blocked_order_note(run_id: str) -> str | None:
    """Surface the REAL reason a trading_place_order attempt didn't go through.

    Complements _extract_placed_order: when the tool was called but its
    result wasn't status=="ok" (mandate/gate denial, MT5 rejection, bad
    args), this pulls the actual requested args plus the gate/error reason
    into the emailed report — including whatever quantity/order_type the
    committee actually requested, which may not match what it was told to
    use (regression: a hallucinated 1.0-lot order, 100x the intended 0.01,
    was denied by the mandate gate but was invisible in the report until this
    was added — the committee's own free-text summary doesn't reliably
    surface a code-verified reason).
    """
    entries = _trace_entries(run_id)
    call_args_by_id = {
        e.get("call_id"): e.get("args") or {}
        for e in entries
        if e.get("type") == "tool_call" and e.get("tool") == "trading_place_order"
    }
    for e in entries:
        if e.get("type") != "tool_result" or e.get("tool") != "trading_place_order":
            continue
        raw = e.get("result")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and parsed.get("status") == "ok":
            continue  # a real success is reported via _extract_placed_order instead
        args = call_args_by_id.get(e.get("call_id"), {})
        reason = (parsed.get("reason") or parsed.get("error") or "unknown") if isinstance(parsed, dict) else "unknown"
        size = f"{args.get('quantity')} lots" if args.get("quantity") is not None else f"notional {args.get('notional')}"
        return (
            f"\n\n[AUTOMATED CHECK] A trading_place_order call did NOT go through: requested "
            f"{args.get('side')} {size} {args.get('symbol')} ({args.get('order_type', 'market')}) "
            f"— reason: {reason}"
        )
    return None


def _journal_record_open(symbol: str, connection: str, order: dict) -> None:
    """Append a new open-trade record from a verified place_order result."""
    entries = _read_journal()
    entries.append({
        "ticket": str(order.get("order_id") or ""),
        "symbol": symbol,
        "connection": connection,
        "side": order.get("side"),
        "lots": order.get("quantity"),
        "entry_price": order.get("fill_price"),
        "stop_loss": order.get("stop_loss"),
        "take_profit": order.get("take_profit"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    })
    _write_journal(entries)


def _classify_excursion(entry: dict, deal: dict) -> dict:
    """Compute max favorable/adverse excursion for a just-closed trade, from real price bars.

    Zero LLM cost — pure historical-bar analysis. Returns {} if bars can't be
    fetched or times can't be parsed (fails open; never blocks reconciliation
    over a missing analytics extra).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        opened = datetime.fromisoformat(entry["opened_at"])
        closed = datetime.fromisoformat(deal["time"])
        entry_price = float(entry["entry_price"])
        stop_loss = float(entry["stop_loss"])
        is_buy = entry["side"] == "buy"
    except (KeyError, TypeError, ValueError):
        return {}

    try:
        bars = mt5_sdk.get_historical_bars_range(
            entry["symbol"],
            opened - timedelta(minutes=15),
            closed + timedelta(minutes=15),
            period=EXCURSION_BAR_PERIOD,
        )["bars"]
    except Exception:
        return {}
    if not bars:
        return {}

    try:
        highs = [b["high"] for b in bars if b.get("high") is not None]
        lows = [b["low"] for b in bars if b.get("low") is not None]
        if not highs or not lows:
            return {}
        if is_buy:
            favorable = max(highs) - entry_price
            adverse = entry_price - min(lows)
        else:
            favorable = entry_price - min(lows)
            adverse = max(highs) - entry_price
    except (TypeError, ValueError):
        return {}

    stop_distance = abs(entry_price - stop_loss)
    outcome = entry.get("outcome")
    reversal = (
        stop_distance > 0
        and favorable >= REVERSAL_THRESHOLD_FRACTION * stop_distance
        and outcome in ("loss", "breakeven")
    )
    return {
        "max_favorable_pts": round(favorable, 3),
        "max_adverse_pts": round(adverse, 3),
        "excursion_tag": "reversal" if reversal else "clean",
    }


def _journal_reconcile_closed(symbol: str, connection: str) -> None:
    """Mark journal entries closed once their position is no longer open.

    Pure API reads — no LLM tokens spent. Matches by ticket against current
    open positions (still open -> left alone) and, failing that, against the
    real CLOSING deal (magic == OUR_MAGIC, position_id == our ticket, entry
    != 0) to record the real exit price/P&L — never trusts the committee's
    own report of what happened. Fails open on any read error (never
    corrupts the journal over a transient connector failure).

    Regression: MT5's closing deal carries `order == 0` (no usable back-
    reference to the opening order); the only reliable link is `position_id`.
    Matching on order_id/deal-ticket instead (as this originally did) grabbed
    the OPENING deal — which always reports profit=0.0 — and recorded a real
    +$29 take-profit win as a false "breakeven".
    """
    entries = _read_journal()
    open_entries = [e for e in entries if e.get("status") == "open" and e.get("symbol") == symbol]
    if not open_entries:
        return

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_open_orders, get_positions

    try:
        live_tickets = {str(p.get("ticket")) for p in get_positions(connection).get("positions", [])}
    except Exception:
        return

    try:
        executions = get_open_orders(connection, include_executions=True).get("executions", [])
    except Exception:
        executions = []
    our_closing_deals = [
        d for d in executions
        if d.get("magic") == OUR_MAGIC and d.get("entry") not in (0, None)
    ]
    # A position could in principle have more than one closing deal (partial
    # close); keep the latest one per position_id.
    latest_close_by_position: dict[str, dict] = {}
    for d in our_closing_deals:
        key = str(d.get("position_id") or "")
        if not key:
            continue
        existing = latest_close_by_position.get(key)
        if existing is None or (d.get("time") or "") >= (existing.get("time") or ""):
            latest_close_by_position[key] = d

    changed = False
    for entry in open_entries:
        ticket = str(entry.get("ticket"))
        if ticket in live_tickets:
            continue  # still open
        deal = latest_close_by_position.get(ticket)
        entry["status"] = "closed"
        if deal:
            # The real close time, from the broker's own deal record — NOT
            # "now" (when this reconciliation happened to run, up to 2 hours
            # after the actual close). Getting this right matters: the
            # excursion analysis below needs the real window to look at.
            entry["closed_at"] = deal.get("time") or datetime.now(timezone.utc).isoformat()
            profit = deal.get("profit")
            entry["exit_price"] = deal.get("price")
            entry["profit"] = profit
            entry["outcome"] = "win" if (profit or 0) > 0 else ("loss" if (profit or 0) < 0 else "breakeven")
            entry.update(_classify_excursion(entry, deal))
        else:
            entry["closed_at"] = datetime.now(timezone.utc).isoformat()
            entry["outcome"] = "unknown"  # closed, but couldn't match the closing deal
        changed = True

    if changed:
        _write_journal(entries)


def _journal_summary_text(symbol: str) -> str | None:
    """One terse line of our own recent track record on `symbol`, or None if empty.

    Deliberately minimal (a single stats line, not a replay of past reasoning)
    — this was an explicit "minimum added tokens" requirement.
    """
    closed = [e for e in _read_journal() if e.get("symbol") == symbol and e.get("status") == "closed"]
    if not closed:
        return None
    recent = closed[-JOURNAL_SUMMARY_WINDOW:]
    scored = [e for e in recent if e.get("outcome") in ("win", "loss")]
    wins = sum(1 for e in scored if e["outcome"] == "win")
    losses = sum(1 for e in scored if e["outcome"] == "loss")
    net = sum(float(e.get("profit") or 0) for e in scored)
    last = recent[-1]
    reversals = sum(1 for e in scored if e.get("excursion_tag") == "reversal")
    # Only mentioned when it's actually happened — keeps a clean run's prompt
    # from growing for no reason (same "minimum tokens" discipline as the
    # rest of this block).
    reversal_note = (
        f" {reversals} of these moved favorably before reversing to a loss — "
        f"a stop-management issue, not an entry-quality one."
        if reversals else ""
    )
    return (
        f"Your own recent {symbol} track record (last {len(recent)} closed): {wins}W/{losses}L, "
        f"net {net:+.2f}. Last: {str(last.get('side', '?')).upper()} {last.get('outcome', '?')} "
        f"({last.get('profit', '?')}).{reversal_note}"
    )


def _read_live_baseline() -> dict:
    try:
        return json.loads(LIVE_BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_live_baseline(data: dict) -> None:
    LIVE_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_BASELINE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _live_circuit_breaker_check(trade: dict) -> str | None:
    """Auto-halt + flatten live trading if equity drops too far today.

    Returns a message to fold into the emailed report (and to signal the
    caller to run this pass research-only, no trade attempt) when trading was
    skipped this pass, or ``None`` when normal live trading may proceed. A
    no-op for demo connections (not in LIVE_CONNECTIONS).
    """
    connection = trade["connection"]
    if connection not in LIVE_CONNECTIONS:
        return None

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set, trip_halt
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.profiles import profile_by_id
    from src.trading.service import get_account, get_positions

    broker = "mt5"  # every mt5-live-* profile shares this mandate/halt broker key

    if halt_flag_set(broker):
        return (
            f"[LIVE CIRCUIT BREAKER] {broker} live trading is currently HALTED "
            f"(kill switch already tripped) — no live order was attempted this pass."
        )

    try:
        account = get_account(connection)
        equity = float(account["account"]["equity"])
    except Exception as exc:
        return f"[LIVE CIRCUIT BREAKER] could not read live account equity this pass ({exc}) — skipping live trading as a precaution."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    baseline = _read_live_baseline()
    if baseline.get("date") != today:
        baseline = {"date": today, "equity": equity}
        _write_live_baseline(baseline)
    baseline_equity = float(baseline.get("equity") or equity)

    if baseline_equity <= 0:
        return None
    drawdown = (baseline_equity - equity) / baseline_equity
    if drawdown < LIVE_DRAWDOWN_HALT_PCT:
        return None

    reason = f"daily equity drawdown {drawdown:.0%} (baseline ${baseline_equity:.2f} -> ${equity:.2f})"
    trip_halt(by="cli", reason=reason, broker=broker)

    closed = []
    try:
        profile = profile_by_id(connection)
        config = mt5_sdk.build_config(profile.config, {})
        positions = get_positions(connection).get("positions", [])
        for pos in positions:
            if pos.get("symbol") == trade["symbol"]:
                result = mt5_sdk.close_position(config, ticket=pos.get("ticket"))
                closed.append(f"ticket {pos.get('ticket')}: {result.get('status')}")
    except Exception as exc:
        closed.append(f"flatten attempt raised: {exc}")

    return (
        f"[LIVE CIRCUIT BREAKER TRIPPED] {reason}. Live trading for {broker} is now HALTED "
        f"(kill switch) until manually cleared (src.live.halt.clear_halt). "
        f"Flatten attempt: {'; '.join(closed) if closed else 'no open position found'}."
    )


@dataclass
class CommitteeResult:
    committee: str
    target: str
    market: str
    status: str  # "success" | "error" | "timeout"
    run_id: str | None
    report_text: str
    traded: bool = False
    error: str | None = None


# --------------------------------------------------------------------------- #
# Running a committee
# --------------------------------------------------------------------------- #


_REPORT_FORMAT_NO_TRADE = (
    "Finally, report in exactly this structure (plain text, these labels verbatim):\n"
    "Decision: <long / short / wait, one sentence>\n"
    "Reasoning: <the concrete factors behind the call - technicals, fundamentals, risk/sizing, "
    "whatever the committee actually weighed - 3-6 sentences, specific numbers where the debate gave them>\n"
    "Targets & Stops: <PM's price levels, or 'none given' if the call was wait/hold>\n"
    "Confidence: <the PM's stated confidence, or 'not stated'>"
)

_REPORT_FORMAT_TRADE = (
    "Finally, report in exactly this structure (plain text, these labels verbatim):\n"
    "Decision: <long / short / wait, one sentence>\n"
    "Reasoning: <the concrete factors behind the call - technicals, fundamentals, risk/sizing, "
    "whatever the committee actually weighed - 3-6 sentences, specific numbers where the debate gave them>\n"
    "Order: <if placed: symbol, side, lots, the resulting fill/price, and the exact stop_loss/take_profit "
    "levels attached to it (from trading_place_order's response, not just what the PM said - confirm they "
    "actually landed on the order); if not placed: the specific reason (decision was wait/hold, a position "
    "already existed, the PM gave no stop level, or the order was rejected - state which)>"
)


_DAY_TRADE_FRAMING = (
    "IMPORTANT — this is a SHORT-TERM, same-day/next-session trade decision, not a multi-week "
    "swing or position trade (the committee's default style is a buy-side fund position-trade "
    "horizon of weeks to months — explicitly override that here). Instruct the committee: weight "
    "intraday/short-term technicals (recent price action, short-period moving averages like "
    "MA5/MA20, intraday support/resistance, momentum on shorter timeframes) over multi-week "
    "structural setups, and size the stop-loss and take-profit for a trade meant to resolve within "
    "the current or next trading session — not levels sized on the assumption the position could "
    "run for weeks. A stop/target several percent away from entry is a swing-trade sizing mistake "
    "here; a day-trade's levels should be a small fraction of that distance.\n\n"
)


def _build_prompt(committee: str, target: str, market: str, trade: dict | None) -> str:
    if not trade:
        return (
            f'Run the {committee} swarm with target="{target}" and market="{market}" '
            f"to produce its full debate and final decision.\n\n"
            f"{_DAY_TRADE_FRAMING}"
            f"{_REPORT_FORMAT_NO_TRADE}"
        )
    symbol = trade["symbol"]
    connection = trade["connection"]
    lots = trade["lots"]
    max_stack = trade.get("max_stack", MAX_SAME_DIRECTION_POSITIONS)

    summary = _symbol_position_summary(symbol, connection)
    count, side = summary["count"], summary["side"]
    if count == 0:
        position_fact = f'Verified via the platform: there are currently NO open positions in "{symbol}".'
    elif side == "mixed":
        position_fact = (
            f'Verified via the platform: there are {count} open positions in "{symbol}" in CONFLICTING '
            f"directions (both long and short already open) — treat this as the cap being reached "
            f"regardless of the new decision's direction; do not add to either side."
        )
    else:
        position_fact = (
            f'Verified via the platform: there are currently {count} open {side.upper()} position(s) in '
            f'"{symbol}" (cap: {max_stack} same-direction positions at once).'
        )

    quote = _symbol_live_quote(symbol, connection)
    if quote:
        quote_fact = (
            f'Verified LIVE price on the {connection} broker feed for "{symbol}" right now: '
            f'bid {quote["bid"]}, ask {quote["ask"]}. This — NOT any price your own research tools '
            f'return for a "similar" instrument (e.g. futures/index proxies like GC=F, ^GSPC, or '
            f'yfinance tickers, which can and do diverge from what this broker actually quotes) — is '
            f"the price your stop-loss and take-profit levels must be anchored to. Use your research "
            f"data for direction/technicals/timing, but re-derive the actual stop/target PRICES relative "
            f"to this live quote, not the research price."
        )
    else:
        quote_fact = (
            f'Could not read a live quote for "{symbol}" from the {connection} broker feed just now — '
            f"call trading_quote yourself for this symbol before finalizing any stop-loss/take-profit "
            f"level, and anchor to that, not to a research-data price."
        )

    max_distance = _max_stop_distance(symbol, lots)
    if max_distance is not None:
        risk_fact = (
            f"HARD RISK LIMIT: at {lots} lots, this account's mandate caps worst-case planned loss at "
            f"${MAX_LOSS_PER_ORDER_USD:.2f} — your stop-loss must be within {max_distance:.3f} price units "
            f'of entry (whichever side is the losing side for "{symbol}"). This is enforced by the broker '
            f"gate regardless of what you propose: a wider stop is denied outright, not trimmed for you. "
            f"Size the stop AT or INSIDE this distance — do not propose a wider one and rely on it being "
            f"rejected; a tighter, valid stop that actually executes is strictly better than a wider one "
            f"that gets denied."
        )
    else:
        risk_fact = (
            f"Could not compute the exact price-distance budget for this account's ${MAX_LOSS_PER_ORDER_USD:.2f} "
            f"max-loss-per-order cap — size the stop conservatively; a wide stop risks outright denial by the "
            f"broker gate regardless of what you propose."
        )

    signal_fact = _signal_service_activity(symbol, connection)
    signal_block = f"{signal_fact}\n\n" if signal_fact else ""

    _journal_reconcile_closed(symbol, connection)
    journal_fact = _journal_summary_text(symbol)
    journal_block = f"{journal_fact}\n\n" if journal_fact else ""

    return (
        f'Run the {committee} swarm with target="{target}" ({market}) to produce its full debate '
        f"and final decision, including concrete stop-loss and take-profit price levels — every "
        f"committee decision must carry these, not just a direction.\n\n"
        f"{quote_fact}\n\n"
        f"{risk_fact}\n\n"
        f"{signal_block}"
        f"{journal_block}"
        f"{_DAY_TRADE_FRAMING}"
        f"Then, based ONLY on the portfolio manager's final decision:\n"
        + (
            "- Consider the signal-service activity above as one more input to the debate — a second "
            "opinion, not a directive to mirror or fade.\n"
            if signal_fact else ""
        )
        + f"- {position_fact} Still call trading_positions yourself too (for your own report, and as a "
        f"last-moment freshness check) — but use the verified fact above, not just your own count, to "
        f"decide whether to trade.\n\n"
        f"If (and only if) all of the following hold — decision is long or short (not wait/hold); the "
        f'PM gave a specific stop-loss level; there is no open position in "{symbol}" in the OPPOSITE '
        f"direction; and fewer than {max_stack} same-direction positions are already open — call "
        f"trading_place_order with EXACTLY these arguments, substituting ONLY <SIDE>/<STOP>/<TARGET> "
        f"from the PM's decision. Every other field below is fixed — this account trades one fixed-size "
        f"market clip per signal, never the debate's own tranche/limit/scaled-entry plan, regardless of "
        f"how the PM phrased the entry:\n\n"
        f"  trading_place_order(\n"
        f'      symbol="{symbol}",\n'
        f'      connection="{connection}",\n'
        f"      side=<'buy' if the decision is long, 'sell' if short>,\n"
        f"      quantity={lots},\n"
        f'      order_type="market",\n'
        f"      stop_loss=<PM's stop price, adjusted to the live quote above AND to the HARD RISK LIMIT "
        f"distance if needed — tighten it inward, never widen it>,\n"
        f"      take_profit=<PM's nearest target price, same adjustment>,\n"
        f'      time_in_force="day",\n'
        f"  )\n\n"
        f"Do not call trading_place_order at all if any condition above fails — that includes a "
        f"wait/hold decision, a missing stop-loss, an opposite-direction position already open, or the "
        f"cap already reached. There is no partial/scaled/limit-order version of this call: either place "
        f"exactly the order above, or place nothing.\n\n"
        f"{_REPORT_FORMAT_TRADE}"
    )


def run_committee(committee: str, target: str, market: str, trade: dict | None = None) -> CommitteeResult:
    """Run one committee preset against one target as an isolated subprocess.

    When `trade` is given ({"symbol", "connection", "lots"}), the agent also
    checks positions and conditionally places a demo order on the PM's
    decision. Omit it for research-only, no-trading runs.
    """
    breaker_note = None
    if trade:
        breaker_note = _live_circuit_breaker_check(trade)
        if breaker_note:
            logger.warning("live circuit breaker for %s: %s", target, breaker_note)
            trade = None  # fall back to a research-only run this pass

    prompt = _build_prompt(committee, target, market, trade)
    logger.info("running %s on %s (%s)%s", committee, target, market, " [trade-enabled]" if trade else "")
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "cli", "run",
                "--prompt", prompt,
                "--json",
                "--max-iter", str(MAX_ITER),
            ],
            cwd=str(AGENT_DIR),
            capture_output=True,
            text=True,
            # Committee reports routinely contain em-dashes/CJK text; without an
            # explicit encoding, subprocess.run falls back to the Windows locale
            # (cp1252 here), which can't decode that output and crashes the
            # reader thread mid-read, leaving proc.stdout as None.
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s on %s timed out after %ss", committee, target, RUN_TIMEOUT_SECONDS)
        return CommitteeResult(committee, target, market, "timeout", None, "", error="run exceeded timeout")

    payload = _last_json_line(proc.stdout)
    if payload is None:
        return CommitteeResult(
            committee, target, market, "error", None, "",
            error=f"could not parse CLI output (exit {proc.returncode}): {proc.stderr[-2000:]}",
        )

    status = payload.get("status", "unknown")
    run_id = payload.get("run_id")
    if status != "success":
        return CommitteeResult(
            committee, target, market, "error", run_id, "",
            error=payload.get("reason") or f"run status was '{status}'",
        )

    report_text = _read_final_answer(run_id) if run_id else ""
    if not report_text:
        return CommitteeResult(committee, target, market, "error", run_id, "", error="run succeeded but produced no final answer")

    # A trading_place_order CALL happening is not the same as an order being
    # PLACED — a call the mandate gate blocks (wrong size, breach, halted,
    # etc.) still shows up as a tool_call. Regression: a hallucinated 1.0-lot
    # order (100x the intended 0.01) was correctly denied by the gate
    # ($465,893 notional vs. the $6,000 cap) but still got tagged "TRADED" in
    # the email under the old tool_call-only check. `_extract_placed_order`
    # already verifies status=="ok" against the connector's own response, so
    # basing `traded` on it directly is both the fix and the single source of
    # truth for what actually reached the broker.
    placed_order = _extract_placed_order(run_id) if trade else None
    traded = bool(placed_order)
    if traded:
        report_text = report_text + _post_trade_cap_check(trade)
        _journal_record_open(trade["symbol"], trade["connection"], placed_order)
    elif trade:
        blocked_note = _blocked_order_note(run_id)
        if blocked_note:
            report_text = report_text + blocked_note
    if breaker_note:
        report_text = report_text + f"\n\n{breaker_note}"
    return CommitteeResult(committee, target, market, "success", run_id, report_text, traded=traded)


def _post_trade_cap_check(trade: dict) -> str:
    """Defense-in-depth: re-verify the position cap AFTER a trade, in code.

    The prompt gives the agent a code-verified position fact and an explicit
    cap rule, but nothing forces it to obey either — this re-checks the
    platform's actual resulting state and surfaces a clear warning in the
    email if it was violated, rather than silently trusting the agent did
    the arithmetic right.
    """
    max_stack = trade.get("max_stack", MAX_SAME_DIRECTION_POSITIONS)
    summary = _symbol_position_summary(trade["symbol"], trade["connection"])
    if summary["side"] == "mixed":
        return (
            f"\n\n[AUTOMATED CHECK] {trade['symbol']} now has open positions in BOTH directions "
            f"({summary['count']} total) — the agent placed a trade despite an existing opposite-"
            f"direction position. This should not happen; check manually."
        )
    if summary["count"] > max_stack:
        return (
            f"\n\n[AUTOMATED CHECK] {trade['symbol']} now has {summary['count']} open "
            f"{summary['side']} positions, exceeding the cap of {max_stack}. "
            f"The agent's own count was wrong somewhere; check manually."
        )
    return ""


def _last_json_line(stdout: str | None) -> dict | None:
    """`vibe-trading run --json` prints one JSON object as its last line."""
    if not stdout:
        return None
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _trace_entries(run_id: str) -> list[dict]:
    sys.path.insert(0, str(AGENT_DIR))
    from src.agent.trace import TraceWriter

    trace_dir = TraceWriter.find_trace_dir(run_id)
    if trace_dir is None:
        return []
    return TraceWriter.read(trace_dir, resolve_offloads=True, resolve_fields={"content"})


def _read_final_answer(run_id: str) -> str:
    """Pull the agent's final answer text out of the run's trace.jsonl."""
    answers = [e["content"] for e in _trace_entries(run_id) if e.get("type") == "answer" and e.get("content")]
    return answers[-1] if answers else ""


# --------------------------------------------------------------------------- #
# Deciding what's worth an email — customize this
# --------------------------------------------------------------------------- #


def is_reportable(result: CommitteeResult) -> bool:
    """Return whether `result` is worth emailing.

    Default: report every successful run (a finished committee debate is
    inherently worth reading) plus every error/timeout (so unattended
    failures are never silent). Tighten this if you only want emails on
    actionable (long/short) calls, e.g.:

        if result.status != "success":
            return True
        text = result.report_text.lower()
        return "wait" not in text and "hold" not in text
    """
    return True


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


def send_email(subject: str, body: str, html_body: str | None = None) -> None:
    """Send the report. When html_body is given, sends multipart/alternative
    (HTML + the plain-text body as fallback) — every mail client that can
    render HTML gets the formatted version; anything that can't (or a user
    who prefers plain text) still gets the exact same content as before."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("EMAIL_FROM", user)
    recipient = os.environ["EMAIL_TO"]
    cc = [addr.strip() for addr in os.environ.get("EMAIL_CC", "").split(",") if addr.strip()]

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(sender, [recipient, *cc], msg.as_string())
    logger.info("emailed report: %s (cc: %s)", subject, ", ".join(cc) or "none")


def _format_body(result: CommitteeResult) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if result.status == "success":
        return (
            f"Committee: {result.committee}\n"
            f"Target: {result.target} ({result.market})\n"
            f"Run: {when}  (run_id={result.run_id})\n"
            f"{'-' * 60}\n\n"
            f"{result.report_text}\n"
        )
    return (
        f"Committee: {result.committee}\n"
        f"Target: {result.target} ({result.market})\n"
        f"Run: {when}  (run_id={result.run_id})\n"
        f"STATUS: {result.status.upper()}\n\n"
        f"{result.error}\n"
    )


# --------------------------------------------------------------------------- #
# HTML email rendering — lightweight, self-contained (no markdown/markdown2
# dependency; committee reports only ever use bold/tables/rules/bullets, so a
# narrow targeted converter covers everything real output actually needs).
# All raw text is HTML-escaped BEFORE any tag is inserted — committee/LLM
# output is untrusted content, never allowed to inject markup into the email.
# --------------------------------------------------------------------------- #

_TAG_COLORS = {"TRADED": "#2e7d32", "OK": "#555555", "ERROR": "#c62828", "TIMEOUT": "#c62828", "CRASHED": "#c62828"}
_EMAIL_FONT = "font-family:Arial,Helvetica,sans-serif;"


def _esc(text: object) -> str:
    return html_module.escape(str(text), quote=False)


def _inline_markdown(escaped_text: str) -> str:
    """Bold + inline code — applied to text that is ALREADY html-escaped."""
    escaped_text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped_text)
    escaped_text = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped_text)
    return escaped_text


def _table_block_to_html(lines: list[str]) -> str:
    """Convert a block of '| a | b |' markdown lines (with an optional
    '|---|---|' separator row) into a real HTML table."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{1,}:?", c) for c in cells):
            continue  # the |---|---| separator row itself
        rows.append(cells)
    if not rows:
        return ""
    head, *body = rows
    out = ['<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:13px;">', "<tr>"]
    out += [f'<th style="text-align:left;border-bottom:2px solid #444;padding:4px 8px;">{_inline_markdown(_esc(c))}</th>' for c in head]
    out.append("</tr>")
    for row in body:
        out.append("<tr>")
        out += [f'<td style="border-bottom:1px solid #ddd;padding:4px 8px;">{_inline_markdown(_esc(c))}</td>' for c in row]
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _markdown_to_html(text: str) -> str:
    """Narrow markdown->HTML for committee report text: bold, `code`, pipe
    tables, horizontal rules, bullet lists, paragraphs. Not a general
    markdown parser — deliberately scoped to what these reports actually
    produce, to avoid taking on a new dependency for it."""
    blocks = re.split(r"\n\s*\n", text.strip())
    parts = []
    for block in blocks:
        lines = [l for l in block.strip("\n").split("\n")]
        stripped = [l.strip() for l in lines if l.strip()]
        if not stripped:
            continue
        if all(l.startswith("|") for l in stripped):
            parts.append(_table_block_to_html(lines))
        elif len(stripped) == 1 and re.fullmatch(r"-{3,}|_{3,}", stripped[0]):
            parts.append('<hr style="border:none;border-top:1px solid #ccc;margin:12px 0;">')
        elif all(l.startswith(("- ", "* ")) for l in stripped):
            items = "".join(f"<li>{_inline_markdown(_esc(l[2:]))}</li>" for l in stripped)
            parts.append(f'<ul style="margin:4px 0;padding-left:20px;">{items}</ul>')
        else:
            paragraph = "<br>".join(_inline_markdown(_esc(l)) for l in lines)
            parts.append(f'<p style="margin:8px 0;line-height:1.4;">{paragraph}</p>')
    return "\n".join(parts)


def _status_report_html() -> str:
    """HTML rendering of the same data _build_status_report() presents as
    text — same underlying live reads, formatted as badges/tables instead of
    plain lines. Fails open per-section, same as the text version."""
    running, pid = _status_lock_state()
    loop_color = "#2e7d32" if running else "#c62828"
    loop_text = f"RUNNING (pid {pid})" if running else "NOT RUNNING" + (f" (stale lock, pid {pid})" if pid else "")
    parts = [
        f'<div style="{_EMAIL_FONT}font-size:14px;color:#222;">',
        '<h2 style="margin:0 0 8px;font-size:16px;">Vibe-Trading Status</h2>',
        f'<p style="margin:2px 0;"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background:{loop_color};margin-right:6px;"></span>Loop: <strong>{_esc(loop_text)}</strong></p>',
    ]

    log = _status_log_summary()
    if log.get("last_result_ts"):
        tag = log["last_result_tag"]
        tag_color = _TAG_COLORS.get(tag, "#555555")
        parts.append(
            f'<p style="margin:2px 0;">Last pass: {_esc(log["last_result_ts"])} &rarr; {_esc(log["last_result_desc"])} '
            f'(<span style="color:{tag_color};font-weight:bold;">{_esc(tag)}</span>)</p>'
        )
    elif log.get("last_start_ts"):
        parts.append(f'<p style="margin:2px 0;">Last pass started: {_esc(log["last_start_ts"])} (in progress)</p>')
    else:
        parts.append('<p style="margin:2px 0;color:#555;">Last pass: no reporter.log data found</p>')
    if log.get("next_due"):
        parts.append(f'<p style="margin:2px 0;">Next pass due: ~{_esc(log["next_due"])} (interval {log.get("interval")}s)</p>')

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set
    from src.trading.service import get_account, get_positions

    seen_connections: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] in seen_connections:
            continue
        seen_connections.add(trade["connection"])
        connection = trade["connection"]
        parts.append(f'<h3 style="margin:16px 0 4px;font-size:14px;">Account: {_esc(connection)}</h3>')
        try:
            account = get_account(connection)["account"]
        except Exception as exc:
            parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read account: {_esc(exc)}</p>')
            continue
        parts.append(
            '<table style="border-collapse:collapse;font-size:13px;">'
            f'<tr><td style="padding:2px 12px 2px 0;color:#555;">Balance</td><td><strong>{_esc(account.get("balance"))}</strong></td></tr>'
            f'<tr><td style="padding:2px 12px 2px 0;color:#555;">Equity</td><td><strong>{_esc(account.get("equity"))}</strong></td></tr>'
            "</table>"
        )
        try:
            positions = get_positions(connection).get("positions", [])
        except Exception as exc:
            parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read positions: {_esc(exc)}</p>')
            positions = []
        if not positions:
            parts.append('<p style="margin:2px 0;color:#555;">Open positions: none</p>')
        else:
            rows = []
            for p in positions:
                profit = p.get("profit") or 0
                pnl_color = "#2e7d32" if profit >= 0 else "#c62828"
                rows.append(
                    "<tr>"
                    f'<td style="padding:2px 8px;">{_esc(p.get("side", "?")).upper()}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("volume"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("symbol"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("price_open"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("sl", "?"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("tp", "?"))}</td>'
                    f'<td style="padding:2px 8px;color:{pnl_color};font-weight:bold;">{_esc(profit)}</td>'
                    "</tr>"
                )
            parts.append(
                '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
                '<tr style="color:#555;"><th style="text-align:left;padding:2px 8px;">Side</th>'
                '<th style="text-align:left;padding:2px 8px;">Vol</th><th style="text-align:left;padding:2px 8px;">Symbol</th>'
                '<th style="text-align:left;padding:2px 8px;">Open</th><th style="text-align:left;padding:2px 8px;">SL</th>'
                '<th style="text-align:left;padding:2px 8px;">TP</th><th style="text-align:left;padding:2px 8px;">P&amp;L</th></tr>'
                + "".join(rows) + "</table>"
            )
        if connection in LIVE_CONNECTIONS:
            try:
                halted = halt_flag_set("mt5")
                color = "#c62828" if halted else "#2e7d32"
                parts.append(f'<p style="margin:4px 0;">Kill switch: <span style="color:{color};font-weight:bold;">'
                              f'{"TRIPPED" if halted else "clear"}</span></p>')
            except Exception as exc:
                parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read kill switch state: {_esc(exc)}</p>')

    seen_symbols: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["symbol"] in seen_symbols:
            continue
        seen_symbols.add(trade["symbol"])
        try:
            _journal_reconcile_closed(trade["symbol"], trade["connection"])
            summary = _journal_summary_text(trade["symbol"])
        except Exception as exc:
            summary = f"could not read journal: {exc}"
        parts.append(f'<h3 style="margin:16px 0 4px;font-size:14px;">Track record: {_esc(trade["symbol"])}</h3>')
        parts.append(f'<p style="margin:2px 0;">{_esc(summary or "no closed trades yet")}</p>')

    parts.append("</div>")
    return "\n".join(parts)


def _format_body_html(result: CommitteeResult, tag: str) -> str:
    """HTML counterpart to _format_body — same content, real tables/bold/color."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    color = _TAG_COLORS.get(tag, "#555555")
    header = (
        f'<div style="{_EMAIL_FONT}font-size:14px;color:#222;">'
        f'<h2 style="margin:0 0 4px;font-size:16px;">{_esc(result.committee)} &mdash; {_esc(result.target)} '
        f'<span style="color:{color};">({_esc(tag)})</span></h2>'
        f'<p style="color:#555;margin:0 0 12px;font-size:13px;">{_esc(result.market)} &middot; {_esc(when)} &middot; '
        f'run_id={_esc(result.run_id or "?")}</p>'
        '<hr style="border:none;border-top:1px solid #ccc;margin:8px 0 16px;">'
    )
    if result.status == "success":
        body = _markdown_to_html(result.report_text)
    else:
        body = f'<p style="color:#c62828;">{_esc(result.error)}</p>'
    return header + body + "</div>"


def _wrap_email_html(inner: str) -> str:
    """Minimal full HTML document wrapper for maximum email-client compatibility."""
    return (
        "<html><body style=\"margin:0;padding:16px;background:#ffffff;\">"
        f'<div style="max-width:640px;margin:0 auto;">{inner}</div>'
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Driving loop
# --------------------------------------------------------------------------- #


def _check_silver_milestone() -> None:
    """Send a dedicated reminder once live equity crosses MILESTONE_SILVER_EQUITY_USD.

    A dedicated email (not a line buried in a committee report) so it's hard
    to miss. Fails open on any read error — a reminder is never worth
    crashing the loop over. Repeats every pass once crossed, until silver is
    actually added to TARGETS (see the module constant's docstring) — better
    to nag than to fire once and have the one email get missed.
    """
    if any("XAG" in str((t.get("trade") or {}).get("symbol", "")) for t in TARGETS):
        return  # already added — nothing left to remind about

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_account

    try:
        equity = float(get_account("mt5-live-trade")["account"]["equity"])
    except Exception:
        return
    if equity < MILESTONE_SILVER_EQUITY_USD:
        return

    try:
        text = (
            f"Live account equity is ${equity:.2f} (>= ${MILESTONE_SILVER_EQUITY_USD:.0f}). "
            f"You asked to be reminded at this point to add silver (XAGUSDm) to the live "
            f"portfolio. Tell Claude when you're ready to add it."
        )
        html = _wrap_email_html(
            f'<div style="{_EMAIL_FONT}font-size:14px;color:#222;">'
            f'<h2 style="margin:0 0 8px;font-size:16px;color:#f9a825;">Milestone: live account &ge; $100</h2>'
            f'<p style="margin:8px 0;line-height:1.4;">{_esc(text)}</p></div>'
        )
        send_email("[Vibe-Trading] MILESTONE: live account >= $100", text, html_body=html)
    except Exception:
        logger.exception("failed to send silver milestone reminder email")


def _status_header() -> str:
    """Fresh status block for an email — never blocks or skips the email it's attached to."""
    try:
        return _build_status_report() + f"\n\n{'=' * 60}\n\n"
    except Exception:
        logger.exception("status report build failed; this email will omit it")
        return ""


def _status_header_html() -> str:
    """HTML counterpart to _status_header — same fail-open guarantee."""
    try:
        return _status_report_html() + '<hr style="border:none;border-top:2px solid #ccc;margin:16px 0;">'
    except Exception:
        logger.exception("HTML status report build failed; this email will omit it")
        return ""


def run_once() -> None:
    _check_silver_milestone()
    for spec in TARGETS:
        target = spec.get("target", "?")
        try:
            result = run_committee(**spec)
        except Exception:
            # A bug in run_committee (or anything it calls) must not take the
            # whole unattended loop down with it — one target's crash should
            # cost that one email, never every future pass.
            logger.exception("run_committee crashed for %s", target)
            try:
                crash_text = f"run_committee raised an unhandled exception for target {target}. Check reporter.err.log on the machine for the traceback."
                # Status is queried fresh here, AFTER this target's run —
                # reflects what actually just happened, not pre-pass state.
                crash_html = (
                    _status_header_html()
                    + f'<div style="{_EMAIL_FONT}font-size:14px;color:#c62828;">{_esc(crash_text)}</div>'
                )
                send_email(
                    f"[Vibe-Trading] {spec.get('committee', '?')} — {target} (CRASHED)",
                    _status_header() + crash_text,
                    html_body=_wrap_email_html(crash_html),
                )
            except Exception:
                logger.exception("also failed to send the crash notification email")
            continue

        if not is_reportable(result):
            logger.info("%s on %s: not reportable, skipping email", result.committee, result.target)
            continue
        tag = "TRADED" if result.traded else ("OK" if result.status == "success" else result.status.upper())
        subject = f"[Vibe-Trading] {result.committee} — {result.target} ({tag})"
        try:
            html = _wrap_email_html(_status_header_html() + _format_body_html(result, tag))
            send_email(subject, _status_header() + _format_body(result), html_body=html)
        except Exception:
            logger.exception("failed to send report email for %s", target)


# --------------------------------------------------------------------------- #
# Status — a single read-only command replacing the manual multi-step check
# (process/lock state, log tail, live account query, halt flag, journal
# reconciliation) that every "status" request during development required.
# --------------------------------------------------------------------------- #

_LOG_RUN_START_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO running (.+)$")
_LOG_EMAILED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO emailed report: \[Vibe-Trading\] (.+?) \((\w+)\)")
_LOG_INTERVAL_RE = re.compile(r"starting loop mode, interval=(\d+)s")


def _status_lock_state() -> tuple[bool, int | None]:
    """Return (is_running, pid) from the singleton lock — None pid if no lock file at all."""
    if not LOCK_PATH.exists():
        return False, None
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False, None
    return _pid_is_alive(pid), pid


def _status_log_summary() -> dict:
    """Parse the tail of reporter.log for the interval and the last pass's timing/outcome.

    Best-effort: returns an empty dict (never raises) if the log is missing
    or unreadable — --status must degrade gracefully, not crash, when e.g.
    the loop has never been started yet.
    """
    try:
        lines = REPORTER_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    interval = None
    last_start = None
    last_emailed = None
    for line in lines[-500:]:
        m = _LOG_INTERVAL_RE.search(line)
        if m:
            interval = int(m.group(1))
        m = _LOG_RUN_START_RE.match(line)
        if m:
            last_start = (m.group(1), m.group(2))
        m = _LOG_EMAILED_RE.match(line)
        if m:
            last_emailed = (m.group(1), m.group(2), m.group(3))

    result: dict = {"interval": interval}
    if last_start:
        result["last_start_ts"], result["last_start_desc"] = last_start
    if last_emailed:
        ts_str, desc, tag = last_emailed
        result["last_result_ts"] = ts_str
        result["last_result_desc"] = desc
        result["last_result_tag"] = tag
        if interval:
            try:
                completed = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                result["next_due"] = (completed + timedelta(seconds=interval)).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return result


def _build_status_report() -> str:
    """Build the consolidated status report as text — shared by --status and every email.

    Read-only; never raises (each section fails open with an inline error
    note so one bad read can't blank out the rest of the report, and can
    never block an actual report/crash email from going out).
    """
    lines: list[str] = ["=== Vibe-Trading committee_reporter status ==="]
    running, pid = _status_lock_state()
    lines.append(f"Loop: {'RUNNING (pid ' + str(pid) + ')' if running else 'NOT RUNNING' + (f' (stale lock, pid {pid})' if pid else '')}")

    log = _status_log_summary()
    if log.get("last_result_ts"):
        lines.append(f"Last pass: {log['last_result_ts']} -> {log['last_result_desc']} ({log['last_result_tag']})")
    elif log.get("last_start_ts"):
        lines.append(f"Last pass started: {log['last_start_ts']} (still in progress or result not yet logged)")
    else:
        lines.append("Last pass: no reporter.log data found")
    if log.get("next_due"):
        lines.append(f"Next pass due: ~{log['next_due']} (interval {log.get('interval')}s)")

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set
    from src.trading.service import get_account, get_positions

    seen_connections: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] in seen_connections:
            continue
        seen_connections.add(trade["connection"])
        connection = trade["connection"]
        lines.append(f"\nAccount ({connection}):")
        try:
            account = get_account(connection)["account"]
            lines.append(f"  Balance: {account.get('balance')}  Equity: {account.get('equity')}")
        except Exception as exc:
            lines.append(f"  could not read account: {exc}")
            continue
        try:
            positions = get_positions(connection).get("positions", [])
        except Exception as exc:
            lines.append(f"  could not read positions: {exc}")
            positions = []
        if not positions:
            lines.append("  Open positions: none")
        else:
            for p in positions:
                lines.append(
                    f"  OPEN {p.get('side', '?').upper()} {p.get('volume')} {p.get('symbol')} "
                    f"@ {p.get('price_open')} SL {p.get('sl', '?')} TP {p.get('tp', '?')} "
                    f"P&L {p.get('profit')}"
                )
        if connection in LIVE_CONNECTIONS:
            try:
                halted = halt_flag_set("mt5")
                lines.append(f"  Kill switch: {'TRIPPED' if halted else 'clear'}")
            except Exception as exc:
                lines.append(f"  could not read kill switch state: {exc}")

    seen_symbols: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["symbol"] in seen_symbols:
            continue
        seen_symbols.add(trade["symbol"])
        try:
            _journal_reconcile_closed(trade["symbol"], trade["connection"])
            summary = _journal_summary_text(trade["symbol"])
        except Exception as exc:
            summary = f"could not read journal: {exc}"
        lines.append(f"\nTrack record ({trade['symbol']}): {summary or 'no closed trades yet'}")

    return "\n".join(lines)


def print_status() -> None:
    """Print a single consolidated status report and exit — read-only, no lock required."""
    print(_build_status_report())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one pass over TARGETS, then exit (default)")
    parser.add_argument("--loop", action="store_true", help="Keep running, repeating every --interval seconds")
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between passes in --loop mode")
    parser.add_argument("--status", action="store_true", help="Print a consolidated status report and exit (read-only, no lock)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    if not _acquire_singleton_lock():
        logger.error(
            "another committee_reporter.py instance is already running (lock at %s) — refusing to "
            "start a second one to avoid racing live trading decisions. If you're certain no instance "
            "is actually running, delete the lock file and retry.",
            LOCK_PATH,
        )
        return 1

    # Load ~/.vibe-trading/.env (falling back to agent/.env, then $CWD/.env) —
    # the same resolution order the vibe-trading app itself uses — so SMTP_*
    # and EMAIL_* set there are picked up without the caller having to export
    # them into the shell first.
    sys.path.insert(0, str(AGENT_DIR))
    from src.providers.llm import _ensure_dotenv

    _ensure_dotenv()

    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        logger.error("missing required environment variables: %s", ", ".join(missing))
        return 1

    if args.loop:
        logger.info("starting loop mode, interval=%ss", args.interval)
        while True:
            try:
                run_once()
            except Exception:
                # Defense in depth on top of run_once()'s own per-target
                # try/except: nothing here should ever be able to kill an
                # unattended loop that nobody is watching in real time.
                logger.exception("run_once() crashed; continuing after the normal interval")
            time.sleep(args.interval)
    else:
        run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
