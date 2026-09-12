"""Commits the live-trading mandate for the ``mt5fn`` broker (FundedNext challenge, forex).

This is the FundedNext-account sibling of ``commit_mt5_mandate.py`` — see that
file's docstring for why this is a deliberately standalone, human-run script,
never an agent tool (``src.live.mandate.commit.commit_mandate`` is by design
unreachable from the agent loop).

``mt5fn`` is a DISTINCT broker key from ``mt5`` (same connector module, see
``src.trading.service._SDK_CONNECTOR_MODULES``), so this mandate lives at its
own ``<runtime_root>/live/mt5fn/mandate.json`` and is completely independent
of the existing Exness ``mt5`` mandate — halting one account never halts the
other, and their daily-trade counters don't share a bucket.

Numbers below are sized for a **$6,000 FundedNext Stellar 2-Step challenge
account**, trading EURUSDm/AUDUSDm at 0.01 lots (see
``scripts/fundednext_reporter.py``) — NOT copied from the Exness mandate's
numbers, which were sized for a very different (much smaller, much higher
leverage) account.

MAX_LOSS_PER_ORDER_USD = $60 is exactly 1% of the $6,000 starting balance,
mirroring FundedNext's own imposable "1% max risk per trade" rule and
``fundednext_guardrails.py``'s ``_effective_max_loss_usd_fundednext``. Unlike
that function (which recomputes 1% of *current* balance on every prompt),
this mandate ceiling is a static number set at commit time — RE-RUN THIS
SCRIPT to bump it as the account balance grows, the same "two independently
maintained numbers" caveat ``commit_mt5_mandate.py`` already carries for its
own MAX_LOSS_PER_ORDER_USD.

MAX_LEVERAGE = 30 is a conservative placeholder, not a confirmed FundedNext
platform figure — VERIFY against the actual leverage offered on your
purchased Stellar 2-Step account (varies by instrument/account) before
relying on it; it is not the binding constraint for 0.01-lot forex clips at
this account size regardless (order notional and the per-order loss cap
below bind first).

MAX_TRADES_PER_DAY = 10 is well under FundedNext's 200/day "hyperactivity"
threshold — the 2-hour pass cadence in fundednext_reporter.py implies at
most ~12 passes/day across both targets anyway, so this is a real ceiling,
not a number anywhere near the compliance line.

Usage:
    .venv\\Scripts\\python.exe scripts\\commit_fundednext_mandate.py
    .venv\\Scripts\\python.exe scripts\\commit_fundednext_mandate.py --show   # print the committed mandate, don't write
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

BROKER = "mt5fn"
# TODO: fill in the FundedNext MT5 login once the challenge account is
# purchased and signed into a terminal — this is an id, not a credential.
ACCOUNT_REF = "TODO-fundednext-login"

# Sized for a $6,000 Stellar 2-Step challenge account — see module docstring.
MAX_ORDER_USD = 3000.0
MAX_TOTAL_EXPOSURE_USD = 3000.0
MAX_LEVERAGE = 30.0  # placeholder — verify against the actual FundedNext platform figure
MAX_TRADES_PER_DAY = 10
LIFETIME_DAYS = 30
FLATTEN_ON_HALT = True
ALLOWED_INSTRUMENTS = ["cfd"]
ASSET_CLASSES = ["forex"]  # EURUSDm, AUDUSDm — see fundednext_reporter.py TARGETS

# 1% of the $6,000 starting balance — re-derive and re-commit as balance
# grows (see module docstring).
MAX_LOSS_PER_ORDER_USD = 60.0


def _build_proposal() -> dict:
    profile = {
        "ordinal": 1,
        "label": "mt5fn-fundednext-challenge",
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
        "notes": "FundedNext Stellar 2-Step challenge mandate ($6,000 account), forex only (EURUSDm/AUDUSDm).",
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
        "session_id": "manual-chat-consent-fundednext-setup",
        "intent_normalized": "FundedNext Stellar 2-Step challenge forex trading via fundednext_reporter.py",
        "account": {"broker": BROKER, "type": "margin", "funded_by": "user"},
        "ceilings_ref": "manual_review_fundednext_setup",
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

    if ACCOUNT_REF.startswith("TODO"):
        print("refusing to commit: set ACCOUNT_REF to the real FundedNext MT5 login first")
        return 1

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
