"""Export full available bar history from an MT5 terminal to spec-format CSVs.

Usage (from repo root; one broker per process, MT5 attaches to one terminal):
    .venv/Scripts/python.exe scripts/export_mt5_history.py exness [--symbols ...] [--all-visible] [--out DIR]
    .venv/Scripts/python.exe scripts/export_mt5_history.py fundednext

One CSV per symbol per interval, plus manifest.json (SHA-256 per file),
metadata.json (per-symbol units/currency/coverage) and NOTES.md. Bars are never
filled, deleted or "cleaned"; problem bars are flagged in extra columns.
"""
import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "agent"))
from src.providers.llm import _ensure_dotenv  # noqa: E402

_ensure_dotenv()
import MetaTrader5 as mt5  # noqa: E402
from src.trading.connectors.mt5 import sdk as mt5_sdk  # noqa: E402
from src.trading.profiles import profile_by_id  # noqa: E402

# Server clocks: Exness UTC (committee_reporter.py, verified 2026-09-26); FundedNext
# Europe/Bucharest EET/EEST (fundednext_state.py; measured UTC+2 on 2026-11-16).
BROKERS = {
    "exness": {"connection": "mt5-live-trade", "tz": timezone.utc, "tz_name": "UTC (no DST)",
               "symbols": ["EURUSDm", "GBPUSDm", "AUDUSDm"]},
    "fundednext": {"connection": "mt5fn-live-trade", "tz": ZoneInfo("Europe/Bucharest"),
                   "tz_name": "Europe/Bucharest (UTC+2 winter / UTC+3 summer)",
                   "symbols": ["EURUSD", "GBPUSD", "AUDUSD"]},
}
INTERVALS = {
    "1m": (mt5.TIMEFRAME_M1, 60), "5m": (mt5.TIMEFRAME_M5, 300), "15m": (mt5.TIMEFRAME_M15, 900),
    "30m": (mt5.TIMEFRAME_M30, 1800), "1h": (mt5.TIMEFRAME_H1, 3600), "4h": (mt5.TIMEFRAME_H4, 14400),
    "1d": (mt5.TIMEFRAME_D1, 86400),
}
EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
CHUNK = timedelta(days=365)
COLUMNS = ["date", "open", "high", "low", "close", "volume", "spread_points", "real_volume",
           "flag_ohlc_invalid", "flag_zero_price", "flag_zero_volume", "gap_before_s"]


def to_server(dt_utc, tz):
    """MT5 takes/returns server wall-clock time encoded as if it were UTC."""
    return dt_utc.astimezone(tz).replace(tzinfo=timezone.utc)


def to_utc(epoch, tz):
    return datetime.fromtimestamp(int(epoch), timezone.utc).replace(tzinfo=tz).astimezone(timezone.utc)


def fetch_all(symbol, tf, step_s, maxbars, tz):
    """Walk backwards until the terminal returns nothing older. A request spanning more
    than maxbars bars fails outright, so chunks are sized to stay well under it."""
    chunk = min(CHUNK, timedelta(seconds=step_s * maxbars // 3))
    rows, end, empty_streak = {}, datetime.now(timezone.utc) + timedelta(days=1), 0
    while end > EARLIEST and empty_streak < max(3, int(timedelta(days=21) / chunk)):
        start = end - chunk
        for attempt in range(3):  # the terminal returns empty while it is still syncing from the server
            got = mt5.copy_rates_range(symbol, tf, to_server(start, tz), to_server(end, tz))
            if got is not None and len(got) > 0:
                break
            time.sleep(1)
        if got is None or len(got) == 0:
            empty_streak += 1
        else:
            empty_streak = 0
            for b in got:
                rows[int(b["time"])] = b
        end = start
    return [rows[k] for k in sorted(rows)]


def ohlc_invalid(b):
    return not (b["high"] >= b["low"] and b["low"] <= min(b["open"], b["close"])
                and b["high"] >= max(b["open"], b["close"]))


def write_csv(path, bars, step_s, tz):
    prev = None
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for b in bars:
            t_utc = to_utc(b["time"], tz)
            o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            gap = int((t_utc - prev).total_seconds()) if prev else 0
            w.writerow([t_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), repr(o), repr(h), repr(l), repr(c),
                        int(b["tick_volume"]), int(b["spread"]), int(b["real_volume"]),
                        int(ohlc_invalid(b)), int(min(o, h, l, c) <= 0), int(b["tick_volume"] == 0),
                        gap if gap > step_s else 0])
            prev = t_utc


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def symbol_meta(symbol, broker):
    i = mt5.symbol_info(symbol)
    return {
        "symbol": symbol, "description": i.description, "path": i.path,
        "currency_base": i.currency_base, "currency_profit": i.currency_profit,
        "currency_margin": i.currency_margin,
        "price_units": f"major units of {i.currency_profit} per 1 {i.currency_base} (not cents)",
        "digits": i.digits, "point": i.point, "contract_size": i.trade_contract_size,
        "volume_units": "tick_volume = number of price updates in the bar (NOT traded size); "
                        "real_volume is 0 for OTC FX",
        "spread_units": f"points (1 point = {i.point})",
        "exchange": f"none - OTC broker quotes ({broker} MT5 server)",
        "timezone_of_export": "UTC", "server_timezone": BROKERS[broker]["tz_name"],
        "swap_long": i.swap_long, "swap_short": i.swap_short,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("broker", choices=sorted(BROKERS))
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--all-visible", action="store_true", help="export every Market Watch symbol")
    ap.add_argument("--out")
    args = ap.parse_args()
    b = BROKERS[args.broker]
    tz = b["tz"]

    cfg = mt5_sdk.build_config(profile_by_id(b["connection"]).config, {})
    mt5_sdk.contract_size(b["symbols"][0], config=cfg)  # attaches this process to the terminal
    term, acct = mt5.terminal_info(), mt5.account_info()
    if args.all_visible:
        symbols = [s.name for s in mt5.symbols_get() if s.visible]
    else:
        symbols = args.symbols or b["symbols"]

    out = Path(args.out or REPO / "data" / "mt5_history" / args.broker)
    out.mkdir(parents=True, exist_ok=True)
    manifest, meta, coverage = [], {}, {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            print(f"skip {sym}: not available on this server")
            continue
        meta[sym] = symbol_meta(sym, args.broker)
        coverage[sym] = {}
        for name, (tf, step) in INTERVALS.items():
            bars = fetch_all(sym, tf, step, term.maxbars, tz)
            if not bars:
                coverage[sym][name] = None
                print(f"{sym} {name}: no data")
                continue
            path = out / f"{sym}_{name}.csv"
            write_csv(path, bars, step, tz)
            first, last = to_utc(bars[0]["time"], tz), to_utc(bars[-1]["time"], tz)
            n_bad = sum(1 for x in bars if ohlc_invalid(x))
            n_zero_vol = sum(1 for x in bars if x["tick_volume"] == 0)
            capped = len(bars) >= 0.95 * term.maxbars  # chunk overlap makes the count land near, not on, maxbars
            coverage[sym][name] = {"from": first.isoformat(), "to": last.isoformat(), "rows": len(bars),
                                   "capped_by_maxbars": capped}
            manifest.append({"file": path.name, "sha256": sha256(path), "rows": len(bars),
                             "from": first.isoformat(), "to": last.isoformat(),
                             "ohlc_invalid_rows": n_bad, "zero_volume_rows": n_zero_vol,
                             "capped_by_maxbars": capped})
            print(f"{sym} {name}: {len(bars):>8} bars  {first:%Y-%m-%d} -> {last:%Y-%m-%d}  "
                  f"invalid={n_bad}{'  CAPPED' if capped else ''}")

    generated = datetime.now(timezone.utc).isoformat()
    (out / "metadata.json").write_text(json.dumps(
        {"generated_utc": generated, "broker": args.broker, "server": acct.server if acct else None,
         "connection_profile": b["connection"], "terminal_maxbars": term.maxbars,
         "server_timezone": b["tz_name"], "timestamp_convention": "bar OPEN time, UTC, ISO 8601",
         "symbols": meta, "coverage": coverage}, indent=2), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(
        {"generated_utc": generated, "algorithm": "sha256", "files": manifest}, indent=2), encoding="utf-8")
    (out / "NOTES.md").write_text(NOTES.format(
        generated=generated, maxbars=term.maxbars, broker=args.broker, server_tz=b["tz_name"],
        server=acct.server if acct else "?"), encoding="utf-8")
    print(f"\nwrote {len(manifest)} files to {out}")


NOTES = """# {broker} MT5 history export - notes

Generated {generated} by scripts/export_mt5_history.py from server {server}.

## Timestamps
- `date` is the bar **OPEN** time, in UTC, ISO 8601 (`...Z`).
- Server clock: {server_tz}. Every bar was converted from server time to UTC individually,
  so DST transitions are handled (for an EET/EEST server, UTC times shift by an hour across
  the late-March / late-October switch - that is correct, not a bug).
- Daily bars open at server midnight, e.g. 22:00 or 21:00 UTC the previous evening for an
  EET/EEST server (the standard "New York 17:00 close" day), and 00:00 UTC for a UTC server.

## Spec sections that do not apply to spot FX
- **Delistings / survivorship:** none. These are broker FX quotes, not listed securities.
  "Listing date" is simply the first bar the server holds (see metadata.json `coverage`).
- **Price adjustment / corporate actions:** none exist. Raw = adjusted; adj_factor = 1.0
  throughout, so no separate factor or corporate-actions files are produced.
- **Index membership:** not applicable.
- **Overnight financing (swap)** is NOT in the bars; current swap rates are in metadata.json.

## Columns beyond the required six
- `spread_points`: the bar's minimum spread in points. Prices are **BID**; ask = bid + spread.
- `real_volume`: exchange volume, always 0 for OTC quotes.
- `flag_ohlc_invalid`: 1 if high<low or high/low fail to bracket open/close. Kept, not dropped.
- `flag_zero_price`, `flag_zero_volume`: kept, not dropped.
- `gap_before_s`: seconds since the previous bar when larger than one interval, else 0. Gaps
  were never filled. Most are weekends/holidays/rollover pauses, not data loss.

## Volume
`volume` = MT5 tick_volume = number of quote updates in the bar. Not traded size; not comparable
across brokers.

## History depth / known quirks
- Depth is whatever the broker server provides. MT5 also caps each series at the terminal's
  "Max bars in chart" setting (currently {maxbars}); series flagged `capped_by_maxbars` in the
  manifest stopped there. Set it to Unlimited (Tools > Options > Charts), restart, re-run.
- Quotes reflect the broker's own liquidity aggregation; spikes at rollover and news are real
  broker prints.
- Trading-session calendars are not exposed by the MT5 Python API; see the symbol specification
  in the terminal (right-click symbol > Specification).

## Source / licence
Broker MT5 server history, downloaded via the MetaTrader5 Python package on the user's own
account. For the account holder's own use; check the broker's client agreement before
redistributing. Refresh: re-run the script any time (it rewrites everything and the manifest).
"""

if __name__ == "__main__":
    main()
