"""Replay closed journal trades on M5 bars under alternative exit rules.

Usage (from repo root): .venv/Scripts/python.exe scripts/exit_replay.py <connection> <journal.json> <server_utc_offset_hours> <out.json>
Run once per connection (MT5 attaches to one terminal per process).
Broker server UTC offsets as of 2026-09: Exness (mt5-live-trade) 0, FundedNext
(mt5fn-live-trade) 3. First used 2026-09-28 to justify STOP_TRAILING_ENABLED=False.
"""
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

conn, journal_path, offset_h, out_path = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
OFFSET = timedelta(hours=offset_h)

# Current production constants (identical in both reporters).
BE_FRACTION_OF_TP = 0.5
BE_BUFFER_POINTS = 20
ATR_BARS, ATR_MULT = 14, 1.5
MAX_HOLD_H, DECAY_START = 40.0, 0.75
WEEKEND_CUTOFF_H = 20
DEFAULT_TRIGGER = 8.0
# early_profit_trigger_usd per symbol as currently configured in TARGETS.
TRIGGERS = {
    "EURUSDm": 1.00, "AUDUSDm": 0.75, "GBPUSDm": 1.00, "XAUUSDm": DEFAULT_TRIGGER,
    "EURUSD": 24.00, "AUDUSD": 24.75, "GBPUSD": 30.00, "XAUUSD": 30.00,
}

cfg = mt5_sdk.build_config(profile_by_id(conn).config, {})
mt5_sdk.contract_size("EURUSDm" if conn == "mt5-live-trade" else "EURUSD", config=cfg)  # attach


def to_server(ts_utc):
    return ts_utc + OFFSET


def bar_utc(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc) - OFFSET


def atr_at(symbol, t_utc, m15):
    prior = [b for b in m15 if bar_utc(b["time"]) + timedelta(minutes=15) <= t_utc][-(ATR_BARS + 1):]
    if len(prior) < 2:
        return None
    trs = [max(prior[i]["high"] - prior[i]["low"], abs(prior[i]["high"] - prior[i - 1]["close"]),
               abs(prior[i]["low"] - prior[i - 1]["close"])) for i in range(1, len(prior))]
    return sum(trs) / len(trs) * ATR_MULT


def weekend_cutoff(t):
    days_to_fri = (4 - t.weekday()) % 7
    fri = (t + timedelta(days=days_to_fri)).replace(hour=WEEKEND_CUTOFF_H, minute=0, second=0, microsecond=0)
    return fri if fri > t else fri + timedelta(days=7)


def simulate(tr, bars, m15, info, rule):
    """Return realized move in price units per full position (partials blended)."""
    buy = tr["side"] == "buy"
    sgn = 1 if buy else -1
    entry, sl0, tp = tr["entry_price"], tr["stop_loss"], tr["take_profit"]
    risk = abs(entry - sl0)
    point = info.point
    opened = datetime.fromisoformat(tr["opened_at"])
    hard_end = min(opened + timedelta(hours=MAX_HOLD_H), weekend_cutoff(opened))
    size = info.trade_contract_size
    lots = tr["lots"]
    stop = sl0
    remaining = 1.0
    realized = 0.0
    partial_done = False
    for b in bars:
        t = bar_utc(b["time"])
        if t < opened.replace(second=0, microsecond=0) + timedelta(minutes=5):
            continue
        spr = (b["spread"] or 0) * point
        # Exit-side prices: buy exits on bid, sell exits on ask (bid + spread).
        hi = b["high"] + (0 if buy else spr)
        lo = b["low"] + (0 if buy else spr)
        # 1) stop first (pessimistic), 2) partial, 3) target.
        if (buy and lo <= stop) or (not buy and hi >= stop):
            return realized + remaining * (stop - entry) * sgn, "stop"
        fav_extreme = hi if buy else lo
        if rule.get("partial_at_r") and not partial_done:
            p_level = entry + sgn * rule["partial_at_r"] * risk
            if (buy and fav_extreme >= p_level) or (not buy and fav_extreme <= p_level):
                realized += 0.5 * (p_level - entry) * sgn
                remaining = 0.5
                partial_done = True
                if rule.get("be_after_partial"):
                    stop = max(stop, entry) if buy else min(stop, entry)
        if (buy and fav_extreme >= tp) or (not buy and fav_extreme <= tp):
            return realized + remaining * (tp - entry) * sgn, "target"
        close_px = b["close"] + (0 if buy else spr)
        if t + timedelta(minutes=5) >= hard_end:
            return realized + remaining * (close_px - entry) * sgn, "time/weekend"
        # Poll-time stop updates, evaluated at bar close (5-min poll cadence).
        gained = (close_px - entry) * sgn
        cands = []
        if rule.get("be_tp_fraction") and gained >= abs(tp - entry) * rule["be_tp_fraction"]:
            cands.append(entry - sgn * BE_BUFFER_POINTS * point)
        if rule.get("be_at_r") and gained >= rule["be_at_r"] * risk:
            cands.append(entry - sgn * BE_BUFFER_POINTS * point)
        if rule.get("usd_trail") and gained >= TRIGGERS.get(tr["symbol"], DEFAULT_TRIGGER) / (size * lots):
            a = atr_at(tr["symbol"], t + timedelta(minutes=5), m15)
            if a:
                cands.append(close_px - sgn * a)
        if rule.get("r_trail_start") and gained >= rule["r_trail_start"] * risk:
            cands.append(close_px - sgn * rule["r_trail_dist"] * risk)
        if rule.get("decay"):
            elapsed = (t + timedelta(minutes=5) - opened).total_seconds() / 3600
            if elapsed >= MAX_HOLD_H * DECAY_START:
                a = atr_at(tr["symbol"], t + timedelta(minutes=5), m15)
                if a:
                    cands.append(close_px - sgn * a)
        if cands:
            best = max(cands) if buy else min(cands)
            # Only ever tighten, and never through the current price.
            if buy and best > stop and best < close_px:
                stop = best
            if not buy and best < stop and best > close_px:
                stop = best
    last = bars[-1]
    return realized + remaining * ((last["close"] - entry) * sgn), "data-end"


RULES = {
    "static": {},
    "no_usd_trail (BE@50%TP+decay)": {"be_tp_fraction": BE_FRACTION_OF_TP, "decay": True},
    "static+decay": {"decay": True},
    "BE@75%TP+decay": {"be_tp_fraction": 0.75, "decay": True},
    "current": {"be_tp_fraction": BE_FRACTION_OF_TP, "usd_trail": True, "decay": True},
    "current_be_0.5R": {"be_at_r": 0.5, "usd_trail": True, "decay": True},
    "be_0.5R_only": {"be_at_r": 0.5, "decay": True},
    "be_0.5R_trail1R@0.75R": {"be_at_r": 0.5, "r_trail_start": 1.0, "r_trail_dist": 0.75, "decay": True},
    "be_0.5R_half@1R": {"be_at_r": 0.5, "partial_at_r": 1.0, "be_after_partial": True, "decay": True},
    "be_0.5R_half@1R_trail": {"be_at_r": 0.5, "partial_at_r": 1.0, "be_after_partial": True,
                               "r_trail_start": 1.5, "r_trail_dist": 1.0, "decay": True},
}

journal = json.load(open(journal_path))
journal = journal if isinstance(journal, list) else journal.get("trades", [])
rows = []
for tr in journal:
    if tr.get("status") != "closed" or not tr.get("entry_price") or not tr.get("stop_loss") or not tr.get("take_profit"):
        continue
    opened = datetime.fromisoformat(tr["opened_at"])
    mt5.symbol_select(tr["symbol"], True)
    info = mt5.symbol_info(tr["symbol"])
    start, end = to_server(opened - timedelta(minutes=5)), to_server(opened + timedelta(hours=MAX_HOLD_H + 1))
    m5 = mt5.copy_rates_range(tr["symbol"], mt5.TIMEFRAME_M5, start, end)
    m15 = mt5.copy_rates_range(tr["symbol"], mt5.TIMEFRAME_M15, to_server(opened - timedelta(hours=6)), end)
    if m5 is None or len(m5) == 0:
        rows.append({"ticket": tr["ticket"], "symbol": tr["symbol"], "skipped": "no bars"})
        continue
    m5 = [dict(zip(m5.dtype.names, r)) for r in m5]
    m15 = [dict(zip(m15.dtype.names, r)) for r in m15] if m15 is not None else []
    risk_px = abs(tr["entry_price"] - tr["stop_loss"])
    usd_per_px = info.trade_contract_size * tr["lots"]
    row = {"ticket": tr["ticket"], "symbol": tr["symbol"], "side": tr["side"], "opened_at": tr["opened_at"],
           "risk_usd": risk_px * usd_per_px, "actual_usd": tr.get("profit"), "results": {}}
    for name, rule in RULES.items():
        move, how = simulate(tr, m5, m15, info, rule)
        row["results"][name] = {"R": move / risk_px, "usd": move * usd_per_px, "exit": how}
    rows.append(row)

json.dump(rows, open(out_path, "w"), indent=1, default=float)
print(f"{conn}: replayed {sum(1 for r in rows if 'results' in r)} trades, skipped {sum(1 for r in rows if 'skipped' in r)}")
