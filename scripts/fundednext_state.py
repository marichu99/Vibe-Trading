"""Challenge-progress state for the FundedNext Stellar 2-Step account.

Tracks the handful of facts that are true ONCE for the life of the challenge
(when it started, what the starting balance was) plus the slowly-accumulating
facts a pass needs to reason about progress (which calendar days have had a
trade, which phase we're in). Deliberately separate from
``fundednext_guardrails.py`` (which reads ``initial_balance_usd`` from here as
the static drawdown floor) and from ``fundednext_reporter.py`` (which reads
everything here for its status/email report) so none of the three needs to
import the others' unrelated pieces.

``initial_balance_usd`` is the single most important number this module
holds: it is the STATIC 10%-drawdown floor's basis (FundedNext's own rule —
see ``fundednext_guardrails.py``'s docstring for the "does this floor trail
up with profit?" caveat that still needs verifying against the real
contract). It is set exactly ONCE, on this module's first-ever call against
the real account, and never rewritten after that — a bug that let it drift
would silently misprice the compliance floor, which is why
``ensure_initialized`` refuses to touch it once present (see its docstring)
and why the test suite's highest-priority case is proving that invariant.

``current_phase`` is advanced ONLY via ``--advance-phase`` on this module's
own CLI, never inferred from balance/profit here — FundedNext's own dashboard
is the authority on when a phase has actually passed (their own consistency/
KYC/etc. checks might not agree with a balance-based guess), not something
this bot should self-declare.

Server-day boundary: FundedNext's daily-loss-limit day rolls over at
00:00 SERVER time, documented as GMT+3 during DST / GMT+2 standard — the
same summer/winter offset ``Europe/Bucharest`` uses (EU DST schedule).
``server_today()`` assumes that timezone. VERIFY this against the actual
FundedNext MT5 terminal's displayed server time before relying on it,
especially close to a DST transition — a wrong assumption shifts the
daily-loss reset boundary by an hour for about a week each transition, which
could open or close the day's loss budget at the wrong moment. See the
matching caveat in ``fundednext_guardrails.py``.
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

# Stellar 2-Step's two phase targets (help.fundednext.com, verified this
# session) — Phase 1: 8% growth, no time limit. Phase 2: 5% growth, no time
# limit. Falls back to Phase 2's (tighter) figure for any unrecognized phase
# number, rather than the looser Phase 1 figure — a wrong default should
# never make progress look easier than it is.
PHASE_TARGET_PCT = {1: 8.0, 2: 5.0}

# Same GMT+3 DST / GMT+2 standard offset FundedNext documents for its server
# time — see module docstring's caveat.
_SERVER_TZ = ZoneInfo("Europe/Bucharest")


def server_today(now: datetime | None = None) -> str:
    """Today's date (YYYY-MM-DD) in the assumed FundedNext server timezone."""
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
    if "current_phase" not in state:
        state["current_phase"] = 1
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
    5-minimum-trading-days rule (see ``fundednext_reporter.py``'s status
    report).
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


def phase_target_pct(phase: int) -> float:
    return PHASE_TARGET_PCT.get(phase, PHASE_TARGET_PCT[2])


def progress_pct(current_balance: float) -> float | None:
    """Percent growth vs. ``initial_balance_usd``, or None if not yet initialized."""
    state = _read_state()
    initial = state.get("initial_balance_usd")
    if not initial:
        return None
    return (current_balance - float(initial)) / float(initial) * 100.0


def advance_phase() -> int:
    """Bump current_phase 1 -> 2. Refuses if already phase 2 or uninitialized.

    Human-run only (via this module's CLI) — see module docstring for why
    phase advancement is never inferred automatically from balance/profit.
    """
    state = _read_state()
    phase = state.get("current_phase")
    if phase is None:
        raise RuntimeError("challenge state is not initialized yet — run the reporter at least once first")
    if phase >= 2:
        raise RuntimeError(f"already at phase {phase}; nothing to advance to")
    state["current_phase"] = phase + 1
    _write_state(state)
    return state["current_phase"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="Print the current challenge state and exit")
    parser.add_argument(
        "--advance-phase", action="store_true",
        help="Advance current_phase from 1 to 2 (human-confirmed only — see module docstring)",
    )
    args = parser.parse_args()

    if args.advance_phase:
        try:
            new_phase = advance_phase()
        except RuntimeError as exc:
            print(f"cannot advance phase: {exc}")
            return 1
        print(f"advanced to phase {new_phase}")
        return 0

    print(json.dumps(get_state(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
