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
    def test_no_cached_or_live_data_fails_closed(self, monkeypatch) -> None:
        # Changed 2026-10-01 (rulebook v4): an unreachable calendar used to
        # silently report "all clear" forever; it now blocks the current
        # pass instead, since the committee has no way to tell the check
        # even ran. Since 2026-10-06 (ForexFactory no-key fallback), a
        # missing Finnhub key ALONE no longer triggers this -- see
        # TestIsNewsBlackoutNoFinnhubKeyUsesForexFactory below -- only both
        # sources failing live with nothing cached does.
        monkeypatch.setattr(fn_news, "fetch_calendar",
                             lambda **kw: (_ for _ in ()).throw(fn_news.CalendarUnavailable("both sources down")))
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


class TestIsNewsBlackoutNoFinnhubKeyUsesForexFactory:
    """2026-10-06: a missing/paid-tier-gated Finnhub key no longer fails
    closed on its own -- the ForexFactory fallback needs no key, so the
    blackout check still runs normally as long as THAT succeeds."""

    def test_no_finnhub_key_but_calendar_still_resolves(self, monkeypatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "fetch_calendar", lambda **kw: [])
        blackout, why = fn_news.is_news_blackout({"EUR", "USD"})
        assert blackout is False
        assert why is None


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

    def test_accepts_forexfactory_field_names(self) -> None:
        # ForexFactory: "date"/"country"/"title" instead of Finnhub's
        # "time"/"currency"/"event", capitalized impact.
        raw = [{"date": "2026-10-07T14:00:00-04:00", "country": "USD", "impact": "High", "title": "FOMC Minutes"}]
        out = fn_news._normalize_events(raw)
        assert out == [{"time": "2026-10-07T14:00:00-04:00", "currency": "USD", "impact": "high", "event": "FOMC Minutes"}]

    def test_forexfactory_holiday_impact_excluded(self) -> None:
        raw = [{"date": "2026-10-07T00:00:00-04:00", "country": "All", "impact": "Holiday", "title": "Columbus Day"}]
        assert fn_news._normalize_events(raw) == []


class TestFetchForexFactoryEvents:
    def _patch_throttled_get_json(self, monkeypatch, fn) -> None:
        import sys
        sys.path.insert(0, str(fn_news.AGENT_DIR))
        import backtest.loaders._http as http_mod
        monkeypatch.setattr(http_mod, "throttled_get_json", fn)

    def test_parses_live_payload(self, monkeypatch) -> None:
        payload = [
            {"title": "FOMC Meeting Minutes", "country": "USD", "date": "2026-10-07T14:00:00-04:00",
             "impact": "High", "forecast": "", "previous": ""},
            {"title": "Minor release", "country": "EUR", "date": "2026-10-07T09:00:00-04:00",
             "impact": "Low", "forecast": "", "previous": ""},
        ]
        self._patch_throttled_get_json(monkeypatch, lambda *a, **kw: payload)
        out = fn_news._fetch_forexfactory_events()
        assert out == [{"time": "2026-10-07T14:00:00-04:00", "currency": "USD", "impact": "high", "event": "FOMC Meeting Minutes"}]

    def test_non_list_payload_yields_no_events(self, monkeypatch) -> None:
        self._patch_throttled_get_json(monkeypatch, lambda *a, **kw: {"unexpected": "shape"})
        assert fn_news._fetch_forexfactory_events() == []

    def test_propagates_http_failure(self, monkeypatch) -> None:
        self._patch_throttled_get_json(monkeypatch, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("503")))
        with pytest.raises(RuntimeError):
            fn_news._fetch_forexfactory_events()


class TestFetchLiveEventsAnySource:
    def test_uses_finnhub_when_key_configured_and_working(self, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "_fetch_live_events", lambda api_key: [{"time": "x", "currency": "USD", "impact": "high", "event": "finnhub"}])
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events",
                             lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
        events, source = fn_news._fetch_live_events_any_source()
        assert source == "finnhub"
        assert events[0]["event"] == "finnhub"

    def test_falls_back_to_forexfactory_when_no_key(self, monkeypatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: [{"time": "x", "currency": "USD", "impact": "high", "event": "ff"}])
        events, source = fn_news._fetch_live_events_any_source()
        assert source == "forexfactory"
        assert events[0]["event"] == "ff"

    def test_falls_back_to_forexfactory_when_finnhub_fails(self, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "_fetch_live_events", lambda api_key: (_ for _ in ()).throw(RuntimeError("403")))
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: [{"time": "x", "currency": "USD", "impact": "high", "event": "ff"}])
        events, source = fn_news._fetch_live_events_any_source()
        assert source == "forexfactory"

    def test_raises_combined_error_when_both_fail(self, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "_fetch_live_events", lambda api_key: (_ for _ in ()).throw(RuntimeError("finnhub down")))
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: (_ for _ in ()).throw(RuntimeError("ff down")))
        with pytest.raises(fn_news.CalendarUnavailable) as exc_info:
            fn_news._fetch_live_events_any_source()
        assert "finnhub down" in str(exc_info.value)
        assert "ff down" in str(exc_info.value)

    def test_no_key_message_when_both_fail(self, monkeypatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: (_ for _ in ()).throw(RuntimeError("ff down")))
        with pytest.raises(fn_news.CalendarUnavailable) as exc_info:
            fn_news._fetch_live_events_any_source()
        assert "no FINNHUB_API_KEY configured" in str(exc_info.value)


class TestFetchCalendarCaching:
    def test_uses_cache_within_same_day_without_network_call(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fn_news._write_cache({"fetched_date": today, "events": [{"time": "x", "currency": "USD", "impact": "high", "event": "cached"}]})

        result = fn_news.fetch_calendar()
        assert result == [{"time": "x", "currency": "USD", "impact": "high", "event": "cached"}]

    def test_no_key_falls_back_to_forexfactory(self, tmp_path, monkeypatch) -> None:
        # 2026-10-06: no Finnhub key no longer short-circuits to [] -- the
        # ForexFactory fallback (needs no key) is tried instead.
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        ff_events = [{"time": "2026-10-06T12:30:00+00:00", "currency": "USD", "impact": "high", "event": "FOMC"}]
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: ff_events)

        result = fn_news.fetch_calendar()
        assert result == ff_events
        assert fn_news._read_cache()["source"] == "forexfactory"

    def test_finnhub_failure_falls_back_to_forexfactory_live(self, tmp_path, monkeypatch) -> None:
        # A configured key whose live Finnhub call fails (e.g. the
        # 2026-10-06 "paid plan only" 403) still gets a real answer from
        # ForexFactory instead of failing closed.
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr(fn_news, "_fetch_live_events",
                             lambda api_key: (_ for _ in ()).throw(RuntimeError("403 Forbidden")))
        ff_events = [{"time": "2026-10-06T12:30:00+00:00", "currency": "EUR", "impact": "high", "event": "ECB"}]
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: ff_events)

        result = fn_news.fetch_calendar()
        assert result == ff_events
        assert fn_news._read_cache()["source"] == "forexfactory"

    def test_both_sources_fail_raises_when_no_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events",
                             lambda: (_ for _ in ()).throw(RuntimeError("down")))

        with pytest.raises(fn_news.CalendarUnavailable):
            fn_news.fetch_calendar()

    def _patch_throttled_get_json(self, monkeypatch, fn) -> None:
        import sys
        sys.path.insert(0, str(fn_news.AGENT_DIR))
        import backtest.loaders._http as http_mod
        monkeypatch.setattr(http_mod, "throttled_get_json", fn)

    def test_live_fetch_failure_raises_when_no_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        self._patch_throttled_get_json(monkeypatch, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

        with pytest.raises(fn_news.CalendarUnavailable):
            fn_news.fetch_calendar()

    def test_live_fetch_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        fn_news._write_cache({"fetched_date": "2020-01-01",  # deliberately stale
                               "events": [{"time": "x", "currency": "USD", "impact": "high", "event": "stale"}]})
        self._patch_throttled_get_json(monkeypatch, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

        result = fn_news.fetch_calendar()
        assert result == [{"time": "x", "currency": "USD", "impact": "high", "event": "stale"}]


class TestRedact:
    """D1 (2026-10-06): a live fetch failure's exception text embeds the
    full request URL, token query param included -- confirmed leaking into
    both logs and the emailed report on 2026-10-02/10-05. _redact strips it
    before the string is ever constructed into a message."""

    def test_strips_token_query_param(self) -> None:
        url = "https://finnhub.io/api/v1/calendar/economic?from=2026-10-03&to=2026-10-07&token=abc123secret"
        redacted = fn_news._redact(url)
        assert "abc123secret" not in redacted
        assert "token=***REDACTED***" in redacted
        assert "from=2026-10-03" in redacted  # non-secret params survive

    def test_case_insensitive_and_leading_ampersand(self) -> None:
        redacted = fn_news._redact("...&TOKEN=SuperSecret123&other=1")
        assert "SuperSecret123" not in redacted

    def test_leaves_token_free_text_unchanged(self) -> None:
        assert fn_news._redact("Finnhub fetch failed: timeout") == "Finnhub fetch failed: timeout"

    def test_calendar_unavailable_message_is_already_redacted(self, tmp_path, monkeypatch) -> None:
        """Regression for the actual incident: raise with a fake token in
        the underlying exception and confirm it never reaches the raised
        CalendarUnavailable's own message."""
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        fake_url_error = RuntimeError(
            "403 Client Error: Forbidden for url: "
            "https://finnhub.io/api/v1/calendar/economic?from=2026-10-03&to=2026-10-07&token=dam5dqhr01live"
        )
        import sys
        sys.path.insert(0, str(fn_news.AGENT_DIR))
        import backtest.loaders._http as http_mod
        monkeypatch.setattr(http_mod, "throttled_get_json", lambda *a, **kw: (_ for _ in ()).throw(fake_url_error))

        with pytest.raises(fn_news.CalendarUnavailable) as exc_info:
            fn_news.fetch_calendar()
        assert "dam5dqhr01live" not in str(exc_info.value)
        assert "token=***REDACTED***" in str(exc_info.value)


class TestMaskSecretsFilter:
    def test_masks_token_in_log_message_args(self, caplog) -> None:
        import logging as _logging
        logger = _logging.getLogger("test_mask_secrets")
        logger.addFilter(fn_news.mask_secrets())
        with caplog.at_level(_logging.WARNING, logger="test_mask_secrets"):
            logger.warning("news blackout for %s: %s", "EURUSD", "...&token=leakedsecret99 failed")
        assert "leakedsecret99" not in caplog.text
        assert "token=***REDACTED***" in caplog.text

    def test_does_not_choke_on_non_string_args(self, caplog) -> None:
        import logging as _logging
        logger = _logging.getLogger("test_mask_secrets_nonstring")
        logger.addFilter(fn_news.mask_secrets())
        with caplog.at_level(_logging.INFO, logger="test_mask_secrets_nonstring"):
            logger.info("count=%s", 42)  # must not raise on a non-str arg
        assert "count=42" in caplog.text


class TestStartupSelfTest:
    def test_true_when_no_key_but_forexfactory_succeeds(self, monkeypatch) -> None:
        # 2026-10-06: no Finnhub key alone no longer fails the self-test --
        # the ForexFactory fallback needs no key, so as long as IT answers
        # live, the pipeline is considered healthy.
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events", lambda: [])
        called = []
        assert fn_news.startup_self_test(alert_fn=lambda *a: called.append(a)) is True
        assert called == []

    def test_true_when_live_fetch_succeeds(self, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "_fetch_live_events", lambda api_key: [])
        called = []
        assert fn_news.startup_self_test(alert_fn=lambda *a: called.append(a)) is True
        assert called == []

    def test_false_and_alerts_when_live_fetch_fails(self, monkeypatch, caplog) -> None:
        import logging as _logging
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(
            fn_news, "_fetch_live_events",
            lambda api_key: (_ for _ in ()).throw(RuntimeError("403 Forbidden ...&token=livesecret")),
        )
        # Both sources must fail for the self-test to fail -- the
        # ForexFactory fallback is tried after Finnhub.
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events",
                             lambda: (_ for _ in ()).throw(RuntimeError("also down")))
        alerts = []
        with caplog.at_level(_logging.CRITICAL):
            result = fn_news.startup_self_test(alert_fn=lambda subject, body: alerts.append((subject, body)))

        assert result is False
        assert len(alerts) == 1
        assert "CRITICAL" in alerts[0][0]
        # The token must not reach the alert email either.
        assert "livesecret" not in alerts[0][1]
        assert "livesecret" not in caplog.text

    def test_false_when_alert_fn_itself_raises(self, monkeypatch) -> None:
        # An alert-sending failure (e.g. SMTP down too) must not mask the
        # real self-test failure as a crash -- still returns False cleanly.
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "_fetch_live_events", lambda api_key: (_ for _ in ()).throw(RuntimeError("down")))
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events",
                             lambda: (_ for _ in ()).throw(RuntimeError("also down")))

        def _broken_alert(subject, body):
            raise ConnectionError("SMTP also down")

        assert fn_news.startup_self_test(alert_fn=_broken_alert) is False

    def test_false_with_no_alert_fn_given(self, monkeypatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "_fetch_live_events", lambda api_key: (_ for _ in ()).throw(RuntimeError("down")))
        monkeypatch.setattr(fn_news, "_fetch_forexfactory_events",
                             lambda: (_ for _ in ()).throw(RuntimeError("also down")))
        assert fn_news.startup_self_test(alert_fn=None) is False


class TestNewsApiStatus:
    """D6/v5.1 (2026-10-06): feeds the committee prompt's NEWS_API_STATUS
    field -- no extra network call, derived from the existing cache."""

    def test_unavailable_when_no_key(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        assert fn_news.news_api_status() == "UNAVAILABLE"

    def test_ok_when_cache_is_fresh_today(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fn_news._write_cache({"fetched_date": today, "events": []})
        assert fn_news.news_api_status() == "OK"

    def test_stale_when_cache_is_from_a_prior_day(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        fn_news._write_cache({"fetched_date": "2020-01-01", "events": [{"time": "x", "currency": "USD",
                                                                          "impact": "high", "event": "old"}]})
        assert fn_news.news_api_status() == "STALE"

    def test_unavailable_when_key_set_but_no_cache_at_all(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
        monkeypatch.setattr(fn_news, "CACHE_PATH", tmp_path / "cache.json")
        assert fn_news.news_api_status() == "UNAVAILABLE"
