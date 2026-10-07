"""Strategy versioning, decision logging, and the hard trend gate (both bots).

Added 2026-09-28 alongside the switch to fx_commodity_day_desk + plain
stop/target exits, so the change can be judged on numbers instead of
impressions:

- STRATEGY_VERSION is stamped on every new journal entry; entries from
  before it exists count as LEGACY_VERSION. weekly_version_report() compares
  versions (trades, win rate, avg win/loss in R, total R and $).
- Every trade-enabled committee pass is appended to DECISION_LOG_PATH with
  its decision (long/short/wait/...) and the mid price at decision time.
  fill_decision_outcomes() later records what price actually did over the
  next OUTCOME_HOURS, so "wait" calls (otherwise invisible) can be judged.
- trend_gate() is the hard trend filter: when H4 and D1 both trend the same
  way, only that direction may be traded.

All of it is advisory bookkeeping except trend_gate, and every function
fails soft -- tracking must never block or crash a trading pass.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market_data_pack as mdp

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agent"
DECISION_LOG_PATH = REPO_ROOT / "logs" / "decision_log.jsonl"
# Both bots' own journal paths (committee_reporter.py / fundednext_reporter.py
# define the same constant locally) -- needed here so the scale rule below can
# pool across both accounts instead of judging each one on its own half-sample.
EXNESS_JOURNAL_PATH = REPO_ROOT / "logs" / "trade_journal.json"
FUNDEDNEXT_JOURNAL_PATH = REPO_ROOT / "logs" / "fundednext_trade_journal.json"

# Bump whenever the committee, data inputs, or exit rules change materially.
# v3 (2026-09-30): the user's prop-firm rulebook (checklist, edge types,
# strict output, net R:R, max stop, bench, 2h news, 0.75%/2% on FundedNext);
# exits deliberately still plain stop+target (see exit_replay rule1_full).
STRATEGY_VERSION = "v3-rulebook-plainexit"
LEGACY_VERSION = "v1-legacy"
OUTCOME_HOURS = 8

# Rulebook 2026-09-30 ("Rule 2" / post-trade review).
BENCH_AFTER_LOSSES = 3
BENCH_HOURS = 24
# An exit within this share of the planned risk distance of the stop or the
# target counts as "exited at the plan's level" (spread/slippage tolerance).
PLAN_LEVEL_TOLERANCE_R = 0.15

# Pre-committed scale/pause criteria for the CURRENT strategy version
# (2026-09-30, agreed before results came in so they aren't decided on mood):
# after SCALE_MIN_TRADES closed trades, total >= SCALE_UP_R -> return
# FundedNext to full size; total <= PAUSE_R at any point -> pause and revisit.
SCALE_MIN_TRADES = 10
# Versions that count as "the new setup" for the scale rule: v2 (FX desk +
# data pack + plain exits + trend gate) and v3 (v2 + rulebook limits) trade
# the same way, so both count toward the same sample.
NEW_SETUP_VERSIONS = {"v2-fxdesk-datapack-plainexit-trendgate", STRATEGY_VERSION}
SCALE_UP_R = 2.0
PAUSE_R = -3.0
# A "wait" followed within OUTCOME_HOURS by a one-way move this large counts
# as a potentially missed trade (evidence for/against adding a London pass).
MISSED_MOVE_PIPS = 30
OPENROUTER_LOW_USD = 5.0

logger = logging.getLogger(__name__)

# The wrapper is asked for a "Decision:" line but doesn't always comply --
# 2026-09-28's first live report used "**Direction:** SHORT" and a markdown
# "| **Side** | SELL (SHORT) |" row instead. Tried in this order.
_DECISION_RES = (
    # Also "### Committee Decision: ..." / "**Final Decision:** ..." / "**Verdict:** ..."
    re.compile(r"^\W*(?:(?:committee|final|pm)\s+)?(?:decision|verdict)\W*:\s*(.+)$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\W*Direction\W*:\s*(.+)$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\|\W*Side\W*\|\s*([^|]+)\|", re.MULTILINE | re.IGNORECASE),
)

# D4 (2026-10-06, extended for v5.1 in D6 2026-10-06): the required fields
# of the v5.1 structured output format both reporters' _REPORT_FORMAT_TRADE
# / _REPORT_FORMAT_RESEARCH_ONLY emit (committee_reporter.py /
# fundednext_reporter.py). "REASON FOR PASS" keeps its literal spaces --
# these are matched against the label exactly as the prompt asks the LLM to
# print it, not a normalized/underscored name.
REQUIRED_COMMITTEE_FIELDS = (
    "DECISION", "CONFIDENCE", "MODE", "DATA_MISSING", "NEWS_API_STATUS", "EDGE", "CHECKLIST", "ORDER",
    "INVALIDATION", "INPUT_PROVENANCE", "PROPOSAL", "REASON FOR PASS",
)


def validate_committee_fields(report_text: str) -> list[str]:
    """Return the REQUIRED_COMMITTEE_FIELDS labels missing from report_text
    (each must appear as "<LABEL>:" at the start of a line, tolerating a
    markdown-bold/punctuation wrapper like "**DECISION:**" -- same \\W*
    prefix tolerance _DECISION_RES already uses above). Empty list means
    every required field is present.

    Real incident 2026-10-07: a live report had all 12 fields present and
    correctly filled in, each one wrapped as "**FIELD: value**" -- the
    original bare `^{field}:` anchor doesn't match through the leading
    "**", so every field registered as missing despite the report being
    entirely well-formed, needlessly tripping run_committee's
    MALFORMED_OUTPUT path. \\W* matches only punctuation/whitespace, never
    a digit or letter, so a numbered checklist line like "5. Reward:Risk
    ..." still can't false-match a field label.

    Deliberately checks presence only, not content -- a field with a junk
    value ("EDGE: idk") is the committee's own problem to answer honestly,
    not something code can validate; this exists to catch the LLM dropping
    a field entirely, which the email/journal pipeline has never been able
    to detect before this (see run_committee's MALFORMED_OUTPUT handling).
    """
    missing = []
    for field in REQUIRED_COMMITTEE_FIELDS:
        if not re.search(rf"^\W*{re.escape(field)}:", report_text or "", re.MULTILINE):
            missing.append(field)
    return missing


# --------------------------------------------------------------------------- #
# Decision parsing / logging
# --------------------------------------------------------------------------- #

def parse_decision(report_text: str) -> str:
    """Classify the report's 'Decision:' line as long / short / wait / unknown."""
    match = next((m for m in (r.search(report_text or "") for r in _DECISION_RES) if m), None)
    if not match:
        return "unknown"
    # The FIRST action word wins: "wait; would go long above 1.1400" is a
    # wait, "short EURUSD, wait for a retest to add" is a short.
    first = re.search(r"\b(long|buy|short|sell|wait|hold|no trade|stand aside|flat|pass)\b",
                      match.group(1).lower())
    if not first:
        return "unknown"
    word = first.group(1)
    if word in ("long", "buy"):
        return "long"
    if word in ("short", "sell"):
        return "short"
    return "wait"


def _append(entry: dict) -> None:
    DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DECISION_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def read_decisions() -> list[dict]:
    try:
        lines = DECISION_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _write_decisions(entries: list[dict]) -> None:
    DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DECISION_LOG_PATH.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    tmp.replace(DECISION_LOG_PATH)


def _mid_price(symbol: str, connection: str) -> float | None:
    try:
        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.service import get_quote

        q = get_quote(symbol, connection).get("quote") or {}
        return (float(q["bid"]) + float(q["ask"])) / 2
    except Exception:
        return None


def record_decision(bot: str, symbol: str, connection: str, *, decision: str, traded: bool,
                    status: str, note: str = "", now: datetime | None = None) -> None:
    """Append one trade-enabled pass's decision. Never raises."""
    try:
        _append({
            "ts": (now or datetime.now(timezone.utc)).isoformat(),
            "bot": bot, "version": STRATEGY_VERSION, "symbol": symbol, "connection": connection,
            "decision": decision, "traded": traded, "status": status, "note": note[:300],
            "price": _mid_price(symbol, connection),
        })
    except Exception:
        logger.exception("decision log: failed to record %s %s", bot, symbol)


def fill_decision_outcomes(bot: str, server_tz, now: datetime | None = None) -> int:
    """Record the OUTCOME_HOURS price path for this bot's due decisions.

    `server_tz` converts MT5 bar times (the broker's wall clock labelled as
    UTC) back to true UTC -- timezone.utc for Exness, EET/EEST for
    FundedNext. Returns how many entries were filled. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    try:
        entries = read_decisions()
    except Exception:
        return 0
    filled = 0
    for e in entries:
        if e.get("bot") != bot or "outcome" in e or e.get("price") is None:
            continue
        try:
            t0 = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        t1 = t0 + timedelta(hours=OUTCOME_HOURS)
        if now < t1:
            continue
        try:
            mt5_sdk, config = mdp._config_for(e["connection"])
            bars = mt5_sdk.get_historical_bars_range(
                e["symbol"], t0 - timedelta(hours=4), t1 + timedelta(hours=4), config=config, period="5m",
            )["bars"]
        except Exception:
            continue
        window = []
        for b in bars:
            try:
                bt = datetime.fromisoformat(str(b["time"])).replace(tzinfo=None).replace(tzinfo=server_tz)
            except (KeyError, ValueError):
                continue
            if t0 <= bt.astimezone(timezone.utc) < t1 and b.get("high") is not None:
                window.append(b)
        if not window:
            e["outcome"] = {"note": "no bars in window (market closed?)"}
            filled += 1
            continue
        # Matches market_data_pack.build_data_pack's digits/pip convention
        # (digits=2 for XAU/XAG -> pip=0.01) -- this used to use 0.1 for XAU,
        # a 10x-too-large pip that would silently understate gold's
        # close/max-up/max-down pip outcomes by 10x the moment gold trading
        # (currently paused in both bots' TARGETS) resumes.
        pip = 0.01 if e["symbol"][3:6] == "JPY" or e["symbol"][:3] in ("XAU", "XAG") else 0.0001
        p0 = e["price"]
        e["outcome"] = {
            "hours": OUTCOME_HOURS,
            "close_move_pips": round((window[-1]["close"] - p0) / pip, 1),
            "max_up_pips": round((max(b["high"] for b in window) - p0) / pip, 1),
            "max_down_pips": round((p0 - min(b["low"] for b in window)) / pip, 1),
        }
        filled += 1
    if filled:
        try:
            _write_decisions(entries)
        except Exception:
            logger.exception("decision log: failed to write outcomes")
            return 0
    return filled


# --------------------------------------------------------------------------- #
# Hard trend gate
# --------------------------------------------------------------------------- #

def trend_gate(symbol: str, connection: str) -> tuple[set[str] | None, str]:
    """Allowed order sides from H4 + D1 trend, or (None, reason) if unrestricted.

    Both 'up' -> only buy; both 'down' -> only sell; anything else (mixed,
    disagreeing, or unreadable) -> no restriction. Fails OPEN: a data error
    must not silently turn into a trading ban.
    """
    try:
        mt5_sdk, config = mdp._config_for(connection)
        labels = {}
        for period, label in (("4h", "H4"), ("1d", "D1")):
            closes = [b["close"] for b in mdp._bars(mt5_sdk, config, symbol, period, mdp.TREND_BARS)]
            labels[label] = mdp.trend_label(closes)
    except Exception as exc:
        return None, f"trend gate unavailable ({exc}); no direction restriction"
    if labels["H4"] == labels["D1"] == "up":
        return {"buy"}, "H4 and D1 are both in an UPTREND: only BUY (long) orders are allowed this pass"
    if labels["H4"] == labels["D1"] == "down":
        return {"sell"}, "H4 and D1 are both in a DOWNTREND: only SELL (short) orders are allowed this pass"
    return None, f"H4 {labels['H4']}, D1 {labels['D1']}: no direction restriction"


def trend_rule_prompt(allowed: set[str], reason: str) -> str:
    side = next(iter(allowed))
    forbidden = "long/buy" if side == "sell" else "short/sell"
    return (
        f"HARD TREND RULE (enforced in code): {reason}. If the final decision is {forbidden}, do NOT "
        f"call trading_place_order -- report the order as not placed because of the trend rule. Any "
        f"{forbidden} order placed anyway is closed automatically the moment it fills.\n\n"
    )


# --------------------------------------------------------------------------- #
# Rulebook: post-trade review, symbol bench, repeat-deviation pass
# --------------------------------------------------------------------------- #

_NEXT_TIME = {
    "none": "No change: the plan was followed; judge the setup, not the outcome.",
    "early_exit": "Size the stop/target so the trade can resolve within the 40h hold and before Friday 20:00 UTC.",
    "stop_adjusted": "Place the stop outside the noise floor (1.5x 15m ATR / spread) at entry.",
    "unknown_exit": "Check the broker history: the exit could not be matched to the plan.",
}


def review_closed_trade(entry: dict) -> dict:
    """Deterministic 3-line review of a closed journal entry vs its own plan.

    Deviation is one of: none (exited at the planned stop or target),
    stop_adjusted (the stop-floor check had to move the committee's stop
    after the fill), early_exit (closed away from both levels: time stop,
    weekend flatten, trend gate, manual), unknown_exit (no exit price).
    """
    try:
        e, sl, tp = float(entry["entry_price"]), float(entry["stop_loss"]), float(entry["take_profit"])
        exit_price = float(entry["exit_price"])
    except (KeyError, TypeError, ValueError):
        deviation = "unknown_exit"
    else:
        risk = abs(e - sl) or 1e-9
        tol = PLAN_LEVEL_TOLERANCE_R * risk
        at_plan = abs(exit_price - sl) <= tol or abs(exit_price - tp) <= tol
        deviation = "stop_adjusted" if entry.get("stop_adjusted") else ("none" if at_plan else "early_exit")
    followed = deviation == "none"
    where = {"none": "none", "stop_adjusted": "stop (moved by the post-fill floor check)",
             "early_exit": "management (exited before the planned stop/target)",
             "unknown_exit": "unknown (exit not reconciled)"}[deviation]
    return {
        "deviation": deviation,
        "lines": [f"1. Followed the plan: {'Yes' if followed else 'No'}",
                  f"2. Deviation: {where}",
                  f"3. Next time: {_NEXT_TIME[deviation]}"],
    }


def _closed_for(journal: list[dict], symbol: str) -> list[dict]:
    closed = [t for t in journal if t.get("symbol") == symbol and t.get("status") == "closed"]
    return sorted(closed, key=lambda t: str(t.get("closed_at") or t.get("opened_at") or ""))


def bench_reason(journal: list[dict], symbol: str, now: datetime | None = None) -> str | None:
    """Rule 2: bench a symbol for BENCH_HOURS after BENCH_AFTER_LOSSES straight losses."""
    now = now or datetime.now(timezone.utc)
    last = _closed_for(journal, symbol)[-BENCH_AFTER_LOSSES:]
    if len(last) < BENCH_AFTER_LOSSES or any(t.get("outcome") != "loss" for t in last):
        return None
    try:
        closed_at = datetime.fromisoformat(str(last[-1].get("closed_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    if now - closed_at >= timedelta(hours=BENCH_HOURS):
        return None
    return (f"[BENCHED] {symbol}: last {BENCH_AFTER_LOSSES} closed trades were all losses, the latest "
            f"closed {closed_at.strftime('%Y-%m-%d %H:%M')} UTC -- benched for {BENCH_HOURS}h (rulebook Rule 2).")


def consume_repeat_deviation(journal: list[dict], symbol: str) -> str | None:
    """Post-trade review rule: same deviation on the last two closes -> PASS the next setup once.

    Mutates `journal` (marks the latest review as served) so the caller can
    persist it; returns the skip note, or None.
    """
    last = _closed_for(journal, symbol)[-2:]
    if len(last) < 2 or not all(isinstance(t.get("review"), dict) for t in last):
        return None
    d1, d2 = last[0]["review"].get("deviation"), last[1]["review"].get("deviation")
    if d1 != d2 or d2 in (None, "none") or last[1]["review"].get("pass_served"):
        return None
    last[1]["review"]["pass_served"] = True
    return (f"[REVIEW PASS] {symbol}: the same deviation ({d2}) appeared on the last two closed trades -- "
            f"passing this setup (rulebook post-trade review).")


# --------------------------------------------------------------------------- #
# Weekly version comparison
# --------------------------------------------------------------------------- #

def _trade_r(entry: dict) -> float | None:
    try:
        risk = abs(float(entry["entry_price"]) - float(entry["stop_loss"]))
        sign = 1 if entry["side"] == "buy" else -1
        return (float(entry["exit_price"]) - float(entry["entry_price"])) * sign / risk if risk else None
    except (KeyError, TypeError, ValueError):
        return None


def pooled_scale_status() -> tuple[int, float]:
    """(n_trades, total_R) for NEW_SETUP_VERSIONS, pooled across BOTH bots' journals.

    scale_verdict's own n/total used to come from whichever single journal
    called weekly_version_report, so each bot judged the scale rule against
    its own half of the sample (e.g. 2 trades each) instead of the pooled
    figure the rule was actually agreed on. Reads both journal files
    directly rather than taking one as a parameter, since the whole point is
    to see across the account boundary. Fails soft (0, 0.0) per unreadable
    file -- a missing/corrupt journal must not crash the prompt this feeds.
    """
    n, total = 0, 0.0
    for path in (EXNESS_JOURNAL_PATH, FUNDEDNEXT_JOURNAL_PATH):
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for t in journal:
            if t.get("status") == "closed" and t.get("strategy_version") in NEW_SETUP_VERSIONS:
                r = _trade_r(t)
                if r is not None:
                    n += 1
                    total += r
    return n, total


def strategic_context_prompt() -> str:
    """Pre-pass prompt block restating the pre-committed scale/pause rule and
    today's live pooled standing, so the committee is told -- every pass, with
    the current numbers, not a snapshot that goes stale -- to execute the
    locked playbook rather than improvise while the sample is still small.
    """
    n, total = pooled_scale_status()
    hit = n >= SCALE_MIN_TRADES and total >= SCALE_UP_R
    paused = total <= PAUSE_R
    if paused or hit:
        # The rule has actually fired -- scale_verdict's own line (surfaced in
        # the weekly email) is the right signal to act on, not this per-pass
        # reminder to hold the line.
        return ""
    return (
        f"STRATEGIC CONTEXT: this strategy version is in a pre-registered observation window. "
        f"Scale rule (pooled across both accounts): at {SCALE_MIN_TRADES}+ closed trades, "
        f"<= {PAUSE_R:+.0f}R -> pause and revisit; >= {SCALE_UP_R:+.0f}R -> scale up. Current pooled "
        f"sample: {n} trades, {total:+.2f}R -- neither trigger hit. Therefore: do NOT change entry logic, "
        f"exit logic, filters, or sizing this pass -- execute the existing playbook consistently, don't "
        f"innovate. If you see a genuine improvement, say so as a PROPOSAL (do not ship it) rather than "
        f"acting on it now.\n\n"
    )


def weekly_version_report(journal: list[dict], bot: str, decisions: list[dict] | None = None) -> str:
    """Plain-text per-version comparison of closed trades and logged decisions."""
    decisions = read_decisions() if decisions is None else decisions
    by_version: dict[str, list[dict]] = {}
    for t in journal:
        if t.get("status") == "closed":
            by_version.setdefault(t.get("strategy_version") or LEGACY_VERSION, []).append(t)
    lines = ["Performance by strategy version (closed trades, all time):"]
    if not by_version:
        lines.append("  no closed trades yet")
    for version, trades in sorted(by_version.items()):
        rs = [r for r in (_trade_r(t) for t in trades) if r is not None]
        usd = sum(float(t.get("profit") or 0) for t in trades)
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        lines.append(
            f"  {version}: {len(trades)} trades, win rate {100 * len(wins) / len(rs):.0f}%, "
            f"avg win {sum(wins) / len(wins) if wins else 0:+.2f}R, "
            f"avg loss {sum(losses) / len(losses) if losses else 0:+.2f}R, "
            f"total {sum(rs):+.2f}R / ${usd:+.2f}" if rs else f"  {version}: {len(trades)} trades (no R data)"
        )
    current = [t for t in journal if t.get("status") == "closed" and t.get("strategy_version") in NEW_SETUP_VERSIONS]
    lines.append(scale_verdict(*pooled_scale_status()))
    by_trend: dict[str, list[float]] = {}
    for t in current:
        r = _trade_r(t)
        if r is not None:
            by_trend.setdefault(t.get("trend_alignment") or "unrecorded", []).append(r)
    if by_trend:
        lines.append("New setup by H4/D1 trend at entry: " + "; ".join(
            f"{k} {len(v)} trades {sum(v):+.2f}R" for k, v in sorted(by_trend.items())))
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    reviewed = [t for t in journal if t.get("status") == "closed" and isinstance(t.get("review"), dict)
                and str(t.get("closed_at") or "") >= week_ago]
    if reviewed:
        lines.append("Post-trade reviews this week:")
        for t in reviewed:
            lines.append(f"  {t.get('symbol')} {t.get('side')} closed {str(t.get('closed_at'))[:16]} "
                         f"({t.get('outcome')}, {float(t.get('profit') or 0):+.2f}):")
            lines += [f"    {line}" for line in t["review"].get("lines", [])]
    mine = [d for d in decisions if d.get("bot") == bot]
    if mine:
        counts: dict[str, int] = {}
        for d in mine:
            counts[d.get("decision", "unknown")] = counts.get(d.get("decision", "unknown"), 0) + 1
        lines.append("Committee decisions logged (trade-enabled passes): "
                     + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        waits = [d for d in mine if d.get("decision") == "wait" and isinstance(d.get("outcome"), dict)
                 and "max_up_pips" in d["outcome"]]
        if waits:
            ups = [d["outcome"]["max_up_pips"] for d in waits]
            downs = [d["outcome"]["max_down_pips"] for d in waits]
            missed = sum(max(u, dn) >= MISSED_MOVE_PIPS for u, dn in zip(ups, downs))
            lines.append(
                f"'Wait' calls with {OUTCOME_HOURS}h outcomes: {len(waits)}; price then ran on average "
                f"{sum(ups) / len(ups):.0f} pips up / {sum(downs) / len(downs):.0f} pips down. "
                f"{missed} of {len(waits)} were followed by a {MISSED_MOVE_PIPS}+ pip one-way move "
                f"(if that stays above ~1 in 3 over 2-3 weeks, a London-open trading pass is worth testing)."
            )
    balance = openrouter_balance_usd()
    if balance is not None:
        lines.append(f"OpenRouter balance: ${balance:.2f}" + (
            f" -- LOW: top up and enable auto top-up at https://openrouter.ai/settings/credits"
            if balance < OPENROUTER_LOW_USD else " (auto top-up at https://openrouter.ai/settings/credits recommended)"))
    return "\n".join(lines)


def scale_verdict(n_trades: int, total_r: float) -> str:
    """The pre-committed scale/pause rule for the current version, as one line."""
    head = f"Scale rule (new setup, v2+v3): {n_trades} closed trades, {total_r:+.2f}R -> "
    if total_r <= PAUSE_R:
        return head + f"PAUSE and revisit (at or below {PAUSE_R:+.0f}R)."
    if n_trades >= SCALE_MIN_TRADES and total_r >= SCALE_UP_R:
        return head + "SCALE UP: return FundedNext to full size (0.24 EURUSD / 0.30 GBPUSD)."
    if n_trades >= SCALE_MIN_TRADES:
        return head + f"hold current size (needs {SCALE_UP_R:+.0f}R after {SCALE_MIN_TRADES} trades to scale up)."
    return head + f"keep collecting ({n_trades}/{SCALE_MIN_TRADES} trades before any size change)."


def openrouter_balance_usd() -> float | None:
    """Remaining OpenRouter credit (same /credits endpoint as the reporter's alert); None on any failure."""
    import os
    import urllib.request

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))["data"]
        return float(data["total_credits"]) - float(data["total_usage"])
    except Exception:
        return None
