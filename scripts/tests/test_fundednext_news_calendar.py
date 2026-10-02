"""Tests for scripts/fundednext_news_calendar.py.

No network call is ever made for real: throttled_get_json is monkeypatched
whenever fetch_calendar would otherwise reach it. CACHE_PATH is monkeypatched
to a tmp_path location in every test that touches it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import fundednext_news_calendar as fn_news

pytestmark = pytest.mark.unit


class TestAvailable:
    def test_true_when_key_set(self, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        assert fn_news.available() is True

    def test_false_when_key_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        assert fn_news.available() is False


class TestIsNewsBlackoutFailsClosed:
    def test_no_api_key_fails_closed(self, monkeypatch) -> None:
        # Changed 2026-10-01 (rulebook v4): an unreachable calendar used to
        # silently report "all clear" forever; it now blocks the current
        # pass instead, since the committee has no way to tell the check
        # even ran.
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        blackout, why = fn_news.is_news_blackout({"EUR", "USD"})
        assert blackout is True
        assert why is not None

    def test_live_fetch_failure_with_no_cache_fails_closed(self, monkeypatch) -> None:
        # A configured key whose live fetch is actually failing (outage,
        # rate limit, timeout) with nothing cached is the same "we don't
        # know" case as no key at all -- code review 2026-10-02 caught this
        # still failing open (fetch_calendar swallowed the exception and
        # returned []), which would have silently defeated the fix above.
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "fetch_calendar",
                             lambda **kw: (_ for _ in ()).throw(fn_news.CalendarUnavailable("Finnhub down")))
        blackout, why = fn_news.is_news_blackout({"EUR", "USD"})
        assert blackout is True
        assert "Finnhub down" in why and "failing closed" in why


class TestIsNewsBlackoutWithEvents:
    def _patch(self, monkeypatch, events: list[dict]) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "fetch_calendar", lambda **kwargs: events)

    def test_within_window_before_event(self, monkeypatch) -> None:
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        event_time = now + timedelta(minutes=3)
        self._patch(monkeypatch, [{"time": event_time.isoformat(), "currency": "USD", "impact": "high", "event": "NFP"}])

        blackout, why = fn_news.is_news_blackout({"EUR", "USD"}, now=now, window_minutes=5)
        assert blackout is True
        assert "USD" in why

    def test_within_window_after_event(self, monkeypatch) -> None:
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        event_time = now - timedelta(minutes=4)
        self._patch(monkeypatch, [{"time": event_time.isoformat(), "currency": "EUR", "impact": "high", "event": "CPI"}])

        blackout, _why = fn_news.is_news_blackout({"EUR", "USD"}, now=now, window_minutes=5)
        assert blackout is True

    def test_outside_window(self, monkeypatch) -> None:
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        event_time = now + timedelta(minutes=30)
        self._patch(monkeypatch, [{"time": event_time.isoformat(), "currency": "USD", "impact": "high", "event": "NFP"}])

        blackout, why = fn_news.is_news_blackout({"EUR", "USD"}, now=now, window_minutes=5)
        assert blackout is False and why is None

    def test_different_currency_ignored(self, monkeypatch) -> None:
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        event_time = now + timedelta(minutes=1)
        self._patch(monkeypatch, [{"time": event_time.isoformat(), "currency": "JPY", "impact": "high", "event": "BOJ rate"}])

        blackout, _why = fn_news.is_news_blackout({"EUR", "USD"}, now=now, window_minutes=5)
        assert blackout is False

    def test_unparseable_event_time_skipped(self, monkeypatch) -> None:
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self._patch(monkeypatch, [{"time": "not-a-date", "currency": "USD", "impact": "high", "event": "NFP"}])

        blackout, _why = fn_news.is_news_blackout({"USD"}, now=now, window_minutes=5)
        assert blackout is False


class TestNormalizeEvents:
    def test_filters_to_high_impact_only(self) -> None:
        raw = [
            {"time": "2026-09-12T12:00:00Z", "currency": "USD", "impact": "high", "event": "NFP"},
            {"time": "2026-09-12T13:00:00Z", "currency": "USD", "impact": "low", "event": "minor release"},
        ]
        out = fn_news._normalize_events(raw)
        assert len(out) == 1
        assert out[0]["event"] == "NFP"

    def test_drops_rows_missing_time_or_currency(self) -> None:
        raw = [{"time": None, "currency": "USD", "impact": "high"}, {"time": "2026-09-12T12:00:00Z", "currency": None, "impact": "high"}]
        assert fn_news._normalize_events(raw) == []

    def test_non_dict_rows_skipped(self) -> None:
        assert fn_news._normalize_events(["not a dict", 123]) == []


class TestFetchCalendarCaching:
    def test_uses_cache_within_same_day_without_network_call(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fn_news._write_cache({"fetched_date": today, "events": [{"time": "x", "currency": "USD", "impact": "high", "event": "cached"}]})

        result = fn_news.fetch_calendar()
        assert result == [{"time": "x", "currency": "USD", "impact": "high", "event": "cached"}]

    def test_no_key_returns_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        assert fn_news.fetch_calendar() == []

    def _patch_throttled_get_json(self, monkeypatch, fn) -> None:
        import sys
        sys.path.insert(0, str(fn_news.AGENT_DIR))
        import backtest.loaders._http as http_mod
        monkeypatch.setattr(http_mod, "throttled_get_json", fn)

    def test_live_fetch_failure_raises_when_no_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        self._patch_throttled_get_json(monkeypatch, lambda **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

        with pytest.raises(fn_news.CalendarUnavailable):
            fn_news.fetch_calendar()

    def test_live_fetch_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        fn_news._write_cache({"fetched_date": "2020-01-01",  # deliberately stale
                               "events": [{"time": "x", "currency": "USD", "impact": "high", "event": "stale"}]})
        self._patch_throttled_get_json(monkeypatch, lambda **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

        result = fn_news.fetch_calendar()
        assert result == [{"time": "x", "currency": "USD", "impact": "high", "event": "stale"}]
