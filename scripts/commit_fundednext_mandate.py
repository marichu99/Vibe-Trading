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

Numbers below are sized for a **$6,000 FundedNext Stellar 1-Step challenge
account**, trading EURUSD/AUDUSD at 0.24/0.33 lots respectively (see
``scripts/fundednext_reporter.py`` TARGETS) — NOT copied from the Exness
mandate's numbers, which were sized for a very different (much smaller,
much higher leverage) account.

MAX_ORDER_USD/MAX_TOTAL_EXPOSURE_USD RAISED 2026-09-17 from an initial
$3,000/$3,000 (sized for the original 0.01-lot default, before the sizing
analysis this session found that left real risk/trade under $1 on a $6,000
account) to $35,000/$60,000, matching the real order notional at the new
lot sizes: EURUSD 0.24 lots * 100,000 * ~1.148 ~= $27.5k, AUDUSD 0.33 lots *
100,000 * ~0.712 ~= $23.5k (both live-quoted 2026-09-17), with headroom for
normal price movement and both positions open at once. Re-verify these
notional figures with a fresh quote before relying on this if either pair
has moved meaningfully since, or if lots is tuned again.

MAX_LOSS_PER_ORDER_USD = $60 is exactly 1% of the $6,000 starting balance,
mirroring FundedNext's own imposable "1% max risk per trade" rule and
``fundednext_guardrails.py``'s ``_effective_max_loss_usd_fundednext``. Unlike
that function (which recomputes 1% of *current* balance on every prompt),
this mandate ceiling is a static number set at commit time — RE-RUN THIS
SCRIPT to bump it as the account balance grows, the same "two independently
maintained numbers" caveat ``commit_mt5_mandate.py`` already carries for its
own MAX_LOSS_PER_ORDER_USD.

MAX_LEVERAGE = 30 matches the account's own actual leverage (confirmed live
via get_account 2026-09-17: "leverage": 30) — no longer just a placeholder.
Margin check at the new lot sizes: both EURUSD (0.24 lots) and AUDUSD (0.33
lots) open simultaneously uses roughly $1,700 of the $6,000 balance at this
leverage — well clear of a margin call, still leaves most of the account as
free margin.

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
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

from src.live.mandate.commit import CommitError, commit_mandate, save_proposal  # noqa: E402
from src.live.mandate.store import load_mandate  # noqa: E402
from src.providers.llm import _ensure_dotenv  # noqa: E402

BROKER = "mt5fn"
# FundedNext MT5 login (an id, not a credential — the password itself is
# never stored anywhere in this repo; see profiles.py's docstring: MT5
# credentials only ever get entered directly into the terminal app).
#
# Deliberately NOT a hardcoded literal here, unlike commit_mt5_mandate.py's
# ACCOUNT_REF — an account number is still identifying information, and this
# repo's git history is not the place for it. Set FUNDEDNEXT_ACCOUNT_REF in
# agent/.env (gitignored) instead; _ensure_dotenv() loads it the same way
# committee_reporter.py/fundednext_reporter.py load SMTP_*/DEEPSEEK_API_KEY.
_ensure_dotenv()
ACCOUNT_REF = os.environ.get("FUNDEDNEXT_ACCOUNT_REF", "")

# Sized for a $6,000 Stellar 1-Step challenge account — see module docstring.
MAX_ORDER_USD = 35000.0
MAX_TOTAL_EXPOSURE_USD = 60000.0
MAX_LEVERAGE = 30.0  # matches the account's own actual leverage — see module docstring
MAX_TRADES_PER_DAY = 10
LIFETIME_DAYS = 30
FLATTEN_ON_HALT = True
ALLOWED_INSTRUMENTS = ["cfd"]
ASSET_CLASSES = ["forex"]  # EURUSD, AUDUSD — see fundednext_reporter.py TARGETS

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
        "notes": "FundedNext Stellar 1-Step challenge mandate ($6,000 account), forex only (EURUSD/AUDUSD).",
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
        "intent_normalized": "FundedNext Stellar 1-Step challenge forex trading via fundednext_reporter.py",
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

    if not ACCOUNT_REF:
        print("refusing to commit: set FUNDEDNEXT_ACCOUNT_REF in agent/.env to the real FundedNext MT5 login first")
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
