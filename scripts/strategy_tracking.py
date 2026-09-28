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

# Bump whenever the committee, data inputs, or exit rules change materially.
STRATEGY_VERSION = "v2-fxdesk-datapack-plainexit-trendgate"
LEGACY_VERSION = "v1-legacy"
OUTCOME_HOURS = 8

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
        pip = 0.01 if e["symbol"][3:6] == "JPY" else (0.1 if e["symbol"][:3] in ("XAU",) else 0.0001)
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
# Weekly version comparison
# --------------------------------------------------------------------------- #

def _trade_r(entry: dict) -> float | None:
    try:
        risk = abs(float(entry["entry_price"]) - float(entry["stop_loss"]))
        sign = 1 if entry["side"] == "buy" else -1
        return (float(entry["exit_price"]) - float(entry["entry_price"])) * sign / risk if risk else None
    except (KeyError, TypeError, ValueError):
        return None


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
            lines.append(
                f"'Wait' calls with {OUTCOME_HOURS}h outcomes: {len(waits)}; price then ran on average "
                f"{sum(ups) / len(ups):.0f} pips up / {sum(downs) / len(downs):.0f} pips down "
                f"(large one-sided runs after waits = missed trades)."
            )
    return "\n".join(lines)
