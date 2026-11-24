"""Would the committee's own proposed shallow-pullback gate have helped?

DRAFT / research tool -- not wired into either reporter's live decision path.

Twice in one day (2026-11-05), on both accounts independently, the committee's
own PROPOSAL line flagged the same pattern: HTF_TREND_CONTINUATION entries
taken while the H1 pullback against the H4 trend is still "shallow" (retraced
less than Gartley's 1/3 floor -- see market_data_pack.pullback_structure,
GARTLEY_MIN_RETRACE) are chasing the impulse rather than buying/selling an
actual dip. The model's suggested fix: reject entries where retracement < 33%
unless a confirmed rejection candle is present.

This script replays that gate's quantifiable half against the closed-trade
journal: for each trade, it reconstructs the H4 trend and H1 pullback zone
AS THEY WOULD HAVE READ AT ENTRY (point-in-time, same "nothing the live pass
couldn't have known yet" discipline as adx_gate_backtest.py and
baseline_trend_backtest.py), using the exact same pullback_structure() the
live data pack calls -- not a reimplementation. It only classifies a trade as
"the gate would have filtered this" when BOTH (a) the H1 pullback zone was
"shallow" and (b) the trade's own side matches the H4 trend direction (i.e.
it was a trend-continuation-style entry the gate's wording targets at all --
a counter-trend trade, e.g. HTF_LEVEL_REJECTION against the trend, was never
going to be caught by a pullback-depth check in the trend direction).

CAVEAT this script cannot test: the "...unless a confirmed rejection candle
is present" exception is a visual judgment call, not a field in the trade
journal. So the "would filter" bucket below is an UPPER BOUND on how much the
real gate (with the rejection-candle exception wired in) would actually
filter -- some of these may have had a rejection candle and would have been
let through for real. Treat this as "how much does the shallow-zone itself,
ignoring the exception, correlate with losing trades," not a final verdict.

Usage (from repo root, same one-terminal-per-process constraint as the other
drafts):
    .venv/Scripts/python.exe scripts/gartley_pullback_backtest.py <connection> <journal.json> <server_utc_offset_hours>

Broker server UTC offsets as of 2026-09 (see exit_replay.py): Exness
(mt5-live-trade) 0, FundedNext (mt5fn-live-trade) 3.
"""
import bisect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO + r"\scripts")
sys.path.insert(0, REPO + r"\agent")
from src.providers.llm import _ensure_dotenv  # noqa: E402

_ensure_dotenv()
import MetaTrader5 as mt5  # noqa: E402
from src.trading.connectors.mt5 import sdk as mt5_sdk  # noqa: E402
from src.trading.profiles import profile_by_id  # noqa: E402

import market_data_pack as mdp  # noqa: E402

conn, journal_path, offset_h = sys.argv[1], sys.argv[2], float(sys.argv[3])
OFFSET = timedelta(hours=offset_h)

TREND_BARS = mdp.TREND_BARS  # 120 -- same window pullback_structure sees live

cfg = mt5_sdk.build_config(profile_by_id(conn).config, {})
mt5_sdk.contract_size("EURUSDm" if conn == "mt5-live-trade" else "EURUSD", config=cfg)  # attach


def to_server(ts_utc):
    return ts_utc + OFFSET


def bar_utc(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc) - OFFSET


def bars_at_entry(symbol, opened_utc, timeframe, bar_duration, n_bars):
    """Point-in-time bars: only ones fully closed before opened_utc, same
    discipline as adx_gate_backtest.py's adx_at_entry / exit_replay.py's atr_at."""
    start = to_server(opened_utc - bar_duration * int(n_bars * 1.5))
    end = to_server(opened_utc)
    mt5.symbol_select(symbol, True)
    raw = mt5.copy_rates_range(symbol, timeframe, start, end)
    if raw is None or len(raw) == 0:
        return None
    rows = [dict(zip(raw.dtype.names, r)) for r in raw]
    cutoffs = [bar_utc(b["time"]) + bar_duration for b in rows]
    idx = bisect.bisect_right(cutoffs, opened_utc)
    prior = rows[max(0, idx - n_bars):idx]
    return prior


journal = json.load(open(journal_path))
journal = journal if isinstance(journal, list) else journal.get("trades", [])

rows = []
for tr in journal:
    if tr.get("status") != "closed" or tr.get("outcome") not in ("win", "loss"):
        continue
    opened = datetime.fromisoformat(tr["opened_at"])
    symbol, side = tr["symbol"], tr.get("side")

    h4 = bars_at_entry(symbol, opened, mt5.TIMEFRAME_H4, timedelta(hours=4), 55)
    h4_trend = mdp.trend_label([b["close"] for b in h4]) if h4 and len(h4) >= 55 else "n/a"

    h1 = bars_at_entry(symbol, opened, mt5.TIMEFRAME_H1, timedelta(hours=1), TREND_BARS)
    pullback = mdp.pullback_structure(h1, h4_trend) if h1 and h4_trend in ("up", "down") else None
    zone = pullback["zone"] if pullback else "n/a"

    trend_aligned = (side == "buy" and h4_trend == "up") or (side == "sell" and h4_trend == "down")
    would_filter = trend_aligned and zone == "shallow"

    rows.append({
        "ticket": tr.get("ticket"), "symbol": symbol, "side": side, "edge": tr.get("edge", "unknown"),
        "outcome": tr["outcome"], "profit_usd": float(tr.get("profit") or 0.0),
        "h4_trend": h4_trend, "pullback_zone": zone, "trend_aligned": trend_aligned,
        "would_filter": would_filter,
    })

json.dump(rows, open(f"gartley_pullback_backtest_{conn}.json", "w"), indent=1, default=float)

print(f"{conn}: {len(rows)} closed trades scored")


def _summary(bucket):
    n = len(bucket)
    if n == 0:
        return "n=0"
    wins = sum(1 for r in bucket if r["outcome"] == "win")
    net = sum(r["profit_usd"] for r in bucket)
    return f"n={n} {wins}W/{n - wins}L ({100 * wins / n:.0f}%) net={net:+.2f}"


aligned = [r for r in rows if r["trend_aligned"]]
print(f"\nTrend-continuation-direction trades only (the only ones this gate could touch): n={len(aligned)}")
if not aligned:
    print("  none -- this account has no trades where side matched the H4 trend direction at entry")
else:
    filtered = [r for r in aligned if r["would_filter"]]
    passed = [r for r in aligned if not r["would_filter"]]
    print(f"  SHALLOW pullback, gate WOULD FILTER (upper bound -- ignores the rejection-candle exception): {_summary(filtered)}")
    print(f"  gartley/deep/too-deep/past-origin, gate WOULD PASS: {_summary(passed)}")

not_aligned = [r for r in rows if not r["trend_aligned"]]
print(f"\nCounter-trend / mixed-trend / no-pullback-computed trades (gate never applies): {_summary(not_aligned)}")

print(f"\nfull per-trade breakdown written to gartley_pullback_backtest_{conn}.json")
