"""What would risk-based position sizing have produced on the ACTUAL closed
trades, compared to the fixed lot size that was really used?

DRAFT / research tool -- not wired into either reporter's live sizing path.
See strategy_tracking.risk_based_lots's own docstring for the full rationale
(the industry-standard "fixed-fractional" sizing method: hold the dollar
risk per trade constant by computing lot size from the stop distance,
instead of a fixed lot count that only varies by the regime volatility
multiplier). This script doesn't change anything live -- it replays the
journal through risk_based_lots with a chosen target risk_usd and reports,
per trade, what lot size that would have produced versus what was actually
used, and what the trade's real $ outcome would have scaled to at that size.

Usage (from repo root; MT5 attaches to one terminal per process, same
constraint as exit_replay.py/adx_gate_backtest.py):
    .venv/Scripts/python.exe scripts/risk_sizing_whatif.py <connection> <journal.json> <target_risk_usd>

`target_risk_usd` is the dollar risk you want held constant across trades --
pick something comparable to what the account's current fixed lots actually
average (this script also reports that average so you can choose sensibly
rather than guessing).
"""
import json
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO + r"\scripts")
sys.path.insert(0, REPO + r"\agent")
from src.providers.llm import _ensure_dotenv  # noqa: E402

_ensure_dotenv()
from src.trading.connectors.mt5 import sdk as mt5_sdk  # noqa: E402
from src.trading.profiles import profile_by_id  # noqa: E402

import strategy_tracking as st  # noqa: E402

conn, journal_path, target_risk_usd = sys.argv[1], sys.argv[2], float(sys.argv[3])

cfg = mt5_sdk.build_config(profile_by_id(conn).config, {})

journal = json.load(open(journal_path))
journal = journal if isinstance(journal, list) else journal.get("trades", [])

rows = []
for tr in journal:
    if tr.get("status") != "closed" or not tr.get("entry_price") or not tr.get("stop_loss"):
        continue
    symbol = tr["symbol"]
    actual_lots = tr.get("lots")
    stop_distance = abs(tr["entry_price"] - tr["stop_loss"])
    size = mt5_sdk.contract_size(symbol, config=cfg)
    actual_risk_usd = (stop_distance * size * actual_lots) if (size and actual_lots) else None

    whatif_lots = st.risk_based_lots(symbol, conn, risk_usd=target_risk_usd, stop_distance_price=stop_distance)
    whatif_profit = None
    actual_profit = tr.get("profit")
    if whatif_lots is not None and actual_lots:
        # Scale the ACTUAL realized outcome by the lot-size ratio -- this
        # assumes the same fill/exit behavior at the different size (a
        # simplification; it does not re-simulate spread/slippage at a
        # different size the way exit_replay.py re-simulates exits). Good
        # enough to compare risk CONSISTENCY across trades, not a claim
        # about exact re-executed P&L.
        whatif_profit = actual_profit * (whatif_lots / actual_lots) if actual_profit is not None else None

    rows.append({
        "ticket": tr.get("ticket"), "symbol": symbol, "opened_at": tr.get("opened_at"),
        "actual_lots": actual_lots, "actual_risk_usd": actual_risk_usd, "actual_profit_usd": actual_profit,
        "whatif_lots": whatif_lots, "whatif_risk_usd": target_risk_usd if whatif_lots is not None else None,
        "whatif_profit_usd": whatif_profit,
    })

json.dump(rows, open(f"risk_sizing_whatif_{conn}.json", "w"), indent=1, default=float)

scored = [r for r in rows if r["actual_risk_usd"] is not None]
risks = [r["actual_risk_usd"] for r in scored]
if risks:
    print(f"{conn}: {len(scored)} trades -- actual $ risk per trade ranged "
          f"{min(risks):.2f} to {max(risks):.2f} (avg {sum(risks) / len(risks):.2f}, "
          f"spread shows how much risk already varies trade-to-trade at a 'fixed' lot size)")

filterable = [r for r in rows if r["whatif_lots"] is None]
if filterable:
    print(f"{len(filterable)}/{len(rows)} trades: target_risk_usd={target_risk_usd:.2f} is below this "
          f"account's broker minimum lot step for that trade's stop distance -- risk_based_lots "
          f"correctly refuses rather than silently sizing up (see its docstring); raise "
          f"target_risk_usd or accept skipping these in a live version.")

actual_total = sum(r["actual_profit_usd"] for r in rows if r["actual_profit_usd"] is not None)
whatif_total = sum(r["whatif_profit_usd"] for r in rows if r["whatif_profit_usd"] is not None)
print(f"actual net: {actual_total:+.2f}  |  what-if net at constant ${target_risk_usd:.2f} risk/trade: {whatif_total:+.2f}")
print(f"full per-trade breakdown written to risk_sizing_whatif_{conn}.json")
