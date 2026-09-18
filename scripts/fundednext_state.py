"""Challenge-progress state for the FundedNext Stellar 1-Step account.

Tracks the handful of facts that are true ONCE for the life of the challenge
(when it started, what the starting balance was) plus the slowly-accumulating
facts a pass needs to reason about progress (which calendar days have had a
trade, whether the challenge has been confirmed passed). Deliberately
separate from ``fundednext_guardrails.py`` (which reads
``initial_balance_usd`` from here as the static drawdown floor) and from
``fundednext_reporter.py`` (which reads everything here for its status/email
report) so none of the three needs to import the others' unrelated pieces.

``initial_balance_usd`` is the single most important number this module
holds: it is the STATIC 6%-drawdown floor's basis (FundedNext's own rule —
see ``fundednext_guardrails.py``'s docstring for the "does this floor trail
up with profit?" caveat that still needs verifying against the real
contract). It is set exactly ONCE, on this module's first-ever call against
the real account, and never rewritten after that — a bug that let it drift
would silently misprice the compliance floor, which is why
``ensure_initialized`` refuses to touch it once present (see its docstring)
and why the test suite's highest-priority case is proving that invariant.

Stellar 1-Step is a SINGLE-PHASE challenge (unlike the Stellar 2-Step model
this was originally scoped for, before the actual purchased account turned
out to be 1-Step) — one 10% profit target, then straight to the funded
account with no second phase. ``passed_date`` is set ONLY via ``--mark-passed``
on this module's own CLI, never inferred from balance/profit here —
FundedNext's own dashboard is the authority on when the challenge has
actually passed (their own consistency/KYC/etc. checks might not agree with
a balance-based guess), not something this bot should self-declare.

Server-day boundary: originally assumed GMT+3 DST / GMT+2 standard
(Europe/Bucharest, matching FundedNext's own general documentation for
their server time). Briefly "corrected" 2026-09-17 to plain UTC after a
flawed verification: D1 bar timestamps read via _epoch_to_iso
(``datetime.fromtimestamp(value, tz=timezone.utc)``) landed on exactly
00:00:00, which was read as "the server's real boundary is UTC" — but
REVERTED 2026-09-18 after finding the actual bug: MT5's raw epoch values
from this broker are NOT true UTC seconds, they're the broker's own
LOCAL server clock (EET/EEST) encoded as if it were epoch/UTC — a common
MT5 platform behavior, and exactly what FundedNext's own docs say (GMT+3
DST / GMT+2 standard). Confirmed directly: this machine's own clock is
correct (Windows Get-Date/[DateTime]::UtcNow agree, E. Africa Standard
Time UTC+3, no DST), yet a live MT5 tick's "UTC" timestamp read 3 hours
AHEAD of true UtcNow at the moment of comparison -- exactly the DST-season
EET offset FundedNext documents, not a genuine UTC reading. The
2026-09-17 D1-bar check was comparing a mislabeled-but-internally-
consistent value against itself, not against true UTC, so "00:00:00"
proved nothing. ``server_today()`` is UNAFFECTED by this specific mislabel
bug (it computes from ``datetime.now()`` on THIS machine's own confirmed-
correct clock, converted to the broker's timezone -- it never reads an
MT5 epoch value), so reverting it back to Europe/Bucharest is the correct
fix here. The epoch-mislabeling bug itself is separate and lives in
``agent/src/trading/connectors/mt5/sdk.py``'s ``_recent_deals`` (see its
own comment) -- IT does read broker epoch values, and needed its own fix.
If this account is ever moved to a different FundedNext server, re-verify
the timezone by comparing a live MT5 tick's reported time against this
machine's own confirmed-correct UtcNow (NOT by reading MT5 epoch values
and comparing them only to each other, which is what went wrong here).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"

STATE_PATH = REPO_ROOT / "logs" / "fundednext_challenge_state.json"

# Stellar 1-Step's single profit target (help.fundednext.com, verified
# 2026-09 for the actual purchased account type) — 10% growth on starting
# balance, no time limit. Once cleared, the account converts to a funded
# account with "no specific profit target going forward" (no second phase).
CHALLENGE_TARGET_PCT = 10.0

# FundedNext's own rule for Stellar 1-Step: at least 2 separate trading days,
# >=1 trade each, non-consecutive OK.
MIN_TRADING_DAYS = 2

# Reverted 2026-09-18 back to FundedNext's documented GMT+3 DST / GMT+2
# standard (Europe/Bucharest) -- see module docstring for the full story of
# why the brief 2026-09-17 "UTC" correction was itself wrong.
_SERVER_TZ = ZoneInfo("Europe/Bucharest")


def server_today(now: datetime | None = None) -> str:
    """Today's date (YYYY-MM-DD) in the assumed FundedNext server timezone (see module docstring)."""
    moment = now or datetime.now(_SERVER_TZ)
    if moment.tzinfo is None:
        moment = moment.astimezone(_SERVER_TZ)
    else:
        moment = moment.astimezone(_SERVER_TZ)
    return moment.date().isoformat()


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def ensure_initialized(connection: str) -> dict:
    """Return the challenge state, setting ``challenge_start_date`` and
    ``initial_balance_usd`` from the live account ONCE if they're not already
    set. Every field this function can set is only ever set when absent —
    never overwritten on a later call, which is what makes
    ``initial_balance_usd`` a true one-time anchor (see module docstring).
    """
    state = _read_state()
    changed = False
    if "initial_balance_usd" not in state:
        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.service import get_account

        balance = float(get_account(connection)["account"]["balance"])
        state["initial_balance_usd"] = balance
        changed = True
    if "challenge_start_date" not in state:
        state["challenge_start_date"] = server_today()
        changed = True
    if "trading_days" not in state:
        state["trading_days"] = []
        changed = True
    if changed:
        _write_state(state)
    return state


def get_state() -> dict:
    """Read-only view of the current state (may be partially/un-initialized)."""
    return _read_state()


def record_trading_day(day: str | None = None) -> None:
    """Mark ``day`` (default: today, server-day) as a day this account traded.

    Idempotent — recording the same day twice is a no-op write. Feeds the
    MIN_TRADING_DAYS rule (see ``fundednext_reporter.py``'s status report).
    """
    day = day or server_today()
    state = _read_state()
    days = state.setdefault("trading_days", [])
    if day in days:
        return
    days.append(day)
    days.sort()
    _write_state(state)


def trading_days_count() -> int:
    return len(_read_state().get("trading_days") or [])


def progress_pct(current_balance: float) -> float | None:
    """Percent growth vs. ``initial_balance_usd``, or None if not yet initialized."""
    state = _read_state()
    initial = state.get("initial_balance_usd")
    if not initial:
        return None
    return (current_balance - float(initial)) / float(initial) * 100.0


def mark_passed() -> str:
    """Record that FundedNext's own dashboard has confirmed the challenge
    passed. Idempotent — calling this again after it's already set just
    returns the original date, never overwrites it. Human-run only (via this
    module's CLI) — see module docstring for why this is never inferred
    automatically from balance/profit.
    """
    state = _read_state()
    if state.get("initial_balance_usd") is None:
        raise RuntimeError("challenge state is not initialized yet — run the reporter at least once first")
    if "passed_date" not in state:
        state["passed_date"] = server_today()
        _write_state(state)
    return state["passed_date"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="Print the current challenge state and exit")
    parser.add_argument(
        "--mark-passed", action="store_true",
        help="Record that FundedNext's dashboard has confirmed the challenge passed (human-confirmed only — see module docstring)",
    )
    args = parser.parse_args()

    if args.mark_passed:
        try:
            passed_date = mark_passed()
        except RuntimeError as exc:
            print(f"cannot mark passed: {exc}")
            return 1
        print(f"challenge marked passed as of {passed_date}")
        return 0

    print(json.dumps(get_state(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
