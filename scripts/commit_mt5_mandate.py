"""Commits the live-trading mandate for the ``mt5`` broker (gold + forex).

This is the ONE-TIME (well — once per ~30-day mandate lifetime) consent
ceremony for MT5 live trading. It is deliberately a standalone script, never
an agent tool: ``src.live.mandate.commit.commit_mandate`` is by design
unreachable from the agent loop, so a real human has to run this file
themselves, with numbers they've actually reviewed, for a mandate to exist at
all (see the module docstring in ``agent/src/live/mandate/commit.py`` — "even
a compromised/hallucinating model cannot self-authorize a mandate").

The numbers below are exactly what was reviewed and approved in chat on
2026-08-25, for the $30.76 Exness Kenya live account (login 134611912):
gold (XAUUSDm) only, $6,000 max order/exposure notional, 200x max leverage,
5 trades/day, 30-day expiry, flatten-on-halt. Since MT5 gold is a leveraged
CFD, "funding"/"exposure" here are an AUTHORIZED NOTIONAL CEILING, not literal
account cash — see the model.py InstrumentType.CFD docstring for why a literal
cash figure would make every leveraged buy fail the gate's funding check.

2026-08-27: added max_loss_per_order_usd=$10 — a cap on worst-case planned
loss (stop-loss distance * contract size * lots), independent of order
notional. Motivated by two real live losses ($14.53, $16.55) that were
correctly-sized (0.01 lots, within every existing cap) but had stops placed
farther than intended. This is enforced deterministically at the mandate
gate (src.live.enforcement.check_mandate), not left to prompt compliance —
an order with no stop-loss at all is now denied outright once this cap is
set (fail-closed: unbounded downside is exactly what this exists to prevent).

2026-09-03: raised max_loss_per_order_usd from $10 to $20, then to $30 the
same day, both at the user's explicit request after reviewing why $10 was
there. Gold's 15m-ATR stop floor spent most of 2026-09-01/02 above $10
(logs/risk_cap_gap_history.jsonl range: ~$6.6-$20.0 across 16 passes,
several >$17), so the cap was blocking most passes outright via the ATR
volatility floor rather than the committee choosing not to trade. $30
leaves more headroom above that observed range while still being a real
cap, not a removal of one — the two 2026-08-27 incidents motivated *having*
a stop-distance cap at all, not the specific $10 figure. Revisit this
number, not the cap's existence, if it needs adjusting again.

2026-09-03: added "forex" to asset_classes and EURUSDm to committee_reporter's
TARGETS, at the user's request, pausing gold in the same change until its
elevated volatility calms down. Verified read-only before this change:
EURUSDm's 15m-ATR stop floor sits comfortably inside the $30 cap at 0.01
lots (~0.00076 price units needed vs. a ~0.03-unit budget) — see the paused
XAUUSD entry's comment in committee_reporter.py for the full context and
the gold resume criteria.

Re-run this (bump --lifetime-days or just re-run before the 30 days are up) to
renew; it always issues a fresh mandate_id/consent record, so the old one is
superseded, never mutated.

Usage:
    .venv\\Scripts\\python.exe scripts\\commit_mt5_mandate.py
    .venv\\Scripts\\python.exe scripts\\commit_mt5_mandate.py --show   # print the committed mandate, don't write
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

from src.live.mandate.commit import CommitError, commit_mandate, save_proposal  # noqa: E402
from src.live.mandate.store import load_mandate  # noqa: E402

BROKER = "mt5"
ACCOUNT_REF = "134611912"  # Exness Kenya live login — not a credential, just an id

# Approved 2026-08-25 (see scripts/commit_mt5_mandate.py module docstring).
MAX_ORDER_USD = 6000.0
MAX_TOTAL_EXPOSURE_USD = 6000.0
MAX_LEVERAGE = 200.0
MAX_TRADES_PER_DAY = 5
LIFETIME_DAYS = 30
FLATTEN_ON_HALT = True
ALLOWED_INSTRUMENTS = ["cfd"]
ASSET_CLASSES = ["commodity", "forex"]  # commodity: gold (paused). forex: EURUSD, added 2026-09-03.

# Added 2026-08-27, raised 2026-09-03 — see module docstring.
MAX_LOSS_PER_ORDER_USD = 30.0


def _build_proposal() -> dict:
    profile = {
        "ordinal": 1,
        "label": "mt5-gold-only",
        "account_funding_usd": MAX_TOTAL_EXPOSURE_USD,
        "max_order_usd": MAX_ORDER_USD,
        "max_total_exposure_usd": MAX_TOTAL_EXPOSURE_USD,
        "leverage": MAX_LEVERAGE,
        "daily_trade_cap": MAX_TRADES_PER_DAY,
        "instruments": ALLOWED_INSTRUMENTS,
        "asset_classes": ASSET_CLASSES,
        "min_market_cap_usd": None,
        "min_avg_daily_volume_usd": None,
        "exclude_symbols": [],
        "flatten_on_halt": FLATTEN_ON_HALT,
        "max_loss_per_order_usd": MAX_LOSS_PER_ORDER_USD,
        "notes": "Gold-only (XAUUSDm) live mandate, approved in chat 2026-08-25; max-loss cap added 2026-08-27.",
    }
    ceilings = {
        "account_funding_usd": MAX_TOTAL_EXPOSURE_USD,
        "max_order_notional_usd": MAX_ORDER_USD,
        "max_total_exposure_usd": MAX_TOTAL_EXPOSURE_USD,
        "leverage": MAX_LEVERAGE,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "allowed_instruments": ALLOWED_INSTRUMENTS,
        "max_loss_per_order_usd": MAX_LOSS_PER_ORDER_USD,
    }
    proposal_id = f"mp_{uuid.uuid4().hex}"
    return {
        "proposal_id": proposal_id,
        "session_id": "manual-chat-consent-2026-08-25",
        "intent_normalized": "gold-only live CFD trading via committee_reporter.py",
        "account": {"broker": BROKER, "type": "margin", "funded_by": "user"},
        "ceilings_ref": "manual_review_2026-08-25",
        "ceilings": ceilings,
        "profiles": [profile],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="Print the currently committed mandate and exit")
    args = parser.parse_args()

    if args.show:
        mandate = load_mandate(BROKER)
        if mandate is None:
            print(f"no valid mandate on file for broker={BROKER!r}")
            return 1
        print(json.dumps(
            {
                "schema_version": mandate.schema_version,
                "hard_caps": mandate.hard_caps.__dict__,
                "universe": mandate.universe.__dict__,
                "consent": mandate.consent.__dict__,
                "flatten_on_halt": mandate.flatten_on_halt,
            },
            indent=2,
            default=str,
        ))
        return 0

    proposal = _build_proposal()
    save_proposal(proposal)
    try:
        result = commit_mandate(
            proposal_id=proposal["proposal_id"],
            ordinal=1,
            adjustments=None,
            consent_ack=True,
            broker=BROKER,
            account_ref=ACCOUNT_REF,
            session_id=proposal["session_id"],
            ceilings_ref=proposal["ceilings"],
            lifetime_days=LIFETIME_DAYS,
            flatten_on_halt=FLATTEN_ON_HALT,
        )
    except CommitError as exc:
        print(f"commit failed: {exc}")
        return 1

    print("mandate committed:")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
