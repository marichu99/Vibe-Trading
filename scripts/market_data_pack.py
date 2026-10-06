"""Verified market data pack for the FX committee (shared by both reporters).

Added 2026-09-28. Before this, every committee sub-agent fetched its own
prices by writing and running scripts (the bulk of each pass's ~$0.80 LLM
cost), and the swarm's automatic "grounding" step promoted ordinary words
in the prompt (ATR, LIVE, NOW, TASK, NEXT) to US stock tickers and fed the
agents those unrelated stocks' bars as if they were verified data --
EURUSD itself was never grounded at all. This module computes one compact,
deterministic pack from the traded broker's own MT5 feed and writes it to
a file every agent reads first (see fx_commodity_day_desk.yaml).

Everything here is pure code (no LLM) and fails soft: any section whose
data can't be read is omitted with a note, never raised, so a data hiccup
can only make the pack smaller -- it can never block a pass.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agent"
# Under agent/runs so the swarm's read_file tool is allowed to open it
# (src.tools.path_utils._default_file_roots).
DATA_PACK_DIR = AGENT_DIR / "runs" / "data_packs"
DATA_PACK_RETENTION_DAYS = 14

logger = logging.getLogger(__name__)

TIMEFRAMES = (("15m", "M15"), ("1h", "H1"), ("4h", "H4"), ("1d", "D1"))
TREND_BARS = 120
SWING_LOOKBACK_H1_BARS = 72

# ICE US Dollar Index weights (the published DXY formula).
_DXY_CONSTANT = 50.14348112
_DXY_WEIGHTS = (("EURUSD", -0.576), ("USDJPY", 0.136), ("GBPUSD", -0.119),
                ("USDCAD", 0.091), ("USDSEK", 0.042), ("USDCHF", 0.036))


# --------------------------------------------------------------------------- #
# Pure indicator math (unit-tested without MT5)
# --------------------------------------------------------------------------- #

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = [max(bars[i]["high"] - bars[i]["low"], abs(bars[i]["high"] - bars[i - 1]["close"]),
               abs(bars[i]["low"] - bars[i - 1]["close"])) for i in range(1, len(bars))]
    return sum(trs[-period:]) / period


def trend_label(closes: list[float]) -> str:
    """'up' / 'down' / 'mixed' from price vs EMA20/EMA50 and EMA20's 5-bar slope."""
    if len(closes) < 55:
        return "n/a"
    e20, e50 = ema(closes, 20), ema(closes, 50)
    price, rising = closes[-1], e20[-1] > e20[-6]
    if price > e20[-1] > e50[-1] and rising:
        return "up"
    if price < e20[-1] < e50[-1] and not rising:
        return "down"
    return "mixed"


def swing_levels(bars: list[dict], width: int = 2) -> tuple[list[float], list[float]]:
    """Fractal swing highs/lows: a bar whose high (low) beats `width` bars on each side."""
    highs, lows = [], []
    for i in range(width, len(bars) - width):
        window = bars[i - width:i + width + 1]
        if bars[i]["high"] == max(b["high"] for b in window):
            highs.append(bars[i]["high"])
        if bars[i]["low"] == min(b["low"] for b in window):
            lows.append(bars[i]["low"])
    return highs, lows


def synthetic_dxy(closes_by_pair: dict[str, list[float]]) -> list[float]:
    """DXY series from aligned component closes (same length, oldest first)."""
    n = min(len(v) for v in closes_by_pair.values())
    series = []
    for i in range(-n, 0):
        value = _DXY_CONSTANT
        for pair, weight in _DXY_WEIGHTS:
            value *= closes_by_pair[pair][i] ** weight
        series.append(value)
    return series


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


# --------------------------------------------------------------------------- #
# MT5 access
# --------------------------------------------------------------------------- #

def _config_for(connection: str):
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.profiles import profile_by_id

    return mt5_sdk, mt5_sdk.build_config(profile_by_id(connection).config, {})


def _bars(mt5_sdk, config, symbol: str, period: str, limit: int) -> list[dict]:
    rows = mt5_sdk.get_historical_bars(symbol, config=config, period=period, limit=limit)["bars"]
    return [b for b in rows if b.get("high") is not None and b.get("low") is not None and b.get("close") is not None]


def _fmt(price: float, digits: int) -> str:
    return f"{price:.{digits}f}"


# --------------------------------------------------------------------------- #
# Pack builder
# --------------------------------------------------------------------------- #

def build_data_pack(symbol: str, connection: str, *, now: datetime | None = None) -> str:
    """Return the markdown data pack for `symbol` on `connection`."""
    now = now or datetime.now(timezone.utc)
    suffix = symbol[6:] if len(symbol) > 6 else ""
    base, quote_ccy = symbol[:3], symbol[3:6]
    digits = 2 if base in ("XAU", "XAG") else (3 if quote_ccy == "JPY" else 5)
    pip = 0.01 if digits <= 3 else 0.0001

    lines = [
        f"# DATA PACK — {symbol} ({connection})",
        f"Built {now.strftime('%Y-%m-%d %H:%M')} UTC from the traded broker's own MT5 feed. "
        f"These numbers are verified ground truth for this decision: use them instead of "
        f"downloading prices, and never contradict them with data from another source.",
        "",
    ]

    try:
        mt5_sdk, config = _config_for(connection)
    except Exception as exc:  # pragma: no cover - environment failure
        return "\n".join(lines + [f"(data pack unavailable: {exc})"])

    # Live quote
    try:
        from src.trading.service import get_quote

        q = get_quote(symbol, connection).get("quote") or {}
        bid, ask = float(q["bid"]), float(q["ask"])
        lines += ["## Live quote",
                  f"bid {_fmt(bid, digits)} / ask {_fmt(ask, digits)} — spread {(ask - bid) / pip:.1f} pips", ""]
        price = (bid + ask) / 2
    except Exception as exc:
        lines += ["## Live quote", f"(unavailable: {exc})", ""]
        price = None

    # Multi-timeframe trend / momentum / volatility
    lines += ["## Trend, momentum, volatility",
              "| TF | close | EMA20 | EMA50 | trend | RSI14 | ATR14 (pips) |",
              "| --- | ---: | ---: | ---: | --- | ---: | ---: |"]
    daily: list[dict] = []
    h1: list[dict] = []
    for period, label in TIMEFRAMES:
        try:
            bars = _bars(mt5_sdk, config, symbol, period, TREND_BARS)
        except Exception as exc:
            lines.append(f"| {label} | (unavailable: {exc}) | | | | | |")
            continue
        closes = [b["close"] for b in bars]
        if price is None and closes:
            price = closes[-1]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        r, a = rsi(closes), atr(bars)
        lines.append(
            f"| {label} | {_fmt(closes[-1], digits)} | {_fmt(e20[-1], digits)} | {_fmt(e50[-1], digits)} | "
            f"{trend_label(closes)} | {r:.0f} | {a / pip:.1f} |" if r is not None and a is not None and e50
            else f"| {label} | {_fmt(closes[-1], digits)} | | | n/a | | |"
        )
        if period == "1d":
            # Brokers on UTC day boundaries (Exness) print a stub Sunday bar
            # for the ~3h Sunday-evening session -- drop it so "previous
            # day" means the last real trading day.
            daily = [b for b in bars if datetime.fromisoformat(str(b["time"])).weekday() != 6]
        if period == "1h":
            h1 = bars
    lines.append("")

    # Key levels
    lines.append("## Key levels (broker day boundaries)")
    if len(daily) >= 6:
        prev, today = daily[-2], daily[-1]
        last5 = daily[-6:-1]
        lines += [
            f"- Today: open {_fmt(today['open'], digits)}, high {_fmt(today['high'], digits)}, "
            f"low {_fmt(today['low'], digits)}",
            f"- Previous day: high {_fmt(prev['high'], digits)}, low {_fmt(prev['low'], digits)}, "
            f"close {_fmt(prev['close'], digits)}",
            f"- Prior 5 days: high {_fmt(max(b['high'] for b in last5), digits)}, "
            f"low {_fmt(min(b['low'] for b in last5), digits)}",
        ]
    try:
        weekly = _bars(mt5_sdk, config, symbol, "1w", 3)
        if len(weekly) >= 2:
            lines.append(f"- Previous week: high {_fmt(weekly[-2]['high'], digits)}, "
                         f"low {_fmt(weekly[-2]['low'], digits)}")
    except Exception:
        pass
    if h1 and price is not None:
        highs, lows = swing_levels(h1[-SWING_LOOKBACK_H1_BARS:])
        above = sorted({round(h, digits) for h in highs + lows if h > price})[:3]
        below = sorted({round(l, digits) for l in highs + lows if l < price}, reverse=True)[:3]
        lines.append(f"- H1 swing levels above price (nearest first): "
                     f"{', '.join(_fmt(x, digits) for x in above) or 'none in last 3 days'}")
        lines.append(f"- H1 swing levels below price (nearest first): "
                     f"{', '.join(_fmt(x, digits) for x in below) or 'none in last 3 days'}")
    lines.append("")

    # Dollar & cross-asset
    lines.append("## Dollar and cross-asset (daily closes, same broker)")
    try:
        comps = {pair: [b["close"] for b in _bars(mt5_sdk, config, pair + suffix, "1d", 30)]
                 for pair, _ in _DXY_WEIGHTS}
        if all(len(v) >= 21 for v in comps.values()):
            dxy = synthetic_dxy(comps)
            lines.append(f"- Synthetic DXY {dxy[-1]:.2f}: {pct(dxy[-1], dxy[-2]):+.2f}% vs prior close, "
                         f"{pct(dxy[-1], dxy[-6]):+.2f}% over 5 days, trend {trend_label(dxy) if len(dxy) >= 55 else ('above' if dxy[-1] > ema(dxy, 20)[-1] else 'below') + ' its 20-day EMA'}")
        else:
            lines.append("- Synthetic DXY: not enough component history")
    except Exception as exc:
        lines.append(f"- Synthetic DXY: unavailable ({exc.__class__.__name__}: a component pair is missing on this broker)")
    for proxy, label in (("XAUUSD", "Gold"), ("USDJPY", "USDJPY (risk/yield proxy)")):
        if proxy == symbol[:6]:
            continue
        try:
            closes = [b["close"] for b in _bars(mt5_sdk, config, proxy + suffix, "1d", 7)]
            lines.append(f"- {label}: {pct(closes[-1], closes[-2]):+.2f}% vs prior close, "
                         f"{pct(closes[-1], closes[-6]):+.2f}% over 5 days")
        except Exception:
            lines.append(f"- {label}: unavailable")
    lines.append("")

    # Economic calendar
    lines.append(f"## High-impact calendar today ({base}/{quote_ccy})")
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import fundednext_news_calendar as fn_news

        events = [e for e in fn_news.fetch_calendar() if e.get("currency") in (base, quote_ccy)]
        lines += [f"- {e['time']} {e['currency']}: {e['event']}" for e in events] or \
                 ["- none scheduled for these currencies today"]
    except Exception as exc:
        lines.append(f"- calendar unavailable ({exc})")
    lines.append("")
    return "\n".join(lines)


def write_data_pack(symbol: str, connection: str, *, now: datetime | None = None) -> Path | None:
    """Build and write the pack; returns its absolute path, or None on any failure."""
    now = now or datetime.now(timezone.utc)
    try:
        text = build_data_pack(symbol, connection, now=now)
        DATA_PACK_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_PACK_DIR / f"{symbol}_{now.strftime('%Y%m%d_%H%M%S')}.md"
        path.write_text(text, encoding="utf-8")
        _prune_old_packs(now)
        return path
    except Exception:
        logger.exception("data pack: failed to build/write for %s", symbol)
        return None


def _prune_old_packs(now: datetime) -> None:
    cutoff = (now - timedelta(days=DATA_PACK_RETENTION_DAYS)).timestamp()
    for old in DATA_PACK_DIR.glob("*.md"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def swarm_instruction(committee: str, target: str, market: str, pack_path: Path | None) -> str:
    """Opening instruction for the wrapping agent's run_swarm call.

    The swarm's {target} variable is filled with the run_swarm prompt text
    itself (src.tools.swarm_tool._build_variables), so the DATA PACK line
    must be part of that prompt for the sub-agents to see the file path.
    """
    # Real miss 2026-09-28: a FundedNext pass decided SHORT, then ended with
    # "Awaiting your confirmation to place the SHORT order" -- no one reads
    # these runs live, so the trade silently never happened (price then fell
    # 23 pips).
    unattended = (
        "This run is FULLY AUTOMATED AND UNATTENDED: no human will read or answer anything before "
        "the market moves. Never ask for confirmation or offer options -- either call "
        "trading_place_order exactly as the rules below allow, or state the specific rule-based "
        "reason no order was placed.\n\n"
    )
    if pack_path is None:
        return unattended + (
            f'Call run_swarm with preset_name="{committee}" and a prompt that starts with the line '
            f'"{target} ({market})", followed by your summary of the facts below.\n\n'
        )
    return unattended + (
        f'Call run_swarm with preset_name="{committee}". The prompt you pass must start with these '
        f"two lines, verbatim:\n\n"
        f"DATA PACK FILE: {pack_path.as_posix()}\n"
        f"{target} ({market})\n\n"
        f"After those two lines, add your summary of the facts below. Every swarm agent is "
        f"instructed to read_file that data pack first, so keep the DATA PACK FILE line exactly as "
        f"written.\n\n"
        f'If run_swarm returns an error, do NOT retry without preset_name or with a different preset '
        f'-- a different committee is not a substitute. Place no order, and report the error text '
        f"verbatim as the reason no order was placed.\n\n"
    )


if __name__ == "__main__":  # manual check: python scripts/market_data_pack.py EURUSDm mt5-live-trade
    sys.path.insert(0, str(AGENT_DIR))
    from src.providers.llm import _ensure_dotenv

    _ensure_dotenv()
    print(build_data_pack(sys.argv[1], sys.argv[2]))
