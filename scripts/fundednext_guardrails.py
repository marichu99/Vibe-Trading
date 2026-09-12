"""FundedNext-specific hard risk/compliance guardrails (code, not prompt text).

This is the FundedNext sibling of ``committee_reporter.py``'s
``_live_circuit_breaker_check``, split into distinct checks because the
FundedNext account has THREE independent limits with genuinely different
anchor semantics (see each function's docstring): a same-day loss limit that
resets every server-day, a static overall-drawdown floor that never moves for
the life of the challenge, and a per-trade risk cap sized as a fraction of
CURRENT balance rather than a flat dollar figure. All three sit ON TOP OF
(not instead of) the committed ``mt5fn`` mandate's own hard caps
(``scripts/commit_fundednext_mandate.py``) — this module's thresholds are
self-imposed, tighter than what FundedNext itself allows, to leave margin for
slippage/spread/swap before the account actually breaches FundedNext's own
rule.

OPEN ITEMS — verify before relying on this at real money (see
``C:\\Users\\Hp\\.claude\\plans\\calm-wondering-snail.md``):
  1. Server-day timezone: ``fundednext_state.server_today()`` assumes
     GMT+3 DST / GMT+2 standard (Europe/Bucharest) for FundedNext's server
     time. Verify against the real MT5 terminal's displayed time.
  2. Swap-in-equity: this module trusts MT5's own ``equity`` figure to
     already net in swap charges (both floating and realized), since
     FundedNext's real daily-loss rule counts swap. Verify against a real
     account holding a position overnight before trusting this blind.
  3. Static-drawdown-floor "never trails up": sources disagree on whether
     FundedNext's 10% max-drawdown floor stays pinned to the INITIAL balance
     forever, or ratchets up as profit is banked. This module implements the
     conservative (never-moves) reading — re-verify against the actual
     purchased challenge's contract/PDF terms, especially once the account
     has a meaningful profit cushion (exactly when a wrong assumption here
     would matter).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import fundednext_state as fn_state

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"

logger = logging.getLogger("fundednext_guardrails")

# Distinct broker key from the Exness "mt5" mandate/halt/daily-count — see
# agent/src/trading/service.py's _SDK_CONNECTOR_MODULES/_order_classification
# and agent/src/trading/connectors/mt5/profiles.py's mt5fn-live-trade profile.
BROKER = "mt5fn"

# Self-imposed, tighter than FundedNext's own 5% daily / 10% overall limits —
# see module docstring.
DAILY_LOSS_HALT_PCT = 0.04
MAX_DRAWDOWN_HALT_PCT = 0.08

# 1% of CURRENT balance, matching FundedNext's own imposable "1% max risk per
# trade" rule (not a portfolio-aggregate split like the Exness account's
# _effective_max_loss_usd — see effective_max_loss_usd's own docstring).
RISK_PER_TRADE_FRACTION = 0.01

# Mirrors scripts/commit_fundednext_mandate.py's MAX_LOSS_PER_ORDER_USD ($60
# = 1% of the $6,000 starting balance). These are two INDEPENDENTLY
# maintained numbers (same caveat as the Exness mandate/committee_reporter.py
# pair) — re-derive and re-commit both as balance grows.
MANDATE_MAX_LOSS_PER_ORDER_USD = 60.0

# Self-imposed daily trade-count ceiling, well under FundedNext's 200/day
# hyperactivity threshold — a soft check the reporter uses to skip attempting
# a trade (and log why) before ever calling trading_place_order, on top of
# the mandate's own hard max_trades_per_day ceiling enforced at the gate.
MAX_DAILY_TRADES_SOFT_CEILING = 10

# Own file — never shares committee_reporter.py's LIVE_BASELINE_PATH, even
# though the two reporters run on separate machines (hygiene against any
# future consolidation onto one box).
DAILY_BASELINE_PATH = REPO_ROOT / "logs" / "fundednext_daily_baseline.json"


def _read_daily_baseline() -> dict:
    try:
        return json.loads(DAILY_BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_daily_baseline(data: dict) -> None:
    DAILY_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAILY_BASELINE_PATH.write_text(json.dumps(data), encoding="utf-8")


def effective_max_loss_usd(connection: str, mandate_ceiling: float = MANDATE_MAX_LOSS_PER_ORDER_USD) -> float:
    """This pass's per-order risk budget: min(mandate_ceiling, 1% of CURRENT balance).

    Unlike the Exness account's ``_effective_max_loss_usd`` (a portfolio-
    aggregate split across concurrent trades), FundedNext's own imposable
    rule is a flat 1%-of-balance-per-trade cap, so that's what this mirrors
    directly. Uses BALANCE, not equity — a stable number that doesn't shrink
    the very budget being computed just because an existing position is
    temporarily underwater. Fails open to ``mandate_ceiling`` on any read
    error, same convention as the Exness version — a stale/wider prompt-side
    budget is still safe, since it can only ask for a stop up to what the
    mandate gate would allow anyway, never past it.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_account

    try:
        balance = float(get_account(connection)["account"]["balance"])
    except Exception:
        return mandate_ceiling
    if balance <= 0:
        return mandate_ceiling
    return min(mandate_ceiling, balance * RISK_PER_TRADE_FRACTION)


def daily_trade_count_check(limit: int = MAX_DAILY_TRADES_SOFT_CEILING) -> str | None:
    """Soft ceiling: skip attempting a trade this pass if today's order count
    already meets ``limit`` — well under FundedNext's 200/day hyperactivity
    threshold. On top of (not instead of) the mandate's own hard
    max_trades_per_day, enforced at the gate regardless of this check.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.live.daily_count import read_daily_count

    count = read_daily_count(BROKER)
    if count < limit:
        return None
    return (
        f"[FUNDEDNEXT TRADE-COUNT GUARD] today's order count ({count}) has reached the "
        f"self-imposed ceiling of {limit}/day (well under FundedNext's own 200/day "
        f"hyperactivity threshold) — skipping any further trade attempt this pass."
    )


def daily_loss_check(equity: float) -> str | None:
    """Same-server-day loss check: self-imposed DAILY_LOSS_HALT_PCT vs.
    FundedNext's 5% daily loss limit.

    Anchored to an equity snapshot taken at the first check of each
    server-day (see fundednext_state.server_today — see module docstring's
    open item 1 on the timezone assumption), NOT UTC midnight like the
    Exness circuit breaker — FundedNext's rule resets on ITS server day, and
    equity is used (not balance) because the real rule counts floating P&L
    too (see module docstring's open item 2 on swap being netted into it).
    """
    today = fn_state.server_today()
    baseline = _read_daily_baseline()
    if baseline.get("date") != today:
        baseline = {"date": today, "equity": equity}
        _write_daily_baseline(baseline)
    baseline_equity = float(baseline.get("equity") or equity)
    if baseline_equity <= 0:
        return None
    drawdown = (baseline_equity - equity) / baseline_equity
    if drawdown < DAILY_LOSS_HALT_PCT:
        return None
    return (
        f"same-server-day equity drawdown {drawdown:.1%} (baseline ${baseline_equity:.2f} -> "
        f"${equity:.2f}) reached the self-imposed {DAILY_LOSS_HALT_PCT:.0%} daily-loss halt "
        f"(FundedNext's own limit is 5%)"
    )


def static_drawdown_check(equity: float) -> str | None:
    """Life-of-challenge check: self-imposed MAX_DRAWDOWN_HALT_PCT vs.
    FundedNext's 10% STATIC max drawdown.

    Anchored to ``initial_balance_usd`` from fundednext_state — a one-time
    snapshot taken on this account's first-ever run, NEVER re-baselined
    (unlike daily_loss_check above). A trip here means the challenge is
    likely breached outright, not a same-day pause — see module docstring's
    open item 3 on whether this floor genuinely never trails up.
    """
    state = fn_state.get_state()
    initial = state.get("initial_balance_usd")
    if not initial:
        return None  # not initialized yet -- nothing to compare against
    initial = float(initial)
    if initial <= 0:
        return None
    drawdown = (initial - equity) / initial
    if drawdown < MAX_DRAWDOWN_HALT_PCT:
        return None
    return (
        f"life-of-challenge equity drawdown {drawdown:.1%} (initial balance ${initial:.2f} -> "
        f"${equity:.2f}) reached the self-imposed {MAX_DRAWDOWN_HALT_PCT:.0%} static max-drawdown "
        f"halt (FundedNext's own static limit is 10%) — the challenge is likely breached"
    )


def _flatten_positions(connection: str, magic: int, live_symbols: set[str]) -> list[str]:
    """Close every one of OUR positions on ``connection`` in ``live_symbols``.

    Same all-TARGETS-on-this-connection + magic-filtered pattern as
    committee_reporter.py's _live_circuit_breaker_check/
    _weekend_flatten_and_notify — a portfolio-level trip must not leave other
    live symbols' exposure open just because only one target's pass noticed.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.profiles import profile_by_id
    from src.trading.service import get_positions

    closed: list[str] = []
    try:
        positions = get_positions(connection).get("positions", [])
        ours = [p for p in positions if p.get("symbol") in live_symbols and p.get("magic") == magic]
        if ours:
            profile = profile_by_id(connection)
            config = mt5_sdk.build_config(profile.config, {})
            for pos in ours:
                ticket = pos.get("ticket")
                result = mt5_sdk.close_position(config, ticket=ticket)
                closed.append(f"{pos.get('symbol')} ticket {ticket}: {result.get('status')}")
    except Exception as exc:
        closed.append(f"flatten attempt raised: {exc}")
    return closed


def guardrail_check(trade: dict, live_symbols: set[str], magic: int) -> str | None:
    """Run every FundedNext guardrail for this pass; trip + flatten on a breach.

    Returns a message to fold into the emailed report (and to signal the
    caller to run this pass research-only) when trading was skipped or
    halted, or ``None`` when normal trading may proceed this pass. Mirrors
    ``_live_circuit_breaker_check``'s calling contract exactly so
    ``fundednext_reporter.py``'s ``run_committee`` can use it as a drop-in
    replacement.

    Check order: kill switch already tripped -> daily trade-count soft
    ceiling -> same-day loss limit -> life-of-challenge static drawdown. The
    first one that fires short-circuits the rest (an already-halted account
    has nothing left to check; a soft trade-count skip doesn't need an
    account read at all).
    """
    connection = trade["connection"]
    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set, trip_halt
    from src.trading.service import get_account

    if halt_flag_set(BROKER):
        return (
            f"[FUNDEDNEXT CIRCUIT BREAKER] {BROKER} trading is currently HALTED "
            f"(kill switch already tripped) — no order was attempted this pass."
        )

    count_note = daily_trade_count_check()
    if count_note:
        return count_note

    fn_state.ensure_initialized(connection)

    try:
        equity = float(get_account(connection)["account"]["equity"])
    except Exception as exc:
        return (
            f"[FUNDEDNEXT CIRCUIT BREAKER] could not read live account equity this pass "
            f"({exc}) — skipping trading as a precaution."
        )

    reason = daily_loss_check(equity)
    severity = "daily"
    if reason is None:
        reason = static_drawdown_check(equity)
        severity = "static"
    if reason is None:
        return None

    trip_halt(by="cli", reason=reason, broker=BROKER)
    closed = _flatten_positions(connection, magic, live_symbols)
    flatten_note = "; ".join(closed) if closed else "no open position found"

    if severity == "daily":
        return (
            f"[FUNDEDNEXT DAILY-LOSS HALT TRIPPED] {reason}. Trading for {BROKER} is now HALTED "
            f"for the rest of the server-day (kill switch) until manually cleared "
            f"(src.live.halt.clear_halt). Flatten attempt: {flatten_note}."
        )
    return (
        f"[FUNDEDNEXT MAX-DRAWDOWN HALT TRIPPED — CHALLENGE LIKELY BREACHED] {reason}. Trading "
        f"for {BROKER} is now HALTED (kill switch) until manually cleared "
        f"(src.live.halt.clear_halt). Flatten attempt: {flatten_note}."
    )
