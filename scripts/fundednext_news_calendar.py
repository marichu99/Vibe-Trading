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
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _redact(text: str) -> str:
    """Strip a Finnhub API token out of a URL-bearing string before it is
    ever embedded in an exception message, a log line, or a report that gets
    emailed. Real incident 2026-10-02/10-05: the 2026-10-01 fail-closed fix
    (CalendarUnavailable/is_news_blackout) made a fetch failure's exception
    text -- which `requests` builds from the full request URL, token query
    param included -- visible in both logs and the emailed report for the
    first time. A `mask_secrets()` logging filter alone would NOT have
    caught this: the email path (report_text -> send_email) never goes
    through the logger, so the token has to be stripped at the source,
    here, not downstream. `?token=`/`&token=` (case-insensitive) up to the
    next `&` or end of string is replaced with a fixed placeholder.
    """
    import re
    return re.sub(r"([?&]token=)[^&\s]+", r"\1***REDACTED***", text, flags=re.IGNORECASE)


class _SecretMaskingLogFilter(logging.Filter):
    """Redacts an API token out of every log record before it's formatted.

    Defense-in-depth for D1, not the primary fix -- the real fix is not
    constructing a token-bearing message in the first place (_redact is
    applied at the source in fetch_calendar/is_news_blackout/
    startup_self_test). This exists in case some future code path logs a
    raw exception or URL directly without going through _redact first. It
    does NOT cover the emailed report body (report_text/send_email never
    passes through the logging system) -- that path is only safe because
    the source-level redaction above keeps a token out of it in the first
    place.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def mask_secrets() -> logging.Filter:
    """A logging.Filter that redacts API tokens from log records. Attach to
    a handler (not just one logger) so it covers every logger that
    propagates through it: `handler.addFilter(mask_secrets())`."""
    return _SecretMaskingLogFilter()


def available() -> bool:
    """True when FINNHUB_API_KEY is configured. When this is False,
    is_news_blackout fails CLOSED (changed 2026-10-01, rulebook v4) -- see
    its own docstring for why."""
    return bool(_api_key())


def news_api_status() -> str:
    """"OK" / "STALE" / "UNAVAILABLE" -- informational, for the committee
    prompt's NEWS_API_STATUS field (D6, v5.1, 2026-10-06). Derived from the
    existing cache file with no extra network call (call this AFTER
    is_news_blackout has already run for this pass, so the cache reflects
    whatever that call just did).

    "STALE" is the state D1's fail-closed fix does NOT catch: a live fetch
    that fails but falls back to a cached (possibly day-old) calendar still
    returns a normal, non-raising blackout verdict from is_news_blackout --
    the pass stays fully trade-enabled, with nothing anywhere flagging that
    the calendar it just traded against might be outdated. This field (and
    the "NEWS_API_STATUS != OK -> PASS" prompt rule) is the first thing that
    surfaces that risk at all.
    """
    if not available():
        return "UNAVAILABLE"
    cache = _read_cache()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cache.get("fetched_date") == today and isinstance(cache.get("events"), list):
        return "OK"
    if isinstance(cache.get("events"), list):
        return "STALE"
    return "UNAVAILABLE"


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, default=str), encoding="utf-8")


class CalendarUnavailable(Exception):
    """Raised by fetch_calendar when a live fetch fails and there is no
    cached data to fall back on -- the "we genuinely have no idea" case
    is_news_blackout fails CLOSED on, as opposed to a graceful degrade to
    slightly-stale cached events (still real data, not a blind guess)."""


def _fetch_live_events(api_key: str) -> list[dict]:
    """The actual Finnhub HTTP call -- no caching, no stale-data fallback,
    raises on any failure. Used by fetch_calendar (which wraps this with
    caching and a stale-cache degrade) and by startup_self_test (which
    wants to know whether the LIVE connection actually works right now, not
    whether a cache happens to exist from a previous successful day)."""
    sys.path.insert(0, str(AGENT_DIR))
    from backtest.loaders._http import resolve_min_interval, throttled_get_json

    start = (datetime.now(timezone.utc) - timedelta(days=_FETCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=_FETCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    payload = throttled_get_json(
        _CALENDAR_URL,
        host_key=_FINNHUB_HOST_KEY,
        min_interval=resolve_min_interval(_FINNHUB_MIN_INTERVAL_ENV, _FINNHUB_DEFAULT_MIN_INTERVAL),
        params={"from": start, "to": end, "token": api_key},
        timeout=_FINNHUB_TIMEOUT_S,
    )
    raw_events = payload.get("economicCalendar") if isinstance(payload, dict) else None
    return _normalize_events(raw_events if isinstance(raw_events, list) else [])


def fetch_calendar(*, force: bool = False) -> list[dict]:
    """Return today's cached (or freshly fetched) high-impact economic events.

    Refetches at most once per UTC calendar day (not once per pass — a
    committee pass runs every couple of hours, an economic calendar doesn't
    change that often). Each event: {"time": iso str, "currency": str,
    "impact": str, "event": str}.

    Returns [] on a missing API key (available() already told the caller
    that). On a fetch/parse error: returns the last cached events if any
    exist (stale-but-real data, a reasonable degrade), or raises
    CalendarUnavailable if there is nothing cached at all -- see that
    class's docstring and is_news_blackout for why those two cases are
    treated differently.
    """
    api_key = _api_key()
    if not api_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache = _read_cache()
    if not force and cache.get("fetched_date") == today and isinstance(cache.get("events"), list):
        return cache["events"]

    try:
        events = _fetch_live_events(api_key)
    except Exception as exc:
        cached = cache.get("events")
        if cached is not None:
            # Stale but real -- today's high-impact calendar rarely changes
            # hour to hour, so yesterday's fetch is still a reasonable signal.
            return cached
        # _redact strips the token query param BEFORE this string exists
        # anywhere else -- is_news_blackout's reason flows straight into the
        # emailed report, and a bare requests exception embeds the full
        # request URL (token included). See _redact's own docstring for the
        # real incident this fixes (2026-10-02/10-05, found alongside D1).
        raise CalendarUnavailable(f"Finnhub fetch failed and no cached events exist: {_redact(str(exc))}") from exc

    _write_cache({"fetched_date": today, "events": events})
    return events


def startup_self_test(*, alert_fn=None) -> bool:
    """Call once per loop process, before its first pass. Makes one live,
    uncached Finnhub call and confirms it actually succeeds.

    Real incident: the key started returning 403 Forbidden on 2026-10-02 and
    nothing caught it -- the fail-closed fix (correctly) blocked trading
    pass after pass, but silently, for four trading days before anyone
    noticed. This exists so a dead/misconfigured key is loud at boot instead
    of quiet forever.

    Returns True (nothing to test) when FINNHUB_API_KEY isn't configured at
    all -- that's a deliberate, already-known "blackout check disabled,
    fails closed every pass" state (available() is False), not something
    this self-test is for. Returns False only when a key IS configured but
    the live call still fails -- logs CRITICAL and, if `alert_fn` is given
    (the caller's own send_email, passed in rather than imported here to
    avoid an SMTP-env dependency in this module), sends an alert. The
    caller decides what to do with a False return (see each reporter's
    startup: exit(1) rather than proceed to trade on a known-broken check).
    """
    if not available():
        return True
    try:
        _fetch_live_events(_api_key())
        return True
    except Exception as exc:
        reason = _redact(str(exc))
        logger.critical("news calendar startup self-test failed: %s", reason)
        if alert_fn:
            try:
                alert_fn(
                    "[Vibe-Trading] CRITICAL: news calendar startup self-test failed",
                    "The Finnhub economic calendar self-test failed at startup:\n\n"
                    f"{reason}\n\n"
                    "This process is exiting rather than trading with a known-broken news "
                    "check. A dead/expired/rate-limited key means EVERY pass fails the news "
                    "blackout closed (no trades at all) until this is fixed -- check your "
                    "Finnhub account, rotate the key if needed (see D8 rotation steps), and "
                    "restart the reporter.",
                )
            except Exception:
                logger.exception("news calendar startup self-test: alert email failed")
        return False


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

    Fails CLOSED (returns (True, reason)) when the calendar is unreachable --
    no FINNHUB_API_KEY configured (changed 2026-10-01, rulebook v4: "if
    calendar API unreachable, PASS, don't fail open" -- the committee has no
    way to see whether this check even ran, so the earlier "fail open" meant
    an unconfigured/broken calendar silently produced an all-clear forever).
    This only skips the CURRENT pass, not a persistent halt -- the next
    scheduled pass re-checks, so a lapsed key costs missed trades, not a
    stuck kill switch. A real fetch that genuinely finds zero matching
    events (API working, just nothing scheduled) still returns (False, None)
    below -- only "we have no way to know" fails closed, not "nothing's on
    the calendar."
    """
    if not available():
        return True, "news calendar unavailable (no FINNHUB_API_KEY) — failing closed (rulebook v4)"

    now = now or datetime.now(timezone.utc)
    try:
        events = fetch_calendar()
    except CalendarUnavailable as exc:
        # A configured key whose live fetch is actually failing (outage,
        # rate limit, timeout) with nothing cached to fall back on is the
        # same "we have no way to know" case as no key at all -- fails
        # closed the same way, not open.
        return True, f"{exc} — failing closed (rulebook v4)"
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
