"""News-blackout guardrail for the FundedNext challenge account.

No economic-calendar/scheduled-news-event tool exists anywhere else in this
repo (agent/src/tools/fred_macro_tool.py is backward-looking macro SERIES
data — CPI, unemployment, etc. — not a forward-looking calendar of exact
release timestamps). This module is a small, standalone integration:
Finnhub's free-tier economic calendar endpoint, fetched/cached once per day
(not once per pass) and filtered to high-impact events for the currencies
this account actually trades (EUR/USD/AUD).

IMPORTANT — this is a RISK-AVOIDANCE measure, not a FundedNext compliance
requirement. FundedNext's own "News Reward Share Rule" (a 5-minute-before/
5-minute-after window that reduces counted profit to 40%) applies only to
the FUNDED (post-challenge) account, not the challenge phase itself — see
the plan doc's Step 6. The reason to avoid trading through news DURING the
challenge is purely that a scheduled high-impact release can blow straight
through a stop within seconds, eating the tight self-imposed daily-loss/
static-drawdown budget (fundednext_guardrails.py) for reasons that have
nothing to do with the trade's own thesis — the existing volatility/spread-
floor check already guards against an inadequately-wide stop, but not
against a SCHEDULED event about to spike volatility regardless of how wide
the stop is.

Needs FINNHUB_API_KEY set in the environment (a free Finnhub account key,
sign up at finnhub.io) — this is a user action, not something committed to
the repo. Modeled on fred_macro_tool.py's env-var-key convention (check
availability, fail with a clear reason if absent) but reads the environment
directly via os.environ rather than going through
src.config.accessor.get_env_config()/EnvConfig: that schema is shared,
central config for agent-registered tools, and this module is a standalone
scripts/-level guardrail (same category as committee_reporter.py's own
SMTP_*/EMAIL_* env vars, which it also reads directly) — adding a field
there for a script-only integration would be a wider-blast-radius change
than this needs.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"

CACHE_PATH = REPO_ROOT / "logs" / "fundednext_news_calendar_cache.json"

_FINNHUB_HOST_KEY = "finnhub"
_FINNHUB_MIN_INTERVAL_ENV = "VIBE_TRADING_FINNHUB_MIN_INTERVAL"
_FINNHUB_DEFAULT_MIN_INTERVAL = 1.0
_FINNHUB_TIMEOUT_S = 15.0
_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"

# How far ahead/behind "today" to fetch in one call — wide enough that a
# blackout check near a UTC day boundary still sees events on the adjacent
# calendar day, cheap enough for a once-a-day fetch.
_FETCH_WINDOW_DAYS = 2


def _api_key() -> str | None:
    return os.environ.get("FINNHUB_API_KEY") or None


def available() -> bool:
    """True when FINNHUB_API_KEY is configured. Callers should skip the
    blackout check entirely (fail OPEN, not closed) when this is False —
    see is_news_blackout's own docstring for why."""
    return bool(_api_key())


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, default=str), encoding="utf-8")


def fetch_calendar(*, force: bool = False) -> list[dict]:
    """Return today's cached (or freshly fetched) high-impact economic events.

    Refetches at most once per UTC calendar day (not once per pass — a
    committee pass runs every couple of hours, an economic calendar doesn't
    change that often). Each event: {"time": iso str, "currency": str,
    "impact": str, "event": str}. Returns [] (fails open — see
    is_news_blackout) on a missing API key or any fetch/parse error.
    """
    api_key = _api_key()
    if not api_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache = _read_cache()
    if not force and cache.get("fetched_date") == today and isinstance(cache.get("events"), list):
        return cache["events"]

    sys.path.insert(0, str(AGENT_DIR))
    from backtest.loaders._http import resolve_min_interval, throttled_get_json

    start = (datetime.now(timezone.utc) - timedelta(days=_FETCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=_FETCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    try:
        payload = throttled_get_json(
            _CALENDAR_URL,
            host_key=_FINNHUB_HOST_KEY,
            min_interval=resolve_min_interval(_FINNHUB_MIN_INTERVAL_ENV, _FINNHUB_DEFAULT_MIN_INTERVAL),
            params={"from": start, "to": end, "token": api_key},
            timeout=_FINNHUB_TIMEOUT_S,
        )
    except Exception:
        # Fails open on the CALLER's side (is_news_blackout treats [] as "no
        # known blackout"), not here -- a transient fetch failure must never
        # itself become a reason to skip trading.
        return cache.get("events") or []

    raw_events = payload.get("economicCalendar") if isinstance(payload, dict) else None
    events = _normalize_events(raw_events if isinstance(raw_events, list) else [])
    _write_cache({"fetched_date": today, "events": events})
    return events


def _normalize_events(raw_events: list) -> list[dict]:
    out = []
    for row in raw_events:
        if not isinstance(row, dict):
            continue
        impact = str(row.get("impact") or "").lower()
        if impact not in ("high", "3"):  # Finnhub uses "high"/"medium"/"low" in practice
            continue
        time_str = row.get("time") or row.get("date")
        currency = row.get("currency")
        if not time_str or not currency:
            continue
        out.append({
            "time": str(time_str),
            "currency": str(currency).upper(),
            "impact": impact,
            "event": row.get("event") or "",
        })
    return out


def is_news_blackout(currencies: set[str], now: datetime | None = None, window_minutes: int = 5) -> tuple[bool, str | None]:
    """True if ``now`` falls within ``window_minutes`` of a high-impact release
    for any of ``currencies``.

    Fails OPEN (returns (False, None)), never closed, when the calendar is
    unavailable (no API key, fetch failure, unparseable event time) — a
    missing news-avoidance signal should never itself block trading; the
    existing volatility/spread-floor check is still in effect regardless.
    """
    if not available():
        return False, None

    now = now or datetime.now(timezone.utc)
    events = fetch_calendar()
    window = timedelta(minutes=window_minutes)
    for event in events:
        if event["currency"] not in currencies:
            continue
        try:
            event_time = datetime.fromisoformat(event["time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        if abs((now - event_time).total_seconds()) <= window.total_seconds():
            return True, (
                f"within {window_minutes}min of a high-impact {event['currency']} release "
                f"({event['event'] or 'scheduled event'} at {event_time.isoformat()})"
            )
    return False, None
