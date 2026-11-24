"""Would an ADX trend-strength gate have improved historical trades?

DRAFT / research tool -- not wired into either reporter's live decision path.
Answers one question: of the trades the committee ACTUALLY took, how did
outcomes split between "ADX was >= threshold at entry" (a trend-strength
gate would have let this through) and "ADX was < threshold" (a gate would
have filtered it out)? If the filtered-out bucket's R is net negative and the
passed bucket's R is net positive, the gate would have helped; if the
filtered-out bucket was actually profitable, the gate would have thrown away
good trades -- this script measures that trade-off directly instead of
guessing from first principles.

This only replays the ENTRY filter, not the exit -- it reuses the same
closed-trade journal and real M15 bars as exit_replay.py, but the question
here is "should this trade have been taken at all," not "how should it have
been managed afterward." See strategy_tracking.compute_adx's docstring for
why trend STRENGTH (this) is a different question from the existing regime
gate's trend VOLATILITY (compute_atr_percentile) -- a gate could filter on
both at once, but this script isolates ADX's own effect first.

Usage (from repo root, same connection-per-process constraint as
exit_replay.py -- MT5 attaches to one terminal per process):
    .venv/Scripts/python.exe scripts/adx_gate_backtest.py <connection> <journal.json> <server_utc_offset_hours>

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

import strategy_tracking as st  # noqa: E402

conn, journal_path, offset_h = sys.argv[1], sys.argv[2], float(sys.argv[3])
OFFSET = timedelta(hours=offset_h)

# Candidate thresholds -- the research consensus (see the chat conversation
# this script was drafted from: no single authoritative "best" value) cites
# 20-30 depending on source. Reporting all three shows whether the result is
# threshold-sensitive rather than committing to one number up front.
ADX_THRESHOLDS = (20.0, 25.0, 30.0)
ADX_PERIOD = 14
# Bars needed before entry for a non-None compute_adx result (see that
# function's own docstring: ~2x period valid bars), plus slack.
ADX_LOOKBACK_BARS = ADX_PERIOD * 3

cfg = mt5_sdk.build_config(profile_by_id(conn).config, {})
mt5_sdk.contract_size("EURUSDm" if conn == "mt5-live-trade" else "EURUSD", config=cfg)  # attach


def to_server(ts_utc):
    return ts_utc + OFFSET


def bar_utc(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc) - OFFSET


def adx_at_entry(opened_utc, m15):
    """ADX using only M15 bars fully closed before ``opened_utc`` -- the
    same "nothing the live pass couldn't have known yet" discipline as
    exit_replay.py's atr_at (a bar is only usable once its own 15-minute
    window has actually closed)."""
    cutoffs = [bar_utc(b["time"]) + timedelta(minutes=15) for b in m15]
    idx = bisect.bisect_right(cutoffs, opened_utc)
    prior = m15[max(0, idx - (ADX_LOOKBACK_BARS + 1)):idx]
    if len(prior) < ADX_PERIOD * 2:
        return None
    return st.compute_adx(
        [b["high"] for b in prior], [b["low"] for b in prior], [b["close"] for b in prior],
        period=ADX_PERIOD,
    )


journal = json.load(open(journal_path))
journal = journal if isinstance(journal, list) else journal.get("trades", [])

rows = []
for tr in journal:
    if tr.get("status") != "closed" or tr.get("outcome") not in ("win", "loss"):
        continue
    opened = datetime.fromisoformat(tr["opened_at"])
    mt5.symbol_select(tr["symbol"], True)
    start = to_server(opened - timedelta(hours=(ADX_LOOKBACK_BARS + ADX_PERIOD) / 4 + 1))
    end = to_server(opened)
    m15 = mt5.copy_rates_range(tr["symbol"], mt5.TIMEFRAME_M15, start, end)
    if m15 is None or len(m15) == 0:
        rows.append({"ticket": tr.get("ticket"), "symbol": tr["symbol"], "skipped": "no bars"})
        continue
    m15 = [dict(zip(m15.dtype.names, r)) for r in m15]
    adx = adx_at_entry(opened, m15)
    risk_px = abs(tr["entry_price"] - tr["stop_loss"]) if tr.get("entry_price") and tr.get("stop_loss") else None
    profit = float(tr.get("profit") or 0.0)
    r_multiple = None
    if risk_px and tr.get("entry_price"):
        info = mt5.symbol_info(tr["symbol"])
        if info:
            usd_per_px = info.trade_contract_size * tr.get("lots", 0.0)
            if usd_per_px:
                r_multiple = profit / (risk_px * usd_per_px)
    rows.append({
        "ticket": tr.get("ticket"), "symbol": tr["symbol"], "edge": tr.get("edge", "unknown"),
        "outcome": tr["outcome"], "profit_usd": profit, "r_multiple": r_multiple, "adx_at_entry": adx,
    })

json.dump(rows, open(f"adx_gate_backtest_{conn}.json", "w"), indent=1, default=float)

scored = [r for r in rows if r.get("adx_at_entry") is not None]
skipped = len(rows) - len(scored)
print(f"{conn}: {len(scored)} trades scored, {skipped} skipped (no bars / insufficient ADX history)")

for threshold in ADX_THRESHOLDS:
    passed = [r for r in scored if r["adx_at_entry"] >= threshold]
    filtered = [r for r in scored if r["adx_at_entry"] < threshold]

    def _summary(bucket):
        n = len(bucket)
        if n == 0:
            return "n=0"
        wins = sum(1 for r in bucket if r["outcome"] == "win")
        net = sum(r["profit_usd"] for r in bucket)
        r_vals = [r["r_multiple"] for r in bucket if r["r_multiple"] is not None]
        avg_r = sum(r_vals) / len(r_vals) if r_vals else None
        avg_r_text = f"{avg_r:+.2f}R" if avg_r is not None else "n/a"
        return f"n={n} {wins}W/{n - wins}L net={net:+.2f} avg={avg_r_text}"

    print(f"ADX >= {threshold:.0f} (gate would PASS): {_summary(passed)}")
    print(f"ADX <  {threshold:.0f} (gate would FILTER OUT): {_summary(filtered)}")
    print()
