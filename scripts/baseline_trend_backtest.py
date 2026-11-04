"""Does the committee's own judgment add anything beyond the hard-coded
H4/D1 trend-agreement gate it already runs under?

DRAFT / research tool -- not wired into either reporter's live decision
path. Motivated by a production study of live LLM trading agents ("What
LLM Trading Agents Actually Do in Production") that found no directional
edge over a simple baseline for the fleets it studied, and recommended
benchmarking against one before trusting a model's calls.

The simplest baseline available here is already partly enforced in code:
trend_gate() in strategy_tracking.py restricts direction to the H4/D1
trend whenever both timeframes agree (and otherwise applies no
restriction at all). That means on every trade where H4/D1 agreed at
entry, the committee's side is NECESSARILY the trend-agreement direction
-- the baseline and the real trade are the same trade, by construction,
and comparing them would be comparing a number to itself.

The only place a real comparison is possible is the trades placed WHILE
H4/D1 DISAGREED -- those only exist because of HTF_LEVEL_REJECTION,
SESSION_RANGE_BREAKOUT, NEWS_MOMENTUM, or the H1-fallback-continuation
edge, each requiring judgment the trend-agreement baseline doesn't have
at all (it would have skipped every one of these). So this script splits
the closed journal into "h4d1_agree" (baseline and actual trade are
identical -- not informative) and "h4d1_disagree" (the committee's own
edge is the ENTIRE source of whatever result these show -- the baseline
contributes nothing), and reports win-rate/net/avg-R for each bucket.
If the disagree bucket is flat or negative, the committee isn't adding
anything the trend-agreement rule wasn't already providing for free.

This does NOT attempt to simulate a mirrored reverse-direction trade on
the agree-bucket's trades (a baseline guess at what stop/target a
trend-only system "would have" used is far shakier than reusing the
real stop/target that was actually set) -- see module docstring above
for why that comparison is structurally impossible here anyway.

Usage (from repo root, same one-terminal-per-process constraint as
exit_replay.py/adx_gate_backtest.py):
    .venv/Scripts/python.exe scripts/baseline_trend_backtest.py <connection> <journal.json> <server_utc_offset_hours>

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

# mdp.trend_label needs >=55 closes; fetch comfortably more (same TREND_BARS
# the live trend_gate()/trend_continuation_fallback() use) so a point-in-time
# read is never starved of history relative to what the live gate itself saw.
TREND_BARS = mdp.TREND_BARS

cfg = mt5_sdk.build_config(profile_by_id(conn).config, {})
mt5_sdk.contract_size("EURUSDm" if conn == "mt5-live-trade" else "EURUSD", config=cfg)  # attach


def to_server(ts_utc):
    return ts_utc + OFFSET


def bar_utc(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc) - OFFSET


def trend_label_at_entry(symbol, opened_utc, timeframe, bar_duration):
    """Point-in-time trend_label: only bars fully closed before opened_utc
    -- the same "nothing the live pass couldn't have known yet" discipline
    adx_gate_backtest.py's adx_at_entry and exit_replay.py's atr_at use.
    Fetches TREND_BARS*1.5 bars of slack so a cutoff near the window's start
    still leaves >=TREND_BARS prior bars for trend_label's own 55-bar floor."""
    start = to_server(opened_utc - bar_duration * int(TREND_BARS * 1.5))
    end = to_server(opened_utc)
    mt5.symbol_select(symbol, True)
    bars = mt5.copy_rates_range(symbol, timeframe, start, end)
    if bars is None or len(bars) == 0:
        return "n/a"
    cutoffs = [bar_utc(b["time"]) + bar_duration for b in bars]
    idx = bisect.bisect_right(cutoffs, opened_utc)
    prior = bars[max(0, idx - TREND_BARS):idx]
    if len(prior) < 55:
        return "n/a"
    return mdp.trend_label([float(b["close"]) for b in prior])


journal = json.load(open(journal_path))
journal = journal if isinstance(journal, list) else journal.get("trades", [])

rows = []
for tr in journal:
    if tr.get("status") != "closed" or tr.get("outcome") not in ("win", "loss"):
        continue
    opened = datetime.fromisoformat(tr["opened_at"])
    h4 = trend_label_at_entry(tr["symbol"], opened, mt5.TIMEFRAME_H4, timedelta(hours=4))
    d1 = trend_label_at_entry(tr["symbol"], opened, mt5.TIMEFRAME_D1, timedelta(days=1))
    if h4 == "n/a" or d1 == "n/a":
        rows.append({"ticket": tr.get("ticket"), "symbol": tr["symbol"], "skipped": "insufficient H4/D1 history"})
        continue
    agree = h4 == d1 and h4 in ("up", "down")
    if agree:
        baseline_side = "buy" if h4 == "up" else "sell"
        # trend_gate() would have forced this direction in-code -- the
        # actual side recorded should always equal it; flag (not fail) a
        # mismatch, since it most plausibly means TREND_FILTER_ENABLED was
        # off or the gate's own live read used slightly different bar
        # timing than this point-in-time reconstruction, not a real finding
        # about baseline vs. committee judgment.
        mismatch = baseline_side != tr.get("side")
    else:
        mismatch = False
    rows.append({
        "ticket": tr.get("ticket"), "symbol": tr["symbol"], "side": tr.get("side"),
        "outcome": tr["outcome"], "profit_usd": float(tr.get("profit") or 0.0),
        "h4": h4, "d1": d1, "bucket": "h4d1_agree" if agree else "h4d1_disagree",
        "trend_gate_mismatch": mismatch,
    })

json.dump(rows, open(f"baseline_trend_backtest_{conn}.json", "w"), indent=1, default=float)

scored = [r for r in rows if "bucket" in r]
skipped = len(rows) - len(scored)
print(f"{conn}: {len(scored)} trades scored, {skipped} skipped (insufficient H4/D1 history)")

mismatches = [r for r in scored if r["trend_gate_mismatch"]]
if mismatches:
    print(f"WARNING: {len(mismatches)} trade(s) placed counter to what trend_gate() should have forced "
          f"when H4/D1 agreed -- check TREND_FILTER_ENABLED was on for these, before trusting the split below.")

for bucket, label in (("h4d1_agree", "H4/D1 AGREED at entry (baseline == actual trade, not informative)"),
                       ("h4d1_disagree", "H4/D1 DISAGREED at entry (committee's own edge is the entire source of this)")):
    entries = [r for r in scored if r["bucket"] == bucket]
    n = len(entries)
    if n == 0:
        print(f"{label}: n=0")
        continue
    wins = sum(1 for r in entries if r["outcome"] == "win")
    net = sum(r["profit_usd"] for r in entries)
    print(f"{label}: n={n} {wins}W/{n - wins}L ({100 * wins / n:.0f}%) net={net:+.2f}")

print(f"full per-trade breakdown written to baseline_trend_backtest_{conn}.json")
