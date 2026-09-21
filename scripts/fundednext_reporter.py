"""Runs Vibe-Trading swarm committees on a schedule for the FundedNext challenge account.

This is the FundedNext sibling of ``committee_reporter.py`` (the Exness live
account's reporter). It is a SEPARATE, ADAPTED script rather than a shared one: the
Exness reporter has several pieces of module-global state (a singleton lock,
a trade journal path, a hardcoded "mt5" broker literal in its circuit
breaker) that would collide or silently misbehave if pointed at a second
account, so this file copies the reusable *patterns* — not the files — and
wires in FundedNext-specific pieces instead:

  - fundednext_state.py: challenge-progress tracking (start date, initial
    balance, trading-days-logged). Stellar 1-Step is single-phase (one 10%
    target, then the funded account) — no phase-2 concept.
  - fundednext_guardrails.py: three hard, code-enforced risk limits (same-
    server-day loss halt, life-of-challenge static drawdown halt, 1%-of-
    balance per-trade risk cap) — see that module's docstring for the open
    items (server-day timezone, swap-in-equity, static-floor-trails-up) that
    must be verified before relying on this at real money.
  - fundednext_news_calendar.py: a news-blackout pre-check (risk-avoidance,
    not a FundedNext compliance requirement — see that module's docstring).

Deliberately SIMPLER than the Exness reporter in one respect: no signal-
service-activity injection (this account has no other EA running on it, so
there's nothing to detect) and no trade-drought/cap-fit/silver-milestone/
LLM-balance alerting — those were live-account-specific operational alerts,
not part of this build's scope.

Deliberate design choice, not an oversight: weekend-flatten stays ON (this
account does NOT hold positions through the weekend) even though FundedNext
itself permits weekend holding. The self-imposed 4%/8% daily-loss/static-
drawdown margins here are much tighter than the Exness account's 50% circuit
breaker and cannot absorb a weekend gap — see fundednext_guardrails.py's
DAILY_LOSS_HALT_PCT/MAX_DRAWDOWN_HALT_PCT.

Usage:
    .venv\\Scripts\\python.exe scripts\\fundednext_reporter.py --once
    .venv\\Scripts\\python.exe scripts\\fundednext_reporter.py --loop --interval 7200
    .venv\\Scripts\\python.exe scripts\\fundednext_reporter.py --status

Required environment (same as committee_reporter.py):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
Optional: EMAIL_CC (comma-separated), FINNHUB_API_KEY (news blackout guard —
see fundednext_news_calendar.py; the guard is skipped, fail-open, without it)
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
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fundednext_guardrails as fn_guard  # noqa: E402
import fundednext_news_calendar as fn_news  # noqa: E402
import fundednext_state as fn_state  # noqa: E402

# See committee_reporter.py's own comment for why this is needed on Windows
# (stdout/stderr default to the system ANSI codepage when redirected to a
# file, but --status reads reporter.log back as UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fundednext_reporter")

# --------------------------------------------------------------------------- #
# What to run each pass
# --------------------------------------------------------------------------- #

# VERIFIED live 2026-09-17 against the real FundedNext-Server 2 terminal
# (via src.trading.service.get_quote): this broker uses PLAIN symbol names,
# no "m" suffix — EURUSDm/AUDUSDm (Exness's own convention) return "-4:
# Terminal: Not found" here, but EURUSD/AUDUSD resolve fine with live
# quotes. max_stack=1 on both (no pyramiding) is a deliberately tighter
# posture than even the Exness account, appropriate for a challenge account
# where over-concentration risk matters more than upside.
#
# lots RAISED 2026-09-17 from the Exness-account-era 0.01 default (~$0.6-0.8
# actual risk/trade at these pairs' own ATR floors -- see the sizing
# analysis in the conversation this session) to target ~$20 risk/trade
# (~0.33% of the $6,000 balance, well under both the $60/trade mandate cap
# and FundedNext's own 1% guidance): lots = $20 / (contract_size *
# atr_stop_floor), using contract_size=100,000 and the live-verified floors
# (EURUSD 0.000815, AUDUSD 0.000610) at the time this was set. REQUIRES
# commit_fundednext_mandate.py's max_order_notional_usd/max_total_exposure_usd
# to be raised accordingly (done in the same change) -- the $3,000 notional
# cap that existed before this would otherwise deny orders at these sizes
# outright, independent of the loss-cap math.
#
# early_profit_trigger_usd RESCALED proportionally to the lots increase (24x
# / 33x) to preserve the same ARMING DISTANCE in price terms (~10 pips
# EURUSD, ~7.5 pips AUDUSD) that the original Exness-tuned $1.00/$0.75
# figures were sized for at 0.01 lots -- leaving the dollar figures
# unchanged would arm the early-profit trail almost immediately at the new
# size, undermining the reward:risk floor this session also added.
TARGETS: list[dict[str, object]] = [
    {
        "committee": "investment_committee", "target": "EURUSD", "market": "forex",
        "trade": {
            "symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.24, "max_stack": 1,
            "early_profit_trigger_usd": 24.00,
        },
    },
    {
        "committee": "investment_committee", "target": "AUDUSD", "market": "forex",
        "trade": {
            "symbol": "AUDUSD", "connection": "mt5fn-live-trade", "lots": 0.33, "max_stack": 1,
            "early_profit_trigger_usd": 24.75,
        },
    },
    # PAUSED 2026-09-18 at the user's request, purely to cut LLM spend while
    # EURUSD/AUDUSD alone prove out today's fixes (terminal-routing,
    # reward:risk enforcement, spec-check, fill-price fallback) -- NOT
    # because anything about gold itself was a problem. No mandate change
    # needed to re-enable (asset_classes already includes "commodity") --
    # just uncomment this block.
    #
    # Added 2026-09-17 at the user's request, after live-verifying (via
    # a full symbols_get() scan of FundedNext-Server 2's 96-symbol
    # catalog) that XAUUSD/XAGUSD/XPTUSD are the only commodities this
    # broker offers, and comparing their $-cost-per-minimum-lot: gold
    # $11.12 (spread-floor-bound, not ATR -- this broker's live gold
    # spread was $1.39 at the time), silver $26.00, platinum $47.60
    # (already near the $60 mandate ceiling at the SMALLEST possible
    # lot, i.e. structurally expensive regardless of direction) --
    # skipped platinum and silver's inflexibility for gold's better
    # cost/sizing-headroom tradeoff.
    #
    # lots=0.02 targets ~$22 risk at the live-verified binding floor
    # (11.12 price units * 100 oz/lot contract size * 0.02 lots), in
    # the same ~$20 range as the EURUSD/AUDUSD sizing above.
    # early_profit_trigger_usd=$30 is ~1.35x that $22 floor value (same
    # "past ordinary noise, but reachable by a real move" ratio the
    # EURUSD/AUDUSD triggers above use).
    # {
    #     "committee": "investment_committee", "target": "XAUUSD", "market": "commodity/forex",
    #     "trade": {
    #         "symbol": "XAUUSD", "connection": "mt5fn-live-trade", "lots": 0.02, "max_stack": 1,
    #         "early_profit_trigger_usd": 30.00,
    #     },
    # },
]

MAX_ITER = 15
RUN_TIMEOUT_SECONDS = 3600

MAX_SAME_DIRECTION_POSITIONS = 1

LIVE_CONNECTIONS = {"mt5fn-live-trade"}

# Magic number distinct from the Exness reporter's OUR_MAGIC (20260000) —
# matches agent/src/trading/connectors/mt5/profiles.py's mt5fn-live-trade
# profile config.
OUR_MAGIC = 20260001

TRADE_JOURNAL_PATH = REPO_ROOT / "logs" / "fundednext_trade_journal.json"
JOURNAL_SUMMARY_WINDOW = 8
JOURNAL_RECONCILE_GRACE = timedelta(seconds=120)

# Minimum acceptable reward:risk ratio on a filled order, enforced in CODE
# (not just prompt guidance) -- added 2026-09-17 after real Exness trade
# data showed the live account's average loss ($2.02) running ~1.7x its
# average win ($1.17) despite a 67% win rate, a fragile foundation. See
# _post_trade_reward_risk_check's docstring for the correction logic.
MIN_REWARD_RISK_RATIO = 1.5

EXCURSION_BAR_PERIOD = "15m"
REVERSAL_THRESHOLD_FRACTION = 0.5

ATR_PERIOD = "15m"
ATR_LOOKBACK_BARS = 14
ATR_STOP_MULTIPLE = 1.5
MIN_STOP_TO_SPREAD_RATIO = 8.0

LOCK_PATH = REPO_ROOT / "logs" / "fundednext_reporter.lock"
REPORTER_LOG_PATH = REPO_ROOT / "logs" / "fundednext_reporter.log"

WEEKEND_CUTOFF_UTC_HOUR = 20
WEEKEND_STATE_PATH = REPO_ROOT / "logs" / "fundednext_weekend_state.json"

# Session-gated trading (2026-09-21) -- mirrored from committee_reporter.py's
# identical feature, see that file's own comment for the full rationale.
SESSION_BIAS_STATE_PATH = REPO_ROOT / "logs" / "fundednext_session_bias_state.json"

BREAKEVEN_POLL_SECONDS = 300
BREAKEVEN_TRIGGER_FRACTION = 0.5
BREAKEVEN_BUFFER_POINTS = 20
EARLY_PROFIT_TRIGGER_USD = 8.0
MAX_HOLD_HOURS = 40.0
TIME_DECAY_START_FRACTION = 0.75

# News-blackout window applied before building the prompt for a trade-enabled
# pass — see fundednext_news_calendar.py's module docstring for why this is
# risk-avoidance, not a FundedNext compliance requirement.
NEWS_BLACKOUT_WINDOW_MINUTES = 5

# Model override for THIS account's committee subprocess only -- passed as
# the child process's own environment, NOT written to agent/.env, so the
# Exness reporter's separate subprocess calls are completely unaffected
# (_ensure_dotenv()'s load_dotenv(override=False) means an already-set env
# var always wins over the .env file's value, which is what makes this
# override reliable).
#
# 2026-09-18: DeepSeek's account balance went negative (both bots moved off
# it entirely -- see agent/.env). Moved this override to OpenRouter/Claude
# too, matching the Exness reporter's shared agent/.env default, rather than
# leaving it pointed at an unusable provider. Kept an explicit override
# (instead of just deleting this and falling through to agent/.env) so this
# account's model choice stays independently controllable -- originally
# deepseek-v4-pro over flash for this account's tighter 3%/6% self-imposed
# error budget; same reasoning would apply again if/when the account's
# cost budget allows Sonnet over Haiku specifically for this account.
LLM_MODEL_OVERRIDE = {"LANGCHAIN_PROVIDER": "openrouter", "LANGCHAIN_MODEL_NAME": "anthropic/claude-haiku-4-5"}


def _kill_process_tree(pid: int) -> None:
    """Kill a process and every descendant it spawned (Windows-only, via taskkill /T).

    See committee_reporter.py's identical helper for the full rationale
    (`cli run` re-execs into a grandchild process; killing only the direct
    child leaves the pipe's write end open and communicate() hangs forever).
    """
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
    except Exception:
        logger.exception("failed to kill process tree for pid %s", pid)


def _pid_is_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _process_creation_time(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = ctypes.windll.kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        )
        if not ok:
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _read_lock_identity() -> tuple[int, int | None] | None:
    if not LOCK_PATH.exists():
        return None
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    pid_part, sep, created_part = raw.partition(":")
    try:
        pid = int(pid_part)
    except ValueError:
        return None
    if not sep:
        return pid, None
    try:
        return pid, int(created_part)
    except ValueError:
        return pid, None


def _lock_identity_is_alive(identity: tuple[int, int | None]) -> bool:
    pid, created = identity
    if created is None:
        return _pid_is_alive(pid)
    current = _process_creation_time(pid)
    return current is not None and current == created


def _acquire_singleton_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity = _read_lock_identity()
    if identity is not None and _lock_identity_is_alive(identity):
        return False
    my_pid = os.getpid()
    my_created = _process_creation_time(my_pid)
    created_token = "" if my_created is None else str(my_created)
    LOCK_PATH.write_text(f"{my_pid}:{created_token}", encoding="utf-8")
    return True


def _symbol_live_quote(symbol: str, connection: str) -> dict | None:
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


def _symbol_currencies(symbol: str) -> set[str]:
    """Best-effort base/quote currency codes for a forex symbol, e.g.
    'EURUSDm' -> {'EUR', 'USD'}. Empty set if the symbol doesn't look like a
    plain 6-letter forex pair (fails open — the news check just skips)."""
    letters = "".join(ch for ch in symbol if ch.isalpha()).upper()
    if len(letters) < 6:
        return set()
    return {letters[:3], letters[3:6]}


def _mt5_config_for(connection: str):
    """Resolve the actual MT5Config (incl. terminal_path) for a connection id.

    Real bug found 2026-09-17: several call sites below used to call
    mt5_sdk.contract_size()/get_historical_bars() with NO config at all,
    which defaults to load_config() (the GLOBAL ~/.vibe-trading/mt5.json,
    which doesn't even exist -- bare MT5Config() defaults, terminal_path="").
    With only one MT5 terminal ever running (the Exness setup this was
    originally built against), that accidentally worked -- there was only
    one terminal for the ambiguous "whatever's already attached" call to
    find. Now that mt5fn-live-trade runs a SEPARATE terminal alongside the
    Exness one, an unpathed call silently connects to the wrong terminal
    (or whichever one the process's MT5 handle happens to be attached to),
    returning None for a symbol name that only exists on the other broker.
    This silently zeroed out the ATR/volatility-floor guidance in the
    prompt ("size the stop-loss at or beyond 0.000 price units from entry")
    during the 2026-09-17 EURUSDm/0.25-lot incident -- likely a contributing
    factor, not just a coincidence. Every call needs the CORRECT profile's
    config explicitly, same as _symbol_live_quote/_flatten_positions
    already do via profile_by_id + build_config.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.profiles import profile_by_id

    profile = profile_by_id(connection)
    return mt5_sdk.build_config(profile.config, {})


def _max_stop_distance(symbol: str, lots: float, budget_usd: float, connection: str) -> float | None:
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        config = _mt5_config_for(connection)
        size = mt5_sdk.contract_size(symbol, config=config)
    except Exception:
        return None
    if not size or size <= 0 or lots <= 0:
        return None
    return budget_usd / (size * lots)


def _atr_stop_floor(symbol: str, connection: str) -> float | None:
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        config = _mt5_config_for(connection)
        bars = mt5_sdk.get_historical_bars(symbol, config=config, period=ATR_PERIOD, limit=ATR_LOOKBACK_BARS + 1)["bars"]
    except Exception:
        return None

    bars = [b for b in bars if b.get("high") is not None and b.get("low") is not None and b.get("close") is not None]
    if len(bars) < 2:
        return None

    true_ranges = [
        max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"] - bars[i - 1]["close"]),
        )
        for i in range(1, len(bars))
    ]
    atr = sum(true_ranges) / len(true_ranges)
    return atr * ATR_STOP_MULTIPLE if atr > 0 else None


def _spread_stop_floor(quote: dict | None) -> float | None:
    if not quote:
        return None
    spread = quote.get("ask", 0) - quote.get("bid", 0)
    return spread * MIN_STOP_TO_SPREAD_RATIO if spread > 0 else None


def _symbol_position_summary(symbol: str, connection: str) -> dict:
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


def _read_journal() -> list[dict]:
    try:
        return json.loads(TRADE_JOURNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _write_journal(entries: list[dict]) -> None:
    TRADE_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADE_JOURNAL_PATH.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def _extract_placed_order(run_id: str) -> dict | None:
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
            continue
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
    """Append a new open-trade record, and mark today as a trading day for
    the minimum-trading-days rule (fundednext_state.record_trading_day,
    fn_state.MIN_TRADING_DAYS) — the one behavioral addition over
    committee_reporter.py's version."""
    entries = _read_journal()
    # Real incident 2026-09-18: order.get("fill_price") can be 0.0 (same MT5
    # propagation-gap bug _resolve_fill_price already works around for the
    # post-trade checks) -- a journal entry recorded with entry_price=0.0
    # can never be correctly classified win/loss on close (outcome ends up
    # "unknown" forever, entry_price wrong in every downstream report). Use
    # the same fallback here, at write time, instead of just patching the
    # symptom in _post_trade_reward_risk_check/_post_trade_spread_check.
    entries.append({
        "ticket": str(order.get("order_id") or ""),
        "symbol": symbol,
        "connection": connection,
        "side": order.get("side"),
        "lots": order.get("quantity"),
        "entry_price": _resolve_fill_price({"connection": connection}, order),
        "stop_loss": order.get("stop_loss"),
        "take_profit": order.get("take_profit"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    })
    _write_journal(entries)
    fn_state.record_trading_day()


def _classify_excursion(entry: dict, deal: dict) -> dict:
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
        config = _mt5_config_for(entry["connection"])
        bars = mt5_sdk.get_historical_bars_range(
            entry["symbol"],
            opened - timedelta(minutes=15),
            closed + timedelta(minutes=15),
            config=config,
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
        orders_resp = get_open_orders(connection, include_executions=True)
    except Exception:
        orders_resp = {}
    live_tickets |= {str(o.get("order_id")) for o in orders_resp.get("open_orders", [])}
    executions = orders_resp.get("executions", [])
    our_closing_deals = [
        d for d in executions
        if d.get("magic") == OUR_MAGIC and d.get("entry") not in (0, None)
    ]
    latest_close_by_position: dict[str, dict] = {}
    for d in our_closing_deals:
        key = str(d.get("position_id") or "")
        if not key:
            continue
        existing = latest_close_by_position.get(key)
        if existing is None or (d.get("time") or "") >= (existing.get("time") or ""):
            latest_close_by_position[key] = d

    now = datetime.now(timezone.utc)
    changed = False
    for entry in open_entries:
        ticket = str(entry.get("ticket"))
        if ticket in live_tickets:
            continue
        try:
            opened_at = datetime.fromisoformat(entry["opened_at"])
        except (KeyError, TypeError, ValueError):
            opened_at = None
        if opened_at is not None and (now - opened_at) < JOURNAL_RECONCILE_GRACE:
            continue
        deal = latest_close_by_position.get(ticket)
        entry["status"] = "closed"
        if deal:
            entry["closed_at"] = deal.get("time") or datetime.now(timezone.utc).isoformat()
            profit = deal.get("profit")
            entry["exit_price"] = deal.get("price")
            entry["profit"] = profit
            entry["outcome"] = "win" if (profit or 0) > 0 else ("loss" if (profit or 0) < 0 else "breakeven")
            entry.update(_classify_excursion(entry, deal))
        else:
            entry["closed_at"] = datetime.now(timezone.utc).isoformat()
            entry["outcome"] = "unknown"
        changed = True

    if changed:
        _write_journal(entries)


def _journal_summary_text(symbol: str) -> str | None:
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


def _in_weekend_window(now: datetime) -> bool:
    if now.weekday() in (5, 6):
        return True
    return now.weekday() == 4 and now.hour >= WEEKEND_CUTOFF_UTC_HOUR


def _next_session_boundary(now: datetime) -> tuple[datetime, str]:
    """Return the next (UTC datetime, session label) boundary strictly after now.

    Mirrored from committee_reporter.py's identical function -- see that
    file's own docstring for the full schedule/DST rationale. Only
    "new_york" trades; "asia"/"london" are research-only.
    """
    day = now.date()
    asia = datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc)
    london = datetime(day.year, day.month, day.day, 8, 0, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
    new_york = datetime(day.year, day.month, day.day, 8, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)

    for boundary, label in sorted([(asia, "asia"), (london, "london"), (new_york, "new_york")]):
        if boundary > now:
            return boundary, label

    tomorrow = day + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, tzinfo=timezone.utc), "asia"


def _next_pass_due_text(now: datetime) -> str:
    """Human-readable 'next scheduled pass' string, straight from the schedule."""
    boundary, session = _next_session_boundary(now)
    return f"{boundary.strftime('%Y-%m-%d %H:%M:%S')} UTC ({session})"


_DECISION_LINE_RE = re.compile(r"^Decision:\s*(.+)$", re.MULTILINE)
_REASONING_LINE_RE = re.compile(r"^Reasoning:\s*(.+)$", re.MULTILINE)


def _parse_decision_reasoning(report_text: str) -> tuple[str, str] | None:
    """Extract the Decision/Reasoning lines from a research-only pass's report.

    Mirrored from committee_reporter.py's identical function.
    """
    decision_match = _DECISION_LINE_RE.search(report_text)
    reasoning_match = _REASONING_LINE_RE.search(report_text)
    if not decision_match or not reasoning_match:
        return None
    return decision_match.group(1).strip(), reasoning_match.group(1).strip()


def _read_session_bias() -> dict:
    try:
        return json.loads(SESSION_BIAS_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_session_bias(data: dict) -> None:
    SESSION_BIAS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_BIAS_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _record_session_bias(symbol: str, session: str, report_text: str) -> None:
    """Save a research-only pass's Decision/Reasoning for the NY pass to read back.

    Mirrored from committee_reporter.py's identical function.
    """
    parsed = _parse_decision_reasoning(report_text)
    if parsed is None:
        return
    decision, reasoning = parsed
    data = _read_session_bias()
    data.setdefault(symbol, {})[session] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "decision": decision,
        "reasoning": reasoning,
    }
    _write_session_bias(data)


def _session_bias_fact(symbol: str) -> str:
    """Build a prompt fact block from today's Asia/London reads for symbol, or "" if none."""
    entries = _read_session_bias().get(symbol, {})
    today = datetime.now(timezone.utc).date().isoformat()
    lines = []
    for session in ("asia", "london"):
        entry = entries.get(session)
        if not isinstance(entry, dict) or entry.get("date") != today:
            continue
        lines.append(
            f"Your {session.capitalize()} session read on {symbol} today: {entry.get('decision', '')} "
            f"— {entry.get('reasoning', '')}"
        )
    if not lines:
        return ""
    return (
        "Earlier session reads today (research-only passes, no trade was placed on "
        "either — this is context for your decision now, not a commitment to follow it):\n"
        + "\n".join(lines)
    )


def _read_weekend_state() -> dict:
    try:
        return json.loads(WEEKEND_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_weekend_state(data: dict) -> None:
    WEEKEND_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEEKEND_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _weekend_flatten_and_notify() -> None:
    """See module docstring: weekend-flatten stays ON for this account even
    though FundedNext itself permits weekend holding — the tighter
    self-imposed daily/static-drawdown margins here can't absorb gap risk."""
    week_key = datetime.now(timezone.utc).strftime("%G-W%V")
    already_notified = _read_weekend_state().get("week") == week_key

    closed_lines: list[str] = []
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue

        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.connectors.mt5 import sdk as mt5_sdk
        from src.trading.profiles import profile_by_id
        from src.trading.service import get_positions

        try:
            positions = get_positions(trade["connection"]).get("positions", [])
        except Exception:
            logger.exception("weekend flatten: could not read positions for %s", trade["symbol"])
            continue

        ours = [
            p for p in positions
            if p.get("symbol") == trade["symbol"] and p.get("magic") == OUR_MAGIC
        ]
        if not ours:
            continue

        try:
            profile = profile_by_id(trade["connection"])
            config = mt5_sdk.build_config(profile.config, {})
        except Exception:
            logger.exception("weekend flatten: could not build config for %s", trade["connection"])
            continue

        for pos in ours:
            ticket = pos.get("ticket")
            try:
                result = mt5_sdk.close_position(config, ticket=ticket)
            except Exception as exc:
                closed_lines.append(f"FAILED to close ticket {ticket} on {trade['symbol']}: {exc}")
                logger.exception("weekend flatten: close_position raised for ticket %s", ticket)
                continue
            if result.get("status") == "ok":
                closed_lines.append(
                    f"Closed {pos.get('side', '?').upper()} {result.get('closed_volume', pos.get('volume'))} lots "
                    f"{trade['symbol']} @ {result.get('fill_price', '?')} "
                    f"(open P&L at trigger \u2248 {pos.get('profit')})"
                )
                logger.info("weekend flatten: closed ticket %s on %s", ticket, trade["symbol"])
            else:
                closed_lines.append(f"FAILED to close ticket {ticket} on {trade['symbol']}: {result.get('error')}")
                logger.error("weekend flatten: close_position failed for ticket %s: %s", ticket, result.get("error"))

    if already_notified and not closed_lines:
        return

    body_lines = ["Market closed for the weekend — no FundedNext committee runs until Monday (UTC)."]
    if closed_lines:
        body_lines.append("")
        body_lines.append("Positions flattened ahead of the weekend (no-weekend-hold rule, code-enforced):")
        body_lines.extend(f"  - {line}" for line in closed_lines)
    else:
        body_lines.append("No open positions of ours to flatten.")

    try:
        send_email("[FundedNext] Weekend status — market closed", _status_header() + "\n".join(body_lines))
    except Exception:
        logger.exception("failed to send weekend status email")

    _write_weekend_state({"week": week_key})


def _profit_protection_check() -> None:
    """See committee_reporter.py's identical function for the full rationale
    of each of the three rules (breakeven-at-halfway, ATR-aware early-profit
    trail, time-decay). Pure code, zero LLM cost."""
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue

        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.connectors.mt5 import sdk as mt5_sdk
        from src.trading.profiles import profile_by_id
        from src.trading.service import get_positions

        try:
            positions = get_positions(trade["connection"]).get("positions", [])
        except Exception:
            logger.exception("profit protection check: could not read positions for %s", trade["symbol"])
            continue

        ours = [
            p for p in positions
            if p.get("symbol") == trade["symbol"] and p.get("magic") == OUR_MAGIC
        ]
        if not ours:
            continue

        try:
            profile = profile_by_id(trade["connection"])
            config = mt5_sdk.build_config(profile.config, {})
        except Exception:
            logger.exception("profit protection check: could not build config for %s", trade["connection"])
            continue

        for pos in ours:
            max_hold_hours = trade.get("max_hold_hours", MAX_HOLD_HOURS)
            elapsed_hours = None
            # Note: pos["time"] is a raw MT5 epoch value -- same broker-
            # local-time-mislabeled-as-UTC skew found and fixed in
            # _recent_deals (agent/src/trading/connectors/mt5/sdk.py) can
            # under-count elapsed_hours here by roughly the broker's UTC
            # offset (currently ~3h). Lower severity than the journal-
            # reconciliation miss that fix addressed (this only shifts
            # MAX_HOLD_HOURS's ~40h window by a few hours, not a hard
            # correctness failure), so left as a known imprecision rather
            # than reworked here -- revisit if MAX_HOLD_HOURS is ever
            # tightened close to that offset's scale.
            opened_raw = pos.get("time")
            if opened_raw:
                try:
                    elapsed_hours = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(str(opened_raw))
                    ).total_seconds() / 3600.0
                except (TypeError, ValueError):
                    elapsed_hours = None

            if elapsed_hours is not None and elapsed_hours >= max_hold_hours:
                try:
                    result = mt5_sdk.close_position(config, ticket=pos.get("ticket"))
                except Exception:
                    logger.exception(
                        "profit protection check: time-stop close_position raised for ticket %s", pos.get("ticket"),
                    )
                    continue
                if result.get("status") == "ok":
                    logger.info(
                        "profit protection check: time-stop flattened ticket %s on %s after %.1fh "
                        "(open P&L at trigger %s)",
                        pos.get("ticket"), trade["symbol"], elapsed_hours, pos.get("profit"),
                    )
                else:
                    logger.error(
                        "profit protection check: time-stop close_position failed for ticket %s: %s",
                        pos.get("ticket"), result.get("error"),
                    )
                continue

            entry, sl, tp, price = pos.get("price_open"), pos.get("stop_loss"), pos.get("take_profit"), pos.get("price_current")
            side = pos.get("side")
            if entry is None or sl is None or tp is None or price is None or side not in ("buy", "sell"):
                continue
            entry, sl, tp, price = float(entry), float(sl), float(tp), float(price)
            is_buy = side == "buy"

            breakeven_candidate = None
            halfway = entry + (tp - entry) * BREAKEVEN_TRIGGER_FRACTION
            reached_halfway = price >= halfway if is_buy else price <= halfway
            if reached_halfway:
                try:
                    point = mt5_sdk.point_size(trade["symbol"], config=config)
                except Exception as exc:
                    # 2026-09-21: found while investigating two gold reversal
                    # trades (this account + Exness) that moved 67%/89% of the
                    # way to target -- well past every protection trigger --
                    # and still closed at a near-full loss. Couldn't
                    # reproduce a live failure here, but this call used to
                    # fail dead silent (bare except, no log), which would
                    # make a repeat of that pattern undiagnosable. Logged
                    # now so a real recurrence leaves a trace.
                    logger.warning(
                        "profit protection check: point_size lookup failed for %s, skipping breakeven rule this cycle: %s",
                        trade["symbol"], exc,
                    )
                    point = None
                if point and point > 0:
                    buffer = point * BREAKEVEN_BUFFER_POINTS
                    breakeven_candidate = entry - buffer if is_buy else entry + buffer

            trail_candidate = None
            try:
                size = mt5_sdk.contract_size(trade["symbol"], config=config)
            except Exception as exc:
                logger.warning(
                    "profit protection check: contract_size lookup failed for %s, skipping trail rule this cycle: %s",
                    trade["symbol"], exc,
                )
                size = None
            if size and size > 0 and trade["lots"] > 0:
                trigger_usd = trade.get("early_profit_trigger_usd", EARLY_PROFIT_TRIGGER_USD)
                trigger_distance = trigger_usd / (size * trade["lots"])
                gained = (price - entry) if is_buy else (entry - price)
                if gained >= trigger_distance:
                    atr_distance = _atr_stop_floor(trade["symbol"], trade["connection"])
                    if atr_distance:
                        trail_candidate = price - atr_distance if is_buy else price + atr_distance

            decay_candidate = None
            if elapsed_hours is not None and elapsed_hours >= max_hold_hours * TIME_DECAY_START_FRACTION:
                atr_distance = _atr_stop_floor(trade["symbol"], trade["connection"])
                if atr_distance:
                    decay_candidate = price - atr_distance if is_buy else price + atr_distance

            candidates = [c for c in (breakeven_candidate, trail_candidate, decay_candidate) if c is not None]
            if not candidates:
                continue
            new_sl = max(candidates) if is_buy else min(candidates)
            improves = new_sl > sl if is_buy else new_sl < sl
            if not improves:
                continue

            try:
                result = mt5_sdk.modify_position(config, ticket=pos.get("ticket"), stop_loss=new_sl, take_profit=tp)
            except Exception:
                logger.exception("profit protection check: modify_position raised for ticket %s", pos.get("ticket"))
                continue
            if result.get("status") == "ok":
                logger.info(
                    "profit protection check: moved SL to %.5f for ticket %s on %s (price %.5f, entry %.5f)",
                    new_sl, pos.get("ticket"), trade["symbol"], price, entry,
                )
            else:
                logger.error(
                    "profit protection check: modify_position failed for ticket %s: %s",
                    pos.get("ticket"), result.get("error"),
                )


@dataclass
class CommitteeResult:
    committee: str
    target: str
    market: str
    status: str
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


def _challenge_framing(connection: str | None) -> str:
    """Prop-firm-challenge framing, replacing committee_reporter.py's
    _DAY_TRADE_FRAMING. States the hard profit target/loss limits up front and
    makes explicit that capital preservation/compliance beats speed — see
    the plan doc's Step 9 for the rationale.

    ``connection`` is None for a research-only (no ``trade`` dict) target —
    there is nothing to initialize/read an account balance for in that case,
    so this falls back to whatever state already exists without touching the
    connector.

    Stellar 1-Step is a SINGLE-PHASE challenge (one 10% target, then straight
    to the funded account) — this was originally written for the Stellar
    2-Step model (two phases, 8%/5%) before the actual purchased account
    turned out to be 1-Step; corrected 2026-09-15, see fundednext_state.py's
    CHALLENGE_TARGET_PCT/MIN_TRADING_DAYS.
    """
    state = fn_state.ensure_initialized(connection) if connection else fn_state.get_state()
    target_pct = fn_state.CHALLENGE_TARGET_PCT
    initial = state.get("initial_balance_usd")
    progress_note = ""
    if initial and connection:
        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.service import get_account

        try:
            balance = float(get_account(connection)["account"]["balance"])
            progress = fn_state.progress_pct(balance)
            days = fn_state.trading_days_count()
            if progress is not None:
                progress_note = (
                    f"Current progress: {progress:+.2f}% toward the {target_pct:.0f}% "
                    f"target (starting balance ${float(initial):.2f}, current balance ${balance:.2f}). "
                    f"Trading days logged so far: {days}/{fn_state.MIN_TRADING_DAYS} minimum (FundedNext's "
                    f"own rule; non-consecutive is fine, no rush to hit it early).\n\n"
                )
        except Exception:
            progress_note = ""

    return (
        "IMPORTANT — this account is a FundedNext Stellar 1-Step PROP-FIRM CHALLENGE account, "
        f"not a profit-maximizing live account. The goal is to clear a {target_pct:.0f}% profit target "
        "WITHOUT ever touching this account's hard daily-loss or overall-drawdown limits — capital "
        "preservation and staying compliant with FundedNext's own trading rules takes priority over "
        "speed or size of gains. Do NOT chase the profit target aggressively: steady, low-variance "
        "progress that never approaches the risk limits below is the explicit goal, not maximizing "
        "return or hitting the target quickly. Treat every risk-budget figure given to you in this "
        "prompt as a hard CEILING, never a target to size up toward. Never propose anything resembling "
        "grid trading, martingale, or averaging into a losing position — this account trades one "
        "fixed-size clip per signal and nothing else, full stop; a losing position is managed by its "
        "stop-loss, never by adding to it. This is also a SHORT-TERM, same-day/next-session trade "
        "decision, not a multi-week swing or position trade (the committee's default style is a "
        "buy-side fund position-trade horizon of weeks to months — explicitly override that here): "
        "weight intraday/short-term technicals over multi-week structural setups, and size the "
        "stop-loss/take-profit for a trade meant to resolve within the current or next session.\n\n"
        f"{progress_note}"
    )


def _build_prompt(committee: str, target: str, market: str, trade: dict | None) -> str:
    if not trade:
        return (
            f'Run the {committee} swarm with target="{target}" and market="{market}" '
            f"to produce its full debate and final decision.\n\n"
            f"{_challenge_framing(None)}"
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
            f'return for a "similar" instrument — is the price your stop-loss and take-profit levels '
            f"must be anchored to. Use your research data for direction/technicals/timing, but re-derive "
            f"the actual stop/target PRICES relative to this live quote, not the research price."
        )
    else:
        quote_fact = (
            f'Could not read a live quote for "{symbol}" from the {connection} broker feed just now — '
            f"call trading_quote yourself for this symbol before finalizing any stop-loss/take-profit "
            f"level, and anchor to that, not to a research-data price."
        )

    effective_budget = fn_guard.effective_max_loss_usd(connection)
    max_distance = _max_stop_distance(symbol, lots, effective_budget, connection)
    if max_distance is not None:
        budget_note = (
            f"${effective_budget:.2f} (this account's self-imposed {fn_guard.RISK_PER_TRADE_FRACTION:.0%}-of-"
            f"current-balance per-trade cap — tighter than the ${fn_guard.MANDATE_MAX_LOSS_PER_ORDER_USD:.2f} "
            f"mandate ceiling right now)"
            if effective_budget < fn_guard.MANDATE_MAX_LOSS_PER_ORDER_USD
            else f"${effective_budget:.2f}"
        )
        risk_fact = (
            f"RISK BUDGET for this pass: at {lots} lots, size the stop-loss to stay within {budget_note} "
            f"of worst-case planned loss — your stop-loss must be within {max_distance:.3f} price units "
            f'of entry (whichever side is the losing side for "{symbol}"). The broker gate independently '
            f"denies outright anything past this account's ${fn_guard.MANDATE_MAX_LOSS_PER_ORDER_USD:.2f} "
            f"mandate ceiling regardless of what you propose, but size to the tighter budget above, not "
            f"just the gate's outer limit — this is a PROP-FIRM CHALLENGE account, so a tighter, valid "
            f"stop that actually executes is strictly better than a wider one that risks denial or eats "
            f"into the daily-loss/drawdown limits."
        )
    else:
        risk_fact = (
            f"Could not compute the exact price-distance budget for this account's ${effective_budget:.2f} "
            f"max-loss-per-order cap — size the stop conservatively; a wide stop risks outright denial by the "
            f"broker gate regardless of what you propose."
        )

    atr_floor = _atr_stop_floor(symbol, connection)
    spread_floor = _spread_stop_floor(quote)
    floor_candidates = [
        (v, reason) for v, reason in (
            (atr_floor, f"{ATR_STOP_MULTIPLE}x the last {ATR_LOOKBACK_BARS}-bar {ATR_PERIOD} ATR"),
            (spread_floor, f"{MIN_STOP_TO_SPREAD_RATIO:.0f}x the live bid/ask spread"),
        ) if v is not None
    ]
    stop_floor, floor_reason = max(floor_candidates, key=lambda pair: pair[0]) if floor_candidates else (None, None)

    if stop_floor is None:
        volatility_fact = None
    elif max_distance is not None and stop_floor > max_distance:
        volatility_fact = (
            f"VOLATILITY/SPREAD FLOOR — READ BEFORE TRADING: the minimum stop distance to sit outside "
            f"\"{symbol}\"'s current normal noise and keep the live spread's own share of the stop under "
            f"1/{MIN_STOP_TO_SPREAD_RATIO:.0f} is {stop_floor:.3f} price units (binding constraint right now: "
            f"{floor_reason}) — WIDER than the {max_distance:.3f}-unit hard risk limit above. There is no "
            f"stop that is both inside the risk cap AND outside normal noise/cost right now. Given this, the "
            f"decision must be WAIT — do not place a trade this pass no matter how strong the setup looks; "
            f"note in your report that current volatility/spread conditions do not fit this account's risk "
            f"budget at this position size."
        )
    elif max_distance is not None:
        volatility_fact = (
            f"Volatility/spread floor: to sit outside \"{symbol}\"'s current normal noise and keep the live "
            f"spread's own share of the stop under 1/{MIN_STOP_TO_SPREAD_RATIO:.0f}, size the stop-loss at or "
            f"beyond {stop_floor:.3f} price units from entry (binding constraint right now: {floor_reason}) — "
            f"combined with the hard risk limit above, your stop should land between {stop_floor:.3f} and "
            f"{max_distance:.3f} price units from entry."
        )
    else:
        volatility_fact = (
            f"Volatility/spread floor: to sit outside \"{symbol}\"'s current normal noise and keep the live "
            f"spread's own share of the stop under 1/{MIN_STOP_TO_SPREAD_RATIO:.0f}, size the stop-loss at or "
            f"beyond {stop_floor:.3f} price units from entry (binding constraint right now: {floor_reason})."
        )
    volatility_block = f"{volatility_fact}\n\n" if volatility_fact else ""

    _journal_reconcile_closed(symbol, connection)
    journal_fact = _journal_summary_text(symbol)
    journal_block = f"{journal_fact}\n\n" if journal_fact else ""

    session_bias_fact = _session_bias_fact(symbol)
    session_bias_block = f"{session_bias_fact}\n\n" if session_bias_fact else ""

    return (
        f'Run the {committee} swarm with target="{target}" ({market}) to produce its full debate '
        f"and final decision, including concrete stop-loss and take-profit price levels — every "
        f"committee decision must carry these, not just a direction.\n\n"
        f"{quote_fact}\n\n"
        f"{risk_fact}\n\n"
        f"{volatility_block}"
        f"{journal_block}"
        f"{session_bias_block}"
        f"{_challenge_framing(connection)}"
        f"Then, based ONLY on the portfolio manager's final decision:\n"
        f"- {position_fact} Still call trading_positions yourself too (for your own report, and as a "
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
        f"      stop_loss=<see stop-loss recomputation rule below>,\n"
        f"      take_profit=<PM's nearest target price, shifted by the same live-quote adjustment as the "
        f"stop-loss below, so the planned reward:risk ratio is preserved rather than skewed by drift>,\n"
        f'      time_in_force="day",\n'
        f"  )\n\n"
        f"STOP-LOSS RECOMPUTATION RULE (read this before filling in stop_loss above): the PM's stop was "
        f"designed as a DISTANCE from their intended entry, not as a fixed absolute price — that distance, "
        f"capped at the HARD RISK LIMIT distance above if the PM's is wider, is what must survive to "
        f"execution. Compute stop_loss as (PM's intended distance, capped at the HARD RISK LIMIT) applied "
        f"from the LIVE quote above — the actual price you are about to fill at — not from the PM's "
        f"original entry reference. This applies just as much on a RETRY after a rejected order — recompute "
        f"it fresh from the live price at retry time, the same way.\n\n"
        f"Do not call trading_place_order at all if any condition above fails — that includes a "
        f"wait/hold decision, a missing stop-loss, an opposite-direction position already open, or the "
        f"cap already reached. There is no partial/scaled/limit-order version of this call: either place "
        f"exactly the order above, or place nothing.\n\n"
        f"{_REPORT_FORMAT_TRADE}"
    )


def run_committee(committee: str, target: str, market: str, trade: dict | None = None) -> CommitteeResult:
    """Run one committee preset against one target as an isolated subprocess.

    Two pre-checks run before a trade-enabled pass, either of which can fall
    back the pass to research-only: fundednext_guardrails.guardrail_check
    (daily-loss/static-drawdown/trade-count) and, if that clears, a news-
    blackout check (fundednext_news_calendar) for the symbol's currencies.
    """
    breaker_note = None
    if trade:
        live_symbols = {
            spec["trade"]["symbol"]
            for spec in TARGETS
            if spec.get("trade") and spec["trade"]["connection"] == trade["connection"]
        }
        breaker_note = fn_guard.guardrail_check(trade, live_symbols, OUR_MAGIC)
        if breaker_note:
            logger.warning("FundedNext guardrail for %s: %s", target, breaker_note)
            trade = None

    if trade:
        currencies = _symbol_currencies(trade["symbol"])
        in_blackout, why = fn_news.is_news_blackout(currencies, window_minutes=NEWS_BLACKOUT_WINDOW_MINUTES)
        if in_blackout:
            breaker_note = f"[NEWS BLACKOUT] {trade['symbol']} pass run research-only: {why}."
            logger.warning("news blackout for %s: %s", target, why)
            trade = None

    prompt = _build_prompt(committee, target, market, trade)
    logger.info("running %s on %s (%s)%s", committee, target, market, " [trade-enabled]" if trade else "")
    cmd = [
        sys.executable, "-m", "cli", "run",
        "--prompt", prompt,
        "--json",
        "--max-iter", str(MAX_ITER),
    ]
    popen = subprocess.Popen(
        cmd,
        cwd=str(AGENT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **LLM_MODEL_OVERRIDE},
    )
    try:
        stdout, stderr = popen.communicate(timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(popen.pid)
        try:
            popen.communicate(timeout=15)
        except Exception:
            pass
        logger.warning("%s on %s timed out after %ss", committee, target, RUN_TIMEOUT_SECONDS)
        return CommitteeResult(committee, target, market, "timeout", None, "", error="run exceeded timeout")
    proc = subprocess.CompletedProcess(cmd, popen.returncode, stdout, stderr)

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

    placed_order = _extract_placed_order(run_id) if trade else None
    traded = bool(placed_order)
    if traded:
        # Resolve the real fill price ONCE here and patch it into
        # placed_order, so every downstream consumer (_post_trade_spread_
        # check, _post_trade_reward_risk_check, _journal_record_open) can
        # just read placed_order["fill_price"] directly instead of each
        # independently calling _resolve_fill_price (and its own
        # get_positions broker round-trip) -- mirrors the identical fix in
        # committee_reporter.py's run_committee, found by /code-review.
        resolved_price = _resolve_fill_price(trade, placed_order)
        if resolved_price is not None:
            placed_order["fill_price"] = resolved_price
        spec_note = _post_trade_spec_check(trade, placed_order)
        if spec_note:
            # A spec violation is CLOSED, not just reported -- see
            # _post_trade_spec_check's docstring for why this is a real
            # corrective action, not another informational-only check.
            report_text = report_text + spec_note
            traded = False
        else:
            report_text = report_text + _post_trade_cap_check(trade)
            report_text = report_text + _post_trade_spread_check(trade, placed_order)
            report_text = report_text + _post_trade_reward_risk_check(trade, placed_order)
            _journal_record_open(trade["symbol"], trade["connection"], placed_order)
    elif trade:
        blocked_note = _blocked_order_note(run_id)
        if blocked_note:
            report_text = report_text + blocked_note
    if breaker_note:
        report_text = report_text + f"\n\n{breaker_note}"
    return CommitteeResult(committee, target, market, "success", run_id, report_text, traded=traded)


def _post_trade_spec_check(trade: dict, placed_order: dict) -> str:
    """Verify a just-filled order actually matches TARGETS' spec (symbol,
    lots, market order) -- and if not, CLOSE it immediately and halt further
    trading, rather than just noting the mismatch in the email afterward.

    Real incident 2026-09-17: the committee submitted symbol="EURUSDm"
    (wrong -- TARGETS says "EURUSD"), quantity=0.25 (25x the mandated 0.01),
    as a limit order with its own staged price levels, directly
    contradicting the prompt's explicit, hardcoded
    trading_place_order(...) template and its "no partial/scaled/limit-
    order version of this call" instruction. The mandate gate happened to
    deny it (EURUSDm doesn't resolve to a priceable instrument on this
    broker), and separately the $3,000 max_order_notional_usd cap would
    also have denied a 0.25-lot fill even with a VALID symbol -- but the
    mandate's caps are a coarse net sized around dollar limits, not a
    guarantee of catching every parameter deviation (e.g. a more modestly
    oversized order, ~0.02 lots, could clear both the notional and
    per-order-loss caps while still being 2x the intended size). This
    function is the actual, specific backstop for "did the LLM follow the
    fixed order parameters," independent of whether the broker-side dollar
    caps happen to also catch a given deviation.

    Returns "" if the order matches spec (nothing to do). Returns a
    [CRITICAL] message (and has already closed the position + tripped the
    halt) if it doesn't.
    """
    mismatches: list[str] = []
    actual_symbol = placed_order.get("symbol")
    if actual_symbol != trade["symbol"]:
        mismatches.append(f'symbol "{actual_symbol}" != expected "{trade["symbol"]}"')
    actual_lots = placed_order.get("quantity")
    try:
        lots_ok = actual_lots is not None and abs(float(actual_lots) - float(trade["lots"])) < 1e-9
    except (TypeError, ValueError):
        lots_ok = False
    if not lots_ok:
        mismatches.append(f"quantity {actual_lots} != expected {trade['lots']} lots")
    order_type = str(placed_order.get("order_type") or "").lower()
    if order_type and "market" not in order_type:
        mismatches.append(f'order_type "{order_type}" is not a market order')
    if not mismatches:
        return ""

    reason = "; ".join(mismatches)
    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import trip_halt
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.service import get_positions

    # Match by the NEW order's own ticket (order_id), not fn_guard's
    # _flatten_positions(symbol+magic) -- real bug found by /code-review
    # (mirrored from the identical fix in committee_reporter.py): with
    # multiple legitimate stacked positions on this symbol, symbol+magic
    # alone force-closes ALL of them on a spec violation, not just the one
    # bad new fill. _flatten_positions itself is correct and untouched --
    # it's still the right tool for a real portfolio-level halt flatten
    # elsewhere in fundednext_guardrails.py, just not for this ticket-
    # specific case.
    target_ticket = str(placed_order.get("order_id") or "").strip()
    closed: list[str] = []
    try:
        positions = get_positions(trade["connection"]).get("positions", [])
        matches = [
            p for p in positions
            if p.get("symbol") == (actual_symbol or trade["symbol"])
            and p.get("magic") == OUR_MAGIC
            and target_ticket
            and str(p.get("ticket")) == target_ticket
        ]
        if matches:
            config = _mt5_config_for(trade["connection"])
            for pos in matches:  # should be exactly one; loop defensively
                ticket = pos.get("ticket")
                result = mt5_sdk.close_position(config, ticket=ticket)
                closed.append(f"{pos.get('symbol')} ticket {ticket}: {result.get('status')}")
        elif not target_ticket:
            closed.append(
                "could not identify the new position's own ticket (order_id missing) -- "
                "refusing to blindly close other positions on this symbol; close manually"
            )
        else:
            closed.append(f"no open position found with ticket {target_ticket} (already closed/never opened?)")
    except Exception as exc:
        closed.append(f"flatten attempt raised: {exc}")

    trip_halt(
        by="cli",
        reason=f"post-trade spec violation on {trade['symbol']}: {reason}",
        broker=fn_guard.BROKER,
    )
    closed_note = "; ".join(closed) if closed else "no position found to close (already flat?)"
    return (
        f"\n\n[CRITICAL — SPEC VIOLATION, AUTO-CLOSED] The filled order did NOT match TARGETS' "
        f"fixed spec: {reason}. This position has been closed immediately and {fn_guard.BROKER} "
        f"trading is now HALTED (kill switch) until manually cleared "
        f"(src.live.halt.clear_halt) — the committee did not follow the prompt's hardcoded "
        f"order parameters, and this needs human review before further automated trading. "
        f"Close attempt: {closed_note}."
    )


def _post_trade_cap_check(trade: dict) -> str:
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


def _post_trade_spread_check(trade: dict, placed_order: dict) -> str:
    entry = _resolve_fill_price(trade, placed_order)
    stop_loss = placed_order.get("stop_loss")
    if entry is None or stop_loss is None:
        return ""
    stop_distance = abs(float(entry) - float(stop_loss))
    if stop_distance <= 0:
        return ""

    quote = _symbol_live_quote(trade["symbol"], trade["connection"])
    if not quote:
        return ""
    spread = quote["ask"] - quote["bid"]
    if spread <= 0:
        return ""

    ratio = stop_distance / spread
    if ratio >= MIN_STOP_TO_SPREAD_RATIO:
        return ""
    spread_share = spread / stop_distance
    return (
        f"\n\n[AUTOMATED CHECK] {trade['symbol']}'s actual stop distance ({stop_distance:.5f}) is only "
        f"{ratio:.1f}x the live spread ({spread:.5f}) — below the {MIN_STOP_TO_SPREAD_RATIO:.0f}x floor, "
        f"meaning the spread alone accounts for ~{spread_share:.0%} of this stop's planned risk (before any "
        f"fill slippage on top). The prompt's volatility/spread floor should have prevented this; check why "
        f"it didn't."
    )


def _resolve_fill_price(trade: dict, placed_order: dict) -> float | None:
    """placed_order['fill_price'] from trading_place_order's own immediate
    response.

    Real incident 2026-09-17: MT5's order_send() result sometimes doesn't
    have the deal's fill price populated yet at the moment the tool call
    returns (a known propagation gap between order confirmation and the
    fill being reflected) — this showed up as a literal 0.0, not a missing
    field, so callers checking `is None` alone don't catch it. That 0.0 fed
    straight into _post_trade_reward_risk_check's math produced a negative
    reward distance, which was silently (and technically correctly, given
    the bad input) treated as "nothing sane to enforce" — meaning the
    guardrail never actually ran on a real trade the very first time it had
    the chance to. Falls back to the live position's own price_open (a
    fresh get_positions() read, same source --status uses) whenever
    fill_price is missing OR non-positive.
    """
    price = placed_order.get("fill_price")
    try:
        if price is not None and float(price) > 0:
            return float(price)
    except (TypeError, ValueError):
        pass

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_positions

    try:
        positions = get_positions(trade["connection"]).get("positions", [])
    except Exception:
        return None
    symbol = placed_order.get("symbol") or trade["symbol"]
    for pos in positions:
        if pos.get("symbol") == symbol and pos.get("magic") == OUR_MAGIC:
            try:
                open_price = float(pos.get("price_open"))
            except (TypeError, ValueError):
                continue
            if open_price > 0:
                return open_price
    return None


def _post_trade_reward_risk_check(trade: dict, placed_order: dict) -> str:
    """Enforce MIN_REWARD_RISK_RATIO on a just-filled order — not just
    prompt guidance (the PM already claims to target ~1.5:1 in its own
    reasoning, but nothing enforced that in code). See MIN_REWARD_RISK_RATIO's
    own comment for the real trade data that motivated this.

    If the filled stop_loss/take_profit imply a ratio below the floor,
    corrects it immediately on the live position:
      - Prefer TIGHTENING the stop-loss (more conservative — reduces risk
        without changing the profit target) down to whatever restores the
        ratio, but never past the ATR/spread volatility floor
        (_atr_stop_floor/_spread_stop_floor) — an over-tight stop just
        trades "loses less" for "gets stopped out by ordinary noise more
        often," the opposite of the goal.
      - If tightening the stop that far would violate the volatility floor
        (the stop was already at/near the floor and the target was simply
        too close), WIDEN the take-profit instead, preserving the stop's
        already-floor-respecting distance.

    Returns "" if the ratio is already fine, missing data means nothing
    can be safely checked, or no matching position exists.
    """
    side = placed_order.get("side")
    entry = _resolve_fill_price(trade, placed_order)
    sl = placed_order.get("stop_loss")
    tp = placed_order.get("take_profit")
    if side not in ("buy", "sell") or entry is None or sl is None or tp is None:
        return ""
    entry, sl, tp = float(entry), float(sl), float(tp)
    is_buy = side == "buy"

    risk_distance = (entry - sl) if is_buy else (sl - entry)
    reward_distance = (tp - entry) if is_buy else (entry - tp)
    if risk_distance <= 0 or reward_distance <= 0:
        return ""  # malformed levels -- nothing sane to enforce

    ratio = reward_distance / risk_distance
    if ratio >= MIN_REWARD_RISK_RATIO:
        return ""

    symbol = trade["symbol"]
    quote = _symbol_live_quote(symbol, trade["connection"])
    floor_distance = max(_atr_stop_floor(symbol, trade["connection"]) or 0.0, _spread_stop_floor(quote) or 0.0)

    desired_risk_distance = reward_distance / MIN_REWARD_RISK_RATIO
    if desired_risk_distance >= floor_distance:
        new_sl = entry - desired_risk_distance if is_buy else entry + desired_risk_distance
        new_tp = tp
        action = f"tightened stop-loss to {new_sl:.5f}"
    else:
        desired_reward_distance = risk_distance * MIN_REWARD_RISK_RATIO
        new_sl = sl
        new_tp = entry + desired_reward_distance if is_buy else entry - desired_reward_distance
        action = f"widened take-profit to {new_tp:.5f}"

    header = (
        f"{symbol}'s filled order has only a {ratio:.2f}:1 reward:risk ratio "
        f"(entry {entry:.5f}, stop {sl:.5f}, target {tp:.5f}), below the {MIN_REWARD_RISK_RATIO:.1f}:1 floor"
    )

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.service import get_positions

    # Match by the NEW order's own ticket (order_id), not ours[0] -- real
    # bug found by /code-review (mirrored from the identical fix in
    # committee_reporter.py): with multiple legitimate stacked positions on
    # this symbol, ours[0] (whatever order the broker happens to return)
    # could be an older, already-compliant position -- modifying it leaves
    # the actual sub-floor new fill uncorrected while altering a healthy one.
    target_ticket = str(placed_order.get("order_id") or "").strip()
    try:
        positions = get_positions(trade["connection"]).get("positions", [])
        matches = [
            p for p in positions
            if p.get("symbol") == symbol and p.get("magic") == OUR_MAGIC
            and target_ticket and str(p.get("ticket")) == target_ticket
        ]
        if not matches:
            return (
                f"\n\n[AUTOMATED CHECK] {header} — no matching open position "
                f"(ticket {target_ticket or '?'}) was found to correct; check manually."
            )
        config = _mt5_config_for(trade["connection"])
        result = mt5_sdk.modify_position(config, ticket=matches[0].get("ticket"), stop_loss=new_sl, take_profit=new_tp)
    except Exception as exc:
        return f"\n\n[AUTOMATED CHECK] {header} — correction attempt raised: {exc}."

    if result.get("status") != "ok":
        return f"\n\n[AUTOMATED CHECK] {header} — correction attempt failed: {result.get('error')}."
    # Mutate placed_order in place so the caller's subsequent journal write
    # (_journal_record_open) records the ACTUAL live stop/target, not the
    # stale pre-correction values -- mirrors the identical fix in
    # committee_reporter.py, found by /code-review.
    placed_order["stop_loss"] = new_sl
    placed_order["take_profit"] = new_tp
    return f"\n\n[AUTOMATED CHECK — CORRECTED] {header} — {action} to restore it."


def _last_json_line(stdout: str | None) -> dict | None:
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
    answers = [e["content"] for e in _trace_entries(run_id) if e.get("type") == "answer" and e.get("content")]
    return answers[-1] if answers else ""


def is_reportable(result: CommitteeResult) -> bool:
    return True


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


def send_email(subject: str, body: str, html_body: str | None = None) -> None:
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


_TAG_COLORS = {"TRADED": "#2e7d32", "OK": "#555555", "ERROR": "#c62828", "TIMEOUT": "#c62828", "CRASHED": "#c62828"}
_EMAIL_FONT = "font-family:Arial,Helvetica,sans-serif;"


def _esc(text: object) -> str:
    return html_module.escape(str(text), quote=False)


def _inline_markdown(escaped_text: str) -> str:
    escaped_text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped_text)
    escaped_text = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped_text)
    return escaped_text


def _table_block_to_html(lines: list[str]) -> str:
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{1,}:?", c) for c in cells):
            continue
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


def _format_body_html(result: CommitteeResult, tag: str) -> str:
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
    return (
        "<html><body style=\"margin:0;padding:16px;background:#ffffff;\">"
        f'<div style="max-width:640px;margin:0 auto;">{inner}</div>'
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Driving loop
# --------------------------------------------------------------------------- #


def run_once(session: str = "new_york") -> None:
    """Run one pass over TARGETS for the given session.

    Mirrored from committee_reporter.py's identical function -- only
    "new_york" trades; "asia"/"london" run every spec with trade forced to
    None and record their Decision/Reasoning via _record_session_bias for
    the NY pass to read back (_session_bias_fact, wired into _build_prompt).
    """
    trade_enabled = session == "new_york"
    for spec in TARGETS:
        target = spec.get("target", "?")
        effective_spec = spec if trade_enabled else {**spec, "trade": None}
        try:
            result = run_committee(**effective_spec)
        except Exception:
            logger.exception("run_committee crashed for %s", target)
            try:
                crash_text = f"run_committee raised an unhandled exception for target {target}. Check fundednext_reporter.err.log on the machine for the traceback."
                crash_html = (
                    _status_header_html()
                    + f'<div style="{_EMAIL_FONT}font-size:14px;color:#c62828;">{_esc(crash_text)}</div>'
                )
                send_email(
                    f"[FundedNext] {spec.get('committee', '?')} — {target} (CRASHED)",
                    _status_header() + crash_text,
                    html_body=_wrap_email_html(crash_html),
                )
            except Exception:
                logger.exception("also failed to send the crash notification email")
            continue

        if not trade_enabled and result.status == "success":
            trade_spec = spec.get("trade")
            symbol = trade_spec.get("symbol") if isinstance(trade_spec, dict) else None
            if symbol:
                try:
                    _record_session_bias(symbol, session, result.report_text)
                except Exception:
                    logger.exception("failed to record session bias for %s (%s)", symbol, session)

        if not is_reportable(result):
            logger.info("%s on %s: not reportable, skipping email", result.committee, result.target)
            continue
        tag = "TRADED" if result.traded else ("OK" if result.status == "success" else result.status.upper())
        # session kept OUTSIDE the trailing (tag) parens on purpose --
        # _LOG_EMAILED_RE parses "... (\w+)$" for last_result_tag.
        subject = f"[FundedNext] {session}: {result.committee} — {result.target} ({tag})"
        try:
            html = _wrap_email_html(_status_header_html() + _format_body_html(result, tag))
            send_email(subject, _status_header() + _format_body(result), html_body=html)
        except Exception:
            logger.exception("failed to send report email for %s", target)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

_LOG_RUN_START_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO running (.+)$")
_LOG_EMAILED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO emailed report: \[FundedNext\] (.+?) \((\w+)\)")


def _status_lock_state() -> tuple[bool, int | None]:
    identity = _read_lock_identity()
    if identity is None:
        return False, None
    pid, _ = identity
    return _lock_identity_is_alive(identity), pid


def _status_log_summary() -> dict:
    """Parse the tail of reporter.log for the last pass's timing/outcome.

    "next_due" is no longer derived here -- see committee_reporter.py's
    identical function's docstring; _next_pass_due_text computes it
    directly from the session schedule instead.
    """
    try:
        lines = REPORTER_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    last_start = None
    last_emailed = None
    for line in lines[-500:]:
        m = _LOG_RUN_START_RE.match(line)
        if m:
            last_start = (m.group(1), m.group(2))
        m = _LOG_EMAILED_RE.match(line)
        if m:
            last_emailed = (m.group(1), m.group(2), m.group(3))

    result: dict = {}
    if last_start:
        result["last_start_ts"], result["last_start_desc"] = last_start
    if last_emailed:
        ts_str, desc, tag = last_emailed
        result["last_result_ts"] = ts_str
        result["last_result_desc"] = desc
        result["last_result_tag"] = tag
    return result


def _challenge_progress_lines(account: dict) -> list[str]:
    """The one genuinely new status-report section: challenge start date,
    initial balance, progress-to-target, trading-days-logged, and today's
    remaining daily-loss room — see the plan doc's Step 7.

    Stellar 1-Step is single-phase (see fn_state.CHALLENGE_TARGET_PCT) —
    corrected 2026-09-15 from the Stellar 2-Step two-phase model this was
    originally written for.
    """
    lines: list[str] = ["\nFundedNext challenge progress:"]
    state = fn_state.get_state()
    initial = state.get("initial_balance_usd")
    if not initial:
        lines.append("  Not yet initialized (no pass has run against the live account yet).")
        return lines
    initial = float(initial)
    target_pct = fn_state.CHALLENGE_TARGET_PCT
    passed_date = state.get("passed_date")
    status_note = f"passed as of {passed_date}" if passed_date else f"target {target_pct:.0f}%"
    lines.append(
        f"  Start date: {state.get('challenge_start_date', '?')}  Initial balance: ${initial:.2f}  "
        f"Status: {status_note}"
    )
    try:
        balance = float(account.get("balance") or 0)
        progress = fn_state.progress_pct(balance)
        if progress is not None:
            lines.append(f"  Progress: {progress:+.2f}% toward the {target_pct:.0f}% target (balance ${balance:.2f})")
    except Exception as exc:
        lines.append(f"  could not compute progress: {exc}")

    days = fn_state.trading_days_count()
    lines.append(f"  Trading days logged: {days}/{fn_state.MIN_TRADING_DAYS} minimum (non-consecutive OK)")

    try:
        baseline = fn_guard._read_daily_baseline()
        today = fn_state.server_today()
        if baseline.get("date") == today and baseline.get("equity"):
            baseline_equity = float(baseline["equity"])
            equity = float(account.get("equity") or 0)
            drawdown = (baseline_equity - equity) / baseline_equity if baseline_equity > 0 else 0
            room = fn_guard.DAILY_LOSS_HALT_PCT - drawdown
            lines.append(
                f"  Today's drawdown: {drawdown:.1%} (self-imposed halt at {fn_guard.DAILY_LOSS_HALT_PCT:.0%}, "
                f"room remaining {room:.1%})"
            )
        else:
            lines.append("  Today's drawdown: no baseline recorded yet this server-day")
    except Exception as exc:
        lines.append(f"  could not compute today's drawdown: {exc}")

    try:
        static_dd = (initial - float(account.get("equity") or initial)) / initial if initial > 0 else 0
        static_room = fn_guard.MAX_DRAWDOWN_HALT_PCT - static_dd
        lines.append(
            f"  Life-of-challenge drawdown: {static_dd:.1%} (self-imposed halt at "
            f"{fn_guard.MAX_DRAWDOWN_HALT_PCT:.0%}, room remaining {static_room:.1%})"
        )
    except Exception as exc:
        lines.append(f"  could not compute life-of-challenge drawdown: {exc}")

    return lines


def _build_status_report() -> str:
    lines: list[str] = ["=== FundedNext fundednext_reporter status ==="]
    running, pid = _status_lock_state()
    lines.append(f"Loop: {'RUNNING (pid ' + str(pid) + ')' if running else 'NOT RUNNING' + (f' (stale lock, pid {pid})' if pid else '')}")

    log = _status_log_summary()
    if log.get("last_result_ts"):
        lines.append(f"Last pass: {log['last_result_ts']} -> {log['last_result_desc']} ({log['last_result_tag']})")
    elif log.get("last_start_ts"):
        lines.append(f"Last pass started: {log['last_start_ts']} (still in progress or result not yet logged)")
    else:
        lines.append("Last pass: no fundednext_reporter.log data found")
    lines.append(f"Next pass due: ~{_next_pass_due_text(datetime.now(timezone.utc))}")

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set
    from src.trading.service import get_account, get_open_orders, get_positions

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
            lines.append(
                f"  Balance: {account.get('balance')}  Equity: {account.get('equity')}  "
                f"Margin level: {account.get('margin_level')}"
            )
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
                    f"@ {p.get('price_open')} SL {p.get('stop_loss', '?')} TP {p.get('take_profit', '?')} "
                    f"P&L {p.get('profit')}"
                )
        try:
            pending = get_open_orders(connection).get("open_orders", [])
        except Exception as exc:
            lines.append(f"  could not read pending orders: {exc}")
            pending = []
        if pending:
            for o in pending:
                lines.append(
                    f"  PENDING {o.get('side', '?').upper()} {o.get('quantity')} {o.get('symbol')} "
                    f"{o.get('order_type')} @ {o.get('limit_price')}"
                )
        if connection in LIVE_CONNECTIONS:
            try:
                halted = halt_flag_set(fn_guard.BROKER)
                lines.append(f"  Kill switch: {'TRIPPED' if halted else 'clear'}")
            except Exception as exc:
                lines.append(f"  could not read kill switch state: {exc}")
            lines.extend(_challenge_progress_lines(account))

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


def _status_header() -> str:
    try:
        return _build_status_report() + f"\n\n{'=' * 60}\n\n"
    except Exception:
        logger.exception("status report build failed; this email will omit it")
        return ""


def _status_header_html() -> str:
    """Plain-text status wrapped in a <pre> block for the HTML email — the
    Exness reporter builds a fully separate styled HTML status section;
    given this account's status report is already compact, reusing the text
    version here avoids duplicating _challenge_progress_lines et al. in two
    parallel renderings for a low marginal benefit."""
    try:
        text = _build_status_report()
        return (
            f'<div style="{_EMAIL_FONT}font-size:13px;color:#222;white-space:pre-wrap;">{_esc(text)}</div>'
            '<hr style="border:none;border-top:2px solid #ccc;margin:16px 0;">'
        )
    except Exception:
        logger.exception("HTML status report build failed; this email will omit it")
        return ""


def print_status() -> None:
    print(_build_status_report())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one pass over TARGETS, then exit (default)")
    parser.add_argument("--loop", action="store_true", help="Keep running, one pass at each session boundary (see _next_session_boundary)")
    parser.add_argument(
        "--interval", type=int, default=7200,
        help="Deprecated / no longer used for pass scheduling (kept for CLI backward compatibility only -- "
        "--loop now fires at session boundaries, see --session)",
    )
    parser.add_argument(
        "--session", choices=["asia", "london", "new_york"], default="new_york",
        help="Session for a --once pass (only 'new_york' trades; others are research-only). Ignored in --loop mode, "
        "which determines the session live from the schedule.",
    )
    parser.add_argument("--status", action="store_true", help="Print a consolidated status report and exit (read-only, no lock)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    if not _acquire_singleton_lock():
        logger.error(
            "another fundednext_reporter.py instance is already running (lock at %s) — refusing to "
            "start a second one to avoid racing live trading decisions. If you're certain no instance "
            "is actually running, delete the lock file and retry.",
            LOCK_PATH,
        )
        return 1

    sys.path.insert(0, str(AGENT_DIR))
    from src.providers.llm import _ensure_dotenv

    _ensure_dotenv()

    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        logger.error("missing required environment variables: %s", ", ".join(missing))
        return 1

    if args.loop:
        next_boundary, next_session = _next_session_boundary(datetime.now(timezone.utc))
        logger.info(
            "starting loop mode, session-gated scheduling (only new_york trades; profit "
            "protection checked every %ss) — next scheduled pass: %s (%s)",
            BREAKEVEN_POLL_SECONDS, next_boundary.isoformat(), next_session,
        )
        # See committee_reporter.py's identical loop for the full rationale
        # (mirrored here) -- a fresh start does NOT fire an immediate pass;
        # use `--once --session <s>` for a manual one-off pass instead.
        while True:
            now_utc = datetime.now(timezone.utc)
            if now_utc >= next_boundary:
                session = next_session
                next_boundary, next_session = _next_session_boundary(now_utc)
                if _in_weekend_window(now_utc):
                    logger.info("weekend (UTC) — market closed, skipping this pass")
                    try:
                        _weekend_flatten_and_notify()
                    except Exception:
                        logger.exception("weekend flatten/notify crashed; continuing to the next scheduled pass")
                else:
                    try:
                        run_once(session)
                    except Exception:
                        logger.exception("run_once() crashed; continuing to the next scheduled pass")
                logger.info("next scheduled pass: %s (%s)", next_boundary.isoformat(), next_session)
            else:
                try:
                    _profit_protection_check()
                except Exception:
                    logger.exception("profit protection check crashed; continuing")
            time.sleep(BREAKEVEN_POLL_SECONDS)
    else:
        run_once(args.session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
