"""Tests for scripts/committee_reporter.py.

This script is the single place that decides AND (conditionally) places
live orders (see its module docstring), but had zero test coverage until
now -- three bugs shipped straight to the live account this way: a stale
MT5 connection handle, a retry-dedup swallowing a gate-blocked order, and
journal reconciliation mislabeling a still-resting pending order as closed
(see _journal_reconcile_closed below, and git blame on this test file's
sibling commit for the incident).

Every test that touches one of the module's *_PATH constants (TRADE_
JOURNAL_PATH, LOCK_PATH, WEEKEND_STATE_PATH, REPORTER_LOG_PATH, ...) MUST
monkeypatch it to a tmp_path location first -- those constants point at the
real logs/ directory this same repo's live trading loop reads and writes,
and a test that forgot to redirect them would corrupt live state.

No network/MT5/subprocess/SMTP call is ever made for real here: every
external read (get_positions, get_open_orders, get_account, contract_size,
get_historical_bars(_range), _trace_entries) is monkeypatched with a fake
matching the shape committee_reporter.py actually consumes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import committee_reporter as cr

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _in_weekend_window
# ---------------------------------------------------------------------------


class TestInWeekendWindow:
    def test_saturday(self) -> None:
        assert cr._in_weekend_window(datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc))

    def test_sunday(self) -> None:
        assert cr._in_weekend_window(datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc))

    def test_friday_before_cutoff(self) -> None:
        assert not cr._in_weekend_window(datetime(2026, 9, 4, 19, 59, tzinfo=timezone.utc))

    def test_friday_at_cutoff(self) -> None:
        assert cr._in_weekend_window(datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc))

    def test_friday_after_cutoff(self) -> None:
        assert cr._in_weekend_window(datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc))

    def test_weekday(self) -> None:
        assert not cr._in_weekend_window(datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# _next_session_boundary -- session-gated trading (2026-09-21): 3 passes/
# day anchored to session opens, only "new_york" trades. Must stay correct
# across DST transitions (America/New_York and Europe/London both observe
# it, on different dates) without a hardcoded UTC offset.
# ---------------------------------------------------------------------------


class TestNextSessionBoundary:
    def test_winter_standard_time_offsets(self) -> None:
        """Mid-January: both London (GMT, UTC+0) and New York (EST, UTC-5)
        are in standard time."""
        now = datetime(2026, 1, 15, 0, 0, 1, tzinfo=timezone.utc)  # just after Asia's 00:00 boundary
        boundary, session = cr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc), "london")

        now = boundary + timedelta(seconds=1)
        boundary, session = cr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc), "new_york")

    def test_summer_dst_offsets(self) -> None:
        """Mid-June: both London (BST, UTC+1) and New York (EDT, UTC-4)
        are in daylight time."""
        now = datetime(2026, 6, 15, 0, 0, 1, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc), "london")

        now = boundary + timedelta(seconds=1)
        boundary, session = cr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), "new_york")

    def test_before_asia_open_returns_asia(self) -> None:
        now = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc) - timedelta(seconds=1)
        boundary, session = cr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc), "asia")

    def test_after_last_boundary_rolls_to_tomorrows_asia(self) -> None:
        # 2026-09-21 is a weekday in BST/EDT: NY opens 12:00 UTC that day.
        now = datetime(2026, 9, 21, 12, 0, 1, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc), "asia")

    def test_exactly_at_a_boundary_is_not_returned_again(self) -> None:
        """A boundary equal to `now` must not be selected as "next" -- the
        caller (main()'s loop) treats now_utc >= next_boundary as "fire
        now", so the boundary that just fired must roll forward, not repeat."""
        asia_open = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(asia_open)
        assert session != "asia"

    def test_america_new_york_dst_spring_forward_2026(self) -> None:
        """US DST starts 2026-03-08 (2nd Sunday of March) -- the day before
        is still EST (UTC-5), the day itself/after is EDT (UTC-4)."""
        before = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(before)
        assert (boundary, session) == (datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc), "new_york")

        after = datetime(2026, 3, 9, 11, 59, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(after)
        assert (boundary, session) == (datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc), "new_york")

    def test_europe_london_dst_spring_forward_2026(self) -> None:
        """UK DST (BST) starts 2026-03-29 (last Sunday of March)."""
        before = datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(before)
        assert (boundary, session) == (datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc), "london")

        after = datetime(2026, 3, 30, 1, 0, tzinfo=timezone.utc)
        boundary, session = cr._next_session_boundary(after)
        assert (boundary, session) == (datetime(2026, 3, 30, 7, 0, tzinfo=timezone.utc), "london")


# ---------------------------------------------------------------------------
# _check_trade_drought -- regression: this used to fetch executions via mt5_
# sdk's own fixed 7-day default lookback, the SAME length as NO_TRADE_ALERT_
# DAYS itself. Once a symbol's last trade aged past that same ~7-day window,
# the deal proving the drought fell OUTSIDE the fetch window too --
# "executions" came back empty, and the function read that as "never traded,
# nothing to check," silently never alerting on exactly the multi-week
# droughts this feature exists to catch.
# ---------------------------------------------------------------------------


class TestCheckTradeDrought:
    SYMBOL = "EURUSDm"
    CONNECTION = "mt5-live-trade"

    def _patch(self, monkeypatch, tmp_path, *, executions):
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles_module

        monkeypatch.setattr(
            cr, "TARGETS",
            [{
                "committee": "x", "target": "x", "market": "forex",
                "trade": {"symbol": self.SYMBOL, "connection": self.CONNECTION, "lots": 0.01},
            }],
        )
        monkeypatch.setattr(cr, "NO_TRADE_ALERT_STATE_PATH", tmp_path / "no_trade_alert_state.json")

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")

        captured: dict = {}

        def _get_open_orders(config, *, include_executions=False, executions_lookback_days=7):
            captured["executions_lookback_days"] = executions_lookback_days
            return {"executions": executions}

        monkeypatch.setattr(mt5_sdk, "get_open_orders", _get_open_orders)

        emailed: list[tuple[str, str]] = []
        monkeypatch.setattr(cr, "send_email", lambda subject, text: emailed.append((subject, text)))
        return captured, emailed

    def test_requests_a_lookback_wider_than_the_alert_threshold(self, monkeypatch, tmp_path) -> None:
        captured, _ = self._patch(monkeypatch, tmp_path, executions=[])
        cr._check_trade_drought()
        assert captured["executions_lookback_days"] > cr.NO_TRADE_ALERT_DAYS

    def test_alerts_on_a_drought_older_than_the_old_fixed_7_day_window(self, monkeypatch, tmp_path) -> None:
        """The real bug: a deal that opened 10 days ago used to fall outside
        the connector's old fixed 7-day executions window, so this silently
        never alerted on a drought of exactly the kind NO_TRADE_ALERT_DAYS=7
        exists to catch."""
        last_trade_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        executions = [{"symbol": self.SYMBOL, "magic": cr.OUR_MAGIC, "entry": 0, "time": last_trade_time}]
        _, emailed = self._patch(monkeypatch, tmp_path, executions=executions)
        cr._check_trade_drought()
        assert len(emailed) == 1
        assert "No trades in" in emailed[0][0]

    def test_no_trade_history_at_all_does_not_alert(self, monkeypatch, tmp_path) -> None:
        _, emailed = self._patch(monkeypatch, tmp_path, executions=[])
        cr._check_trade_drought()
        assert emailed == []


# ---------------------------------------------------------------------------
# Session bias carry-over: Asia/London (research-only) passes' Decision/
# Reasoning get recorded and read back into the New York (trading) pass's
# prompt instead of being thrown away.
# ---------------------------------------------------------------------------


class TestParseDecisionReasoning:
    def test_parses_well_formed_report(self) -> None:
        text = "Decision: long, momentum favors EURUSD\nReasoning: broke resistance on volume\nConfidence: high"
        assert cr._parse_decision_reasoning(text) == ("long, momentum favors EURUSD", "broke resistance on volume")

    def test_missing_decision_returns_none(self) -> None:
        assert cr._parse_decision_reasoning("Reasoning: because\nConfidence: high") is None

    def test_missing_reasoning_returns_none(self) -> None:
        assert cr._parse_decision_reasoning("Decision: wait\nConfidence: high") is None

    def test_garbage_text_returns_none(self) -> None:
        assert cr._parse_decision_reasoning("the committee had an error") is None


class TestSessionBiasReadWrite:
    def test_record_then_fact_round_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        cr._record_session_bias("EURUSDm", "asia", "Decision: long\nReasoning: broke resistance")

        fact = cr._session_bias_fact("EURUSDm")

        assert "Asia session read on EURUSDm today" in fact
        assert "long" in fact and "broke resistance" in fact

    def test_both_sessions_appear_when_both_recorded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        cr._record_session_bias("EURUSDm", "asia", "Decision: long\nReasoning: asia reason")
        cr._record_session_bias("EURUSDm", "london", "Decision: short\nReasoning: london reason")

        fact = cr._session_bias_fact("EURUSDm")

        assert "Asia session read" in fact and "asia reason" in fact
        assert "London session read" in fact and "london reason" in fact

    def test_no_entries_returns_empty_string(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        assert cr._session_bias_fact("EURUSDm") == ""

    def test_stale_prior_day_entry_is_ignored(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "session_bias.json"
        monkeypatch.setattr(cr, "SESSION_BIAS_STATE_PATH", path)
        path.write_text(
            json.dumps({"EURUSDm": {"asia": {"date": "2020-01-01", "decision": "long", "reasoning": "old"}}}),
            encoding="utf-8",
        )
        assert cr._session_bias_fact("EURUSDm") == ""

    def test_unparseable_report_text_records_nothing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        cr._record_session_bias("EURUSDm", "asia", "the committee had an error, no structured report")
        assert cr._session_bias_fact("EURUSDm") == ""

    def test_different_symbol_does_not_leak(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        cr._record_session_bias("EURUSDm", "asia", "Decision: long\nReasoning: eur reason")
        assert cr._session_bias_fact("AUDUSDm") == ""


# ---------------------------------------------------------------------------
# run_once session gating (2026-09-21): only "new_york" trades; "asia"/
# "london" force trade=None on every TARGETS spec and record the bias.
# ---------------------------------------------------------------------------


class TestRunOnceSessionGating:
    def _patch(self, monkeypatch, *, report_text: str = "Decision: long\nReasoning: because") -> list[dict]:
        calls: list[dict] = []
        # Existing tests cover the research-pass path itself; the kill
        # switch (off in production) is covered by the tests below.
        monkeypatch.setattr(cr, "RESEARCH_PASSES_ENABLED", True)
        monkeypatch.setattr(cr, "_check_trade_drought", lambda: None)
        monkeypatch.setattr(cr, "_check_cap_fit_alert", lambda: None)
        monkeypatch.setattr(cr, "_check_llm_balance_alert", lambda: None)
        monkeypatch.setattr(cr, "_log_cap_gap", lambda: None)
        monkeypatch.setattr(cr, "is_reportable", lambda result: False)
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda symbol, connection: {"count": 0, "side": None})
        monkeypatch.setattr(
            cr, "TARGETS",
            [{
                "committee": "investment_committee", "target": "EURUSD", "market": "forex",
                "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1},
            }],
        )

        def _fake_run_committee(**kwargs):
            calls.append(kwargs)
            return cr.CommitteeResult(
                committee=kwargs["committee"], target=kwargs["target"], market=kwargs["market"],
                status="success", run_id="r1", report_text=report_text, traded=False,
            )

        monkeypatch.setattr(cr, "run_committee", _fake_run_committee)
        return calls

    def test_asia_session_strips_trade_and_records_bias(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        recorded = []
        monkeypatch.setattr(cr, "_record_session_bias", lambda symbol, session, text: recorded.append((symbol, session, text)))

        cr.run_once("asia")

        assert calls[0]["trade"] is None
        assert recorded == [("EURUSDm", "asia", "Decision: long\nReasoning: because")]

    def test_london_session_strips_trade(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        monkeypatch.setattr(cr, "_record_session_bias", lambda *a: None)

        cr.run_once("london")

        assert calls[0]["trade"] is None

    def test_new_york_session_keeps_trade_and_does_not_record_bias(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        recorded = []
        monkeypatch.setattr(cr, "_record_session_bias", lambda *a: recorded.append(a))

        cr.run_once("new_york")

        assert calls[0]["trade"] == {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}
        assert recorded == []

    def test_research_passes_disabled_skips_committee_on_asia_and_london(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        monkeypatch.setattr(cr, "RESEARCH_PASSES_ENABLED", False)
        recorded = []
        monkeypatch.setattr(cr, "_record_session_bias", lambda *a: recorded.append(a))

        cr.run_once("asia")
        cr.run_once("london")

        assert calls == []
        assert recorded == []

    def test_research_passes_disabled_still_runs_new_york(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        monkeypatch.setattr(cr, "RESEARCH_PASSES_ENABLED", False)
        monkeypatch.setattr(cr, "_record_session_bias", lambda *a: None)

        cr.run_once("new_york")

        assert len(calls) == 1
        assert calls[0]["trade"] is not None

    def test_research_passes_disabled_still_runs_alerts(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        monkeypatch.setattr(cr, "RESEARCH_PASSES_ENABLED", False)
        fired = []
        monkeypatch.setattr(cr, "_check_llm_balance_alert", lambda: fired.append("balance"))
        monkeypatch.setattr(cr, "_check_trade_drought", lambda: fired.append("drought"))

        cr.run_once("london")

        assert fired == ["drought", "balance"]

    def test_targets_list_itself_is_never_mutated(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        monkeypatch.setattr(cr, "_record_session_bias", lambda *a: None)
        original_trade = cr.TARGETS[0]["trade"]

        cr.run_once("asia")

        assert cr.TARGETS[0]["trade"] is original_trade


# ---------------------------------------------------------------------------
# _read_journal / _write_journal
# ---------------------------------------------------------------------------


class TestJournalReadWrite:
    def test_round_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        entries = [{"ticket": "1", "symbol": "EURUSDm", "status": "open"}]
        cr._write_journal(entries)
        assert cr._read_journal() == entries

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "does_not_exist.json")
        assert cr._read_journal() == []

    def test_corrupt_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "journal.json"
        path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", path)
        assert cr._read_journal() == []

    def test_write_creates_parent_dir(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "nested" / "journal.json"
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", path)
        cr._write_journal([{"ticket": "1"}])
        assert path.exists()


# ---------------------------------------------------------------------------
# _journal_summary_text
# ---------------------------------------------------------------------------


class TestJournalSummaryText:
    def test_none_when_no_closed_trades(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([{"symbol": "EURUSDm", "status": "open"}])
        assert cr._journal_summary_text("EURUSDm") is None

    def test_reversal_note_does_not_bias_toward_stop_management(self, tmp_path, monkeypatch) -> None:
        """D5 (2026-10-06): the old wording ("a stop-management issue, not
        an entry-quality one") biased the committee toward active stop
        management on exactly the pattern the replay evidence (n=39) shows
        is negative-expectancy to touch. New wording states the count
        neutrally and points to entry/stop-sizing discipline instead."""
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([
            {"symbol": "EURUSDm", "status": "closed", "outcome": "loss", "profit": -0.5, "side": "buy",
             "excursion_tag": "reversal"},
            {"symbol": "EURUSDm", "status": "closed", "outcome": "win", "profit": 1.0, "side": "sell",
             "excursion_tag": "clean"},
        ])
        summary = cr._journal_summary_text("EURUSDm")
        assert summary is not None
        assert "stop-management issue" not in summary
        assert "1 of last 2 closed trades moved favorably before reversing" in summary
        assert "negative-expectancy" in summary
        assert "entry/stop-sizing discipline, not exit management" in summary

    def test_no_reversal_note_when_no_reversals(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([
            {"symbol": "EURUSDm", "status": "closed", "outcome": "win", "profit": 1.0, "side": "buy",
             "excursion_tag": "clean"},
        ])
        summary = cr._journal_summary_text("EURUSDm")
        assert summary is not None
        assert "moved favorably before reversing" not in summary


# ---------------------------------------------------------------------------
# _journal_reconcile_closed
# ---------------------------------------------------------------------------


class TestJournalReconcileClosed:
    """The bug fixed today: a resting pending order must not be reconciled
    as closed just because it doesn't appear in get_positions()."""

    def _entry(self, **overrides) -> dict:
        base = {
            "ticket": "555",
            "symbol": "AUDUSDm",
            "connection": "mt5-live-trade",
            "side": "buy",
            "lots": 0.01,
            "entry_price": 0.7200,
            "stop_loss": 0.7180,
            "opened_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "status": "open",
        }
        base.update(overrides)
        return base

    def _patch_reads(self, monkeypatch, *, positions=None, open_orders=None, executions=None) -> None:
        import src.trading.service as service

        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions or []})
        monkeypatch.setattr(
            service,
            "get_open_orders",
            lambda conn, include_executions=False: {
                "open_orders": open_orders or [],
                "executions": executions or [],
            },
        )

    def test_no_open_entries_short_circuits(self, tmp_path, monkeypatch) -> None:
        """No open journal rows for the symbol -> must not even call get_positions."""
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([self._entry(symbol="EURUSDm", status="closed")])

        import src.trading.service as service

        def _boom(conn):
            raise AssertionError("get_positions should not be called")

        monkeypatch.setattr(service, "get_positions", _boom)
        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")  # must not raise

    def test_ticket_still_a_live_position_left_alone(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([self._entry(ticket="555")])
        self._patch_reads(monkeypatch, positions=[{"ticket": "555", "symbol": "AUDUSDm"}])

        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        entry = cr._read_journal()[0]
        assert entry["status"] == "open"

    def test_ticket_still_a_resting_pending_order_left_alone(self, tmp_path, monkeypatch) -> None:
        """Regression: ticket 1050385917 (AUDUSDm buy_limit @ 0.7206) was live
        on the broker but absent from get_positions() -- reconciliation must
        check get_open_orders() too, not mark it closed/unknown."""
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([self._entry(ticket="1050385917", symbol="AUDUSDm")])
        self._patch_reads(
            monkeypatch,
            positions=[],
            open_orders=[{"order_id": "1050385917", "symbol": "AUDUSDm", "side": "buy_limit"}],
        )

        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        entry = cr._read_journal()[0]
        assert entry["status"] == "open"
        assert "outcome" not in entry

    def test_within_grace_period_left_alone(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        just_opened = datetime.now(timezone.utc).isoformat()
        cr._write_journal([self._entry(ticket="555", opened_at=just_opened)])
        self._patch_reads(monkeypatch, positions=[], open_orders=[])

        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        entry = cr._read_journal()[0]
        assert entry["status"] == "open"

    def test_closed_with_matching_deal_records_real_outcome(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([self._entry(ticket="555")])
        self._patch_reads(
            monkeypatch,
            positions=[],
            open_orders=[],
            executions=[
                {
                    "magic": cr.OUR_MAGIC,
                    "position_id": "555",
                    "entry": 1,  # non-zero = a closing deal, not the opening one
                    "time": "2026-09-07T10:00:00+00:00",
                    "profit": 3.5,
                    "price": 0.7250,
                }
            ],
        )
        monkeypatch.setattr(cr, "_classify_excursion", lambda entry, deal: {})

        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        entry = cr._read_journal()[0]
        assert entry["status"] == "closed"
        assert entry["outcome"] == "win"
        assert entry["profit"] == 3.5
        assert entry["exit_price"] == 0.7250
        assert entry["closed_at"] == "2026-09-07T10:00:00+00:00"

    def test_closed_without_matching_deal_is_unknown(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([self._entry(ticket="555")])
        self._patch_reads(monkeypatch, positions=[], open_orders=[], executions=[])

        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        entry = cr._read_journal()[0]
        assert entry["status"] == "closed"
        assert entry["outcome"] == "unknown"

    def test_opening_deal_not_mistaken_for_closing_deal(self, tmp_path, monkeypatch) -> None:
        """entry == 0 marks the OPENING deal (always profit 0.0) -- must be
        ignored, not recorded as a false breakeven close."""
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        cr._write_journal([self._entry(ticket="555")])
        self._patch_reads(
            monkeypatch,
            positions=[],
            open_orders=[],
            executions=[
                {"magic": cr.OUR_MAGIC, "position_id": "555", "entry": 0, "time": "t0", "profit": 0.0},
            ],
        )

        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        entry = cr._read_journal()[0]
        assert entry["outcome"] == "unknown"  # not "breakeven"

    def test_get_positions_error_fails_open(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        original = [self._entry(ticket="555")]
        cr._write_journal(original)

        import src.trading.service as service

        def _boom(conn):
            raise RuntimeError("MT5 connection dropped")

        monkeypatch.setattr(service, "get_positions", _boom)
        cr._journal_reconcile_closed("AUDUSDm", "mt5-live-trade")

        assert cr._read_journal() == original  # untouched


# ---------------------------------------------------------------------------
# _extract_placed_order / _blocked_order_note
# ---------------------------------------------------------------------------


class TestExtractPlacedOrder:
    def test_returns_the_ok_result(self, monkeypatch) -> None:
        placed = {"status": "ok", "ticket": "1", "symbol": "EURUSDm"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [{"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(placed)}],
        )
        assert cr._extract_placed_order("run-1") == placed

    def test_none_when_no_such_tool_call(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_trace_entries", lambda run_id: [{"type": "tool_result", "tool": "trading_quote"}])
        assert cr._extract_placed_order("run-1") is None

    def test_none_when_result_not_ok(self, monkeypatch) -> None:
        blocked = {"status": "blocked", "decision": "pause_for_reauth"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [{"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(blocked)}],
        )
        assert cr._extract_placed_order("run-1") is None

    def test_none_on_unparseable_result(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [{"type": "tool_result", "tool": "trading_place_order", "result": "{not json"}],
        )
        assert cr._extract_placed_order("run-1") is None


class TestExtractPlacedOrders:
    def test_returns_every_ok_result_not_just_the_first(self, monkeypatch) -> None:
        """Regression: a pass that places two orders (e.g. the LLM misreads an
        ambiguous first result and "retries" an order that already filled)
        used to only ever have its FIRST fill checked by the post-trade
        guardrail chain / journaled -- a second live position could silently
        escape spec/trend/stop-floor/reward:risk enforcement entirely."""
        first = {"status": "ok", "ticket": "1", "symbol": "EURUSDm"}
        second = {"status": "ok", "ticket": "2", "symbol": "EURUSDm"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [
                {"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(first)},
                {"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(second)},
            ],
        )
        assert cr._extract_placed_orders("run-1") == [first, second]

    def test_skips_non_ok_results_between_ok_ones(self, monkeypatch) -> None:
        first = {"status": "ok", "ticket": "1"}
        rejected = {"status": "blocked", "reason": "denied"}
        second = {"status": "ok", "ticket": "2"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [
                {"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(first)},
                {"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(rejected)},
                {"type": "tool_result", "tool": "trading_place_order", "result": json.dumps(second)},
            ],
        )
        assert cr._extract_placed_orders("run-1") == [first, second]

    def test_empty_when_no_placements(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_trace_entries", lambda run_id: [{"type": "tool_result", "tool": "trading_quote"}])
        assert cr._extract_placed_orders("run-1") == []


class TestBlockedOrderNote:
    def test_surfaces_the_real_reason(self, monkeypatch) -> None:
        call_id = "call-1"
        blocked = {"status": "blocked", "reason": "current positions could not be read (fail-closed)"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [
                {
                    "type": "tool_call", "tool": "trading_place_order", "call_id": call_id,
                    "args": {"symbol": "EURUSDm", "side": "sell", "quantity": 0.01, "order_type": "market"},
                },
                {"type": "tool_result", "tool": "trading_place_order", "call_id": call_id, "result": json.dumps(blocked)},
            ],
        )
        note = cr._blocked_order_note("run-1")
        assert note is not None
        assert "sell 0.01 lots EURUSDm" in note
        assert "current positions could not be read (fail-closed)" in note

    def test_regression_surfaces_hallucinated_size(self, monkeypatch) -> None:
        """A 1.0-lot order (100x intended) denied by the mandate gate must show
        the REQUESTED size, not the intended one -- that's the whole point."""
        call_id = "call-1"
        denied = {"status": "blocked", "reason": "order breaches max_order_notional_usd"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [
                {
                    "type": "tool_call", "tool": "trading_place_order", "call_id": call_id,
                    "args": {"symbol": "XAUUSDm", "side": "buy", "quantity": 1.0, "order_type": "market"},
                },
                {"type": "tool_result", "tool": "trading_place_order", "call_id": call_id, "result": json.dumps(denied)},
            ],
        )
        note = cr._blocked_order_note("run-1")
        assert "1.0 lots XAUUSDm" in note

    def test_none_when_order_actually_succeeded(self, monkeypatch) -> None:
        call_id = "call-1"
        ok = {"status": "ok", "ticket": "1"}
        monkeypatch.setattr(
            cr, "_trace_entries",
            lambda run_id: [
                {"type": "tool_call", "tool": "trading_place_order", "call_id": call_id, "args": {}},
                {"type": "tool_result", "tool": "trading_place_order", "call_id": call_id, "result": json.dumps(ok)},
            ],
        )
        assert cr._blocked_order_note("run-1") is None

    def test_none_when_tool_never_called(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_trace_entries", lambda run_id: [])
        assert cr._blocked_order_note("run-1") is None


# ---------------------------------------------------------------------------
# _read_lock_identity / _acquire_singleton_lock
# ---------------------------------------------------------------------------


class TestReadLockIdentity:
    def test_missing_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "LOCK_PATH", tmp_path / "nope.lock")
        assert cr._read_lock_identity() is None

    def test_pid_and_creation_time(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "reporter.lock"
        path.write_text("12345:987654321", encoding="utf-8")
        monkeypatch.setattr(cr, "LOCK_PATH", path)
        assert cr._read_lock_identity() == (12345, 987654321)

    def test_legacy_bare_pid(self, tmp_path, monkeypatch) -> None:
        """Pre-upgrade locks had no creation-time suffix."""
        path = tmp_path / "reporter.lock"
        path.write_text("12345", encoding="utf-8")
        monkeypatch.setattr(cr, "LOCK_PATH", path)
        assert cr._read_lock_identity() == (12345, None)

    def test_garbage_content(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "reporter.lock"
        path.write_text("not-a-pid:also-not-a-number", encoding="utf-8")
        monkeypatch.setattr(cr, "LOCK_PATH", path)
        assert cr._read_lock_identity() is None


class TestAcquireSingletonLock:
    def test_acquires_when_no_lock_exists(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "LOCK_PATH", tmp_path / "reporter.lock")
        monkeypatch.setattr(cr.os, "getpid", lambda: 42)
        monkeypatch.setattr(cr, "_process_creation_time", lambda pid: 111)
        assert cr._acquire_singleton_lock() is True
        assert cr.LOCK_PATH.read_text(encoding="utf-8") == "42:111"

    def test_refuses_when_another_live_instance_holds_it(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "reporter.lock"
        path.write_text("99:222", encoding="utf-8")
        monkeypatch.setattr(cr, "LOCK_PATH", path)
        monkeypatch.setattr(cr, "_lock_identity_is_alive", lambda identity: True)
        assert cr._acquire_singleton_lock() is False
        assert path.read_text(encoding="utf-8") == "99:222"  # untouched

    def test_self_heals_a_stale_lock(self, tmp_path, monkeypatch) -> None:
        """A dead process's lock (crash/reboot never ran a cleanup handler)
        must be reclaimable, not require manual deletion."""
        path = tmp_path / "reporter.lock"
        path.write_text("99:222", encoding="utf-8")
        monkeypatch.setattr(cr, "LOCK_PATH", path)
        monkeypatch.setattr(cr, "_lock_identity_is_alive", lambda identity: False)
        monkeypatch.setattr(cr.os, "getpid", lambda: 42)
        monkeypatch.setattr(cr, "_process_creation_time", lambda pid: 333)

        assert cr._acquire_singleton_lock() is True
        assert path.read_text(encoding="utf-8") == "42:333"


class TestLockIdentityIsAlive:
    def test_matches_current_creation_time(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_process_creation_time", lambda pid: 555)
        assert cr._lock_identity_is_alive((1, 555)) is True

    def test_mismatched_creation_time_means_pid_reused(self, monkeypatch) -> None:
        """Same PID but a different creation time = a different process now
        owns that PID (Windows recycles PIDs quickly) -- must not be 'alive'."""
        monkeypatch.setattr(cr, "_process_creation_time", lambda pid: 999)
        assert cr._lock_identity_is_alive((1, 555)) is False

    def test_process_gone_entirely(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_process_creation_time", lambda pid: None)
        assert cr._lock_identity_is_alive((1, 555)) is False

    def test_legacy_lock_falls_back_to_liveness_only(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_pid_is_alive", lambda pid: True)
        assert cr._lock_identity_is_alive((1, None)) is True


# ---------------------------------------------------------------------------
# _symbol_position_summary / _post_trade_cap_check
# ---------------------------------------------------------------------------


class TestSymbolPositionSummary:
    def _patch_positions(self, monkeypatch, positions) -> None:
        import src.trading.service as service
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})

    def test_no_positions(self, monkeypatch) -> None:
        self._patch_positions(monkeypatch, [])
        assert cr._symbol_position_summary("EURUSDm", "mt5-live-trade") == {"count": 0, "side": None}

    def test_same_side_multiple(self, monkeypatch) -> None:
        self._patch_positions(
            monkeypatch,
            [
                {"symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "side": "buy"},
                {"symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "side": "buy"},
            ],
        )
        assert cr._symbol_position_summary("EURUSDm", "mt5-live-trade") == {"count": 2, "side": "buy"}

    def test_mixed_sides(self, monkeypatch) -> None:
        self._patch_positions(
            monkeypatch,
            [
                {"symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "side": "buy"},
                {"symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "side": "sell"},
            ],
        )
        assert cr._symbol_position_summary("EURUSDm", "mt5-live-trade")["side"] == "mixed"

    def test_ignores_other_symbols_and_other_magic(self, monkeypatch) -> None:
        self._patch_positions(
            monkeypatch,
            [
                {"symbol": "AUDUSDm", "magic": cr.OUR_MAGIC, "side": "buy"},
                {"symbol": "EURUSDm", "magic": 999, "side": "buy"},  # someone else's EA
            ],
        )
        assert cr._symbol_position_summary("EURUSDm", "mt5-live-trade") == {"count": 0, "side": None}


class TestPostTradeCapCheck:
    def test_within_cap_is_silent(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda symbol, conn: {"count": 1, "side": "buy"})
        note = cr._post_trade_cap_check({"symbol": "EURUSDm", "connection": "mt5-live-trade"})
        assert note == ""

    def test_mixed_direction_warns(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda symbol, conn: {"count": 2, "side": "mixed"})
        note = cr._post_trade_cap_check({"symbol": "EURUSDm", "connection": "mt5-live-trade"})
        assert "BOTH directions" in note

    def test_over_cap_warns(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda symbol, conn: {"count": 5, "side": "buy"})
        note = cr._post_trade_cap_check({"symbol": "EURUSDm", "connection": "mt5-live-trade", "max_stack": 4})
        assert "exceeding the cap of 4" in note

    def test_default_max_stack_used_when_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cr, "_symbol_position_summary",
            lambda symbol, conn: {"count": cr.MAX_SAME_DIRECTION_POSITIONS + 1, "side": "buy"},
        )
        note = cr._post_trade_cap_check({"symbol": "EURUSDm", "connection": "mt5-live-trade"})
        assert f"exceeding the cap of {cr.MAX_SAME_DIRECTION_POSITIONS}" in note


# ---------------------------------------------------------------------------
# _spread_stop_floor / _post_trade_spread_check
# ---------------------------------------------------------------------------


class TestSpreadStopFloor:
    def test_none_quote_returns_none(self) -> None:
        assert cr._spread_stop_floor(None) is None

    def test_computes_ratio_times_spread(self) -> None:
        # spread = 0.00008 (0.8 pip EURUSD-style); floor = spread * MIN_STOP_TO_SPREAD_RATIO
        floor = cr._spread_stop_floor({"bid": 1.16248, "ask": 1.16256})
        assert floor == pytest.approx(0.00008 * cr.MIN_STOP_TO_SPREAD_RATIO, abs=1e-9)

    def test_zero_or_negative_spread_returns_none(self) -> None:
        assert cr._spread_stop_floor({"bid": 1.1626, "ask": 1.1626}) is None
        assert cr._spread_stop_floor({"bid": 1.1627, "ask": 1.1626}) is None


class TestPostTradeSpreadCheck:
    def _trade(self, **overrides) -> dict:
        base = {"symbol": "EURUSDm", "connection": "mt5-live-trade"}
        base.update(overrides)
        return base

    def test_silent_when_ratio_meets_floor(self, monkeypatch) -> None:
        # stop distance 0.00080, spread 0.00008 -> ratio 10x, floor is 8x.
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.16248, "ask": 1.16256})
        placed_order = {"fill_price": 1.16256, "stop_loss": 1.16176}
        note = cr._post_trade_spread_check(self._trade(), placed_order)
        assert note == ""

    def test_warns_when_spread_dominates_the_stop(self, monkeypatch) -> None:
        # stop distance 0.00020, spread 0.00008 -> ratio 2.5x, below the 8x floor.
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.16248, "ask": 1.16256})
        placed_order = {"fill_price": 1.16256, "stop_loss": 1.16236}
        note = cr._post_trade_spread_check(self._trade(), placed_order)
        assert "[AUTOMATED CHECK]" in note
        assert "2.5x the live spread" in note

    def test_silent_when_fill_price_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.0, "ask": 1.0001})
        note = cr._post_trade_spread_check(self._trade(), {"stop_loss": 1.1620})
        assert note == ""

    def test_silent_when_quote_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda symbol, conn: None)
        placed_order = {"fill_price": 1.16256, "stop_loss": 1.16236}
        note = cr._post_trade_spread_check(self._trade(), placed_order)
        assert note == ""

    def test_silent_when_stop_distance_is_zero(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.16248, "ask": 1.16256})
        placed_order = {"fill_price": 1.16256, "stop_loss": 1.16256}
        note = cr._post_trade_spread_check(self._trade(), placed_order)
        assert note == ""


# ---------------------------------------------------------------------------
# _last_json_line
# ---------------------------------------------------------------------------


class TestLastJsonLine:
    def test_single_line(self) -> None:
        assert cr._last_json_line('{"status": "success"}') == {"status": "success"}

    def test_picks_the_last_of_several_lines(self) -> None:
        stdout = 'some log noise\n{"a": 1}\n{"status": "success", "run_id": "x"}\n'
        assert cr._last_json_line(stdout) == {"status": "success", "run_id": "x"}

    def test_none_input(self) -> None:
        assert cr._last_json_line(None) is None

    def test_empty_string(self) -> None:
        assert cr._last_json_line("") is None

    def test_last_line_not_json(self) -> None:
        assert cr._last_json_line("plain text output, no json") is None


# ---------------------------------------------------------------------------
# _classify_excursion
# ---------------------------------------------------------------------------


class TestClassifyExcursion:
    def _entry(self, **overrides) -> dict:
        base = {
            "symbol": "EURUSDm",
            "connection": "mt5-live-trade",
            "opened_at": "2026-09-07T10:00:00+00:00",
            "entry_price": 1.1620,
            "stop_loss": 1.1600,
            "side": "buy",
            "outcome": "loss",
        }
        base.update(overrides)
        return base

    def _deal(self, **overrides) -> dict:
        base = {"time": "2026-09-07T12:00:00+00:00"}
        base.update(overrides)
        return base

    def test_tags_reversal_when_favorable_move_was_large(self, monkeypatch) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk
        monkeypatch.setattr(cr, "_mt5_config_for", lambda connection: None)
        # Stop distance 0.0020; a favorable excursion of 0.0018 is >= 50% of it.
        monkeypatch.setattr(
            mt5_sdk, "get_historical_bars_range",
            lambda symbol, start, end, config=None, period="15m": {"bars": [{"high": 1.1638, "low": 1.1605, "close": 1.1610}]},
        )
        result = cr._classify_excursion(self._entry(), self._deal())
        assert result["excursion_tag"] == "reversal"
        # max_favorable_pts is round(favorable, 3) -- 0.0018 rounds to 0.002.
        assert result["max_favorable_pts"] == 0.002

    def test_tags_clean_when_no_meaningful_favorable_move(self, monkeypatch) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk
        monkeypatch.setattr(cr, "_mt5_config_for", lambda connection: None)
        monkeypatch.setattr(
            mt5_sdk, "get_historical_bars_range",
            lambda symbol, start, end, config=None, period="15m": {"bars": [{"high": 1.1622, "low": 1.1600, "close": 1.1610}]},
        )
        result = cr._classify_excursion(self._entry(), self._deal())
        assert result["excursion_tag"] == "clean"

    def test_win_never_tagged_reversal(self, monkeypatch) -> None:
        """Reversal only makes sense for a loss/breakeven that came close to
        working before turning -- a win with a big favorable excursion is
        just... winning."""
        import src.trading.connectors.mt5.sdk as mt5_sdk
        monkeypatch.setattr(cr, "_mt5_config_for", lambda connection: None)
        monkeypatch.setattr(
            mt5_sdk, "get_historical_bars_range",
            lambda symbol, start, end, config=None, period="15m": {"bars": [{"high": 1.1660, "low": 1.1605, "close": 1.1650}]},
        )
        result = cr._classify_excursion(self._entry(outcome="win"), self._deal())
        assert result["excursion_tag"] == "clean"

    def test_empty_dict_on_read_failure(self, monkeypatch) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk

        monkeypatch.setattr(cr, "_mt5_config_for", lambda connection: None)

        def _boom(symbol, start, end, config=None, period="15m"):
            raise RuntimeError("no bars")

        monkeypatch.setattr(mt5_sdk, "get_historical_bars_range", _boom)
        assert cr._classify_excursion(self._entry(), self._deal()) == {}

    def test_empty_dict_on_missing_fields(self) -> None:
        assert cr._classify_excursion({"symbol": "EURUSDm"}, self._deal()) == {}


# ---------------------------------------------------------------------------
# _status_log_summary
# ---------------------------------------------------------------------------


class TestStatusLogSummary:
    def test_missing_log_returns_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cr, "REPORTER_LOG_PATH", tmp_path / "nope.log")
        assert cr._status_log_summary() == {}

    def test_parses_last_start_and_last_emailed(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "reporter.log"
        log.write_text(
            "2026-09-07 19:17:45,724 INFO starting loop mode, session-gated scheduling\n"
            "2026-09-07 19:17:46,606 INFO running investment_committee on EURUSD (forex) [trade-enabled]\n"
            "2026-09-07 19:31:12,000 INFO emailed report: [Vibe-Trading] new_york: investment_committee — EURUSD (OK)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cr, "REPORTER_LOG_PATH", log)

        result = cr._status_log_summary()

        assert result["last_start_ts"] == "2026-09-07 19:17:46"
        assert result["last_result_ts"] == "2026-09-07 19:31:12"
        assert result["last_result_tag"] == "OK"
        # next_due is no longer derived from the log (see _status_log_summary's
        # docstring) -- _next_pass_due_text computes it straight from the
        # session schedule, tested separately below.
        assert "next_due" not in result
        assert "interval" not in result

    def test_no_next_due_without_interval(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "reporter.log"
        log.write_text(
            "2026-09-07 19:31:12,000 INFO emailed report: [Vibe-Trading] investment_committee — EURUSD (TRADED)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cr, "REPORTER_LOG_PATH", log)

        result = cr._status_log_summary()

        assert "next_due" not in result
        assert result["last_result_tag"] == "TRADED"


# ---------------------------------------------------------------------------
# _profit_protection_check -- rule 3 (time-decay stop / hard time-stop)
# ---------------------------------------------------------------------------


class TestProfitProtectionCheckTimeDecay:
    """Regression coverage for the 2026-09-08 time-decay/time-stop rule.

    A PM's "hard flat by HH:MM UTC" was prose only -- nothing enforced it,
    and a losing position kept its full original stop distance live
    indefinitely while a winner already got its risk ratcheted down by
    rules 1-2. These tests cover the new rule 3 in isolation via a single
    fake TARGETS entry, mocking every MT5/service read so no real broker
    call is ever made.
    """

    SYMBOL = "EURUSDm"
    CONNECTION = "mt5-live-trade"

    def _trade(self, **overrides) -> dict:
        base = {"symbol": self.SYMBOL, "connection": self.CONNECTION, "lots": 0.01, "max_stack": 1}
        base.update(overrides)
        return base

    def _position(self, *, hours_open: float | None, side="buy", entry=1.1600, sl=1.1580, tp=1.1650,
                   price=1.1605, ticket="1", profit=0.0, **overrides) -> dict:
        opened = (
            (datetime.now(timezone.utc) - timedelta(hours=hours_open)).isoformat()
            if hours_open is not None else None
        )
        pos = {
            "ticket": ticket, "symbol": self.SYMBOL, "magic": cr.OUR_MAGIC, "side": side,
            "price_open": entry, "stop_loss": sl, "take_profit": tp, "price_current": price,
            "time": opened, "profit": profit,
        }
        pos.update(overrides)
        return pos

    def _patch_broker(self, monkeypatch, *, positions, atr_floor=0.0010,
                       modify_result=None, close_result=None) -> dict:
        """Wires every MT5/service call _profit_protection_check makes to a
        fake, and returns dicts recording each modify_position/close_position
        call for assertions."""
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles_module
        import src.trading.service as service

        # These tests exercise the stop-moving rules themselves, which are
        # off in production (STOP_TRAILING_ENABLED) but kept behind the switch.
        monkeypatch.setattr(cr, "STOP_TRAILING_ENABLED", True)
        monkeypatch.setattr(cr, "TARGETS", [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade()}])
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")
        monkeypatch.setattr(mt5_sdk, "point_size", lambda symbol, config=None: 0.00001)
        monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol, config=None: 100_000)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda symbol, connection: atr_floor)

        calls = {"modify": [], "close": []}

        def _modify(config, *, ticket, stop_loss, take_profit):
            calls["modify"].append({"ticket": ticket, "stop_loss": stop_loss, "take_profit": take_profit})
            return modify_result or {"status": "ok"}

        def _close(config, *, ticket):
            calls["close"].append({"ticket": ticket})
            return close_result or {"status": "ok", "fill_price": 1.0, "closed_volume": 0.01}

        monkeypatch.setattr(mt5_sdk, "modify_position", _modify)
        monkeypatch.setattr(mt5_sdk, "close_position", _close)
        return calls

    def test_flattens_at_max_hold_hours(self, monkeypatch) -> None:
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS + 1, ticket="T1")
        calls = self._patch_broker(monkeypatch, positions=[pos])

        cr._profit_protection_check()

        assert calls["close"] == [{"ticket": "T1"}]
        assert calls["modify"] == []

    def test_flattens_a_naked_position_with_no_sl_tp(self, monkeypatch) -> None:
        """The unconditional flatten must not depend on sl/tp/entry being
        present -- a stray naked position is exactly the case that most
        needs the backstop."""
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS + 1, ticket="T2", sl=None, tp=None, entry=None)
        calls = self._patch_broker(monkeypatch, positions=[pos])

        cr._profit_protection_check()

        assert calls["close"] == [{"ticket": "T2"}]

    def test_tightens_stop_once_decay_window_starts(self, monkeypatch) -> None:
        start_hours = cr.MAX_HOLD_HOURS * cr.TIME_DECAY_START_FRACTION + 0.5
        # buy: price 1.1605, atr_floor 0.0010 -> candidate 1.1595, tighter
        # than the existing sl (1.1580).
        pos = self._position(hours_open=start_hours, ticket="T3", side="buy", price=1.1605, sl=1.1580)
        calls = self._patch_broker(monkeypatch, positions=[pos], atr_floor=0.0010)

        cr._profit_protection_check()

        assert calls["close"] == []
        assert calls["modify"] == [{"ticket": "T3", "stop_loss": pytest.approx(1.1595), "take_profit": pos["take_profit"]}]

    def test_no_op_before_decay_window_starts(self, monkeypatch) -> None:
        just_under = cr.MAX_HOLD_HOURS * cr.TIME_DECAY_START_FRACTION - 0.5
        # Same geometry as the tightening test above, but too early -- and
        # rules 1/2 don't trigger either (price hasn't reached halfway,
        # profit hasn't reached EARLY_PROFIT_TRIGGER_USD).
        pos = self._position(hours_open=just_under, ticket="T4", side="buy", price=1.1605, sl=1.1580, tp=1.1700)
        calls = self._patch_broker(monkeypatch, positions=[pos], atr_floor=0.0010)

        cr._profit_protection_check()

        assert calls["close"] == []
        assert calls["modify"] == []

    def test_never_widens_a_stop_already_tighter_than_the_decay_candidate(self, monkeypatch) -> None:
        start_hours = cr.MAX_HOLD_HOURS * cr.TIME_DECAY_START_FRACTION + 0.5
        # candidate would be 1.1595 (price 1.1605 - atr 0.0010), but the
        # existing stop (1.1600) is already tighter -- must not loosen it.
        pos = self._position(hours_open=start_hours, ticket="T5", side="buy", price=1.1605, sl=1.1600)
        calls = self._patch_broker(monkeypatch, positions=[pos], atr_floor=0.0010)

        cr._profit_protection_check()

        assert calls["modify"] == []

    def test_missing_open_time_degrades_gracefully(self, monkeypatch) -> None:
        """No pos['time'] -> elapsed_hours is None -> rule 3 contributes
        nothing, but rules 1/2 must still run normally (no crash)."""
        pos = self._position(hours_open=None, ticket="T6", side="buy", price=1.1605, sl=1.1580, tp=1.1700)
        calls = self._patch_broker(monkeypatch, positions=[pos], atr_floor=0.0010)

        cr._profit_protection_check()

        assert calls["close"] == []
        assert calls["modify"] == []

    def test_malformed_open_time_degrades_gracefully(self, monkeypatch) -> None:
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS + 1, ticket="T7")
        pos["time"] = "not-a-timestamp"
        calls = self._patch_broker(monkeypatch, positions=[pos])

        cr._profit_protection_check()  # must not raise

        assert calls["close"] == []

    def test_per_target_max_hold_hours_override(self, monkeypatch) -> None:
        custom_max = 4.0
        import src.trading.service as service
        import src.trading.profiles as profiles_module
        import src.trading.connectors.mt5.sdk as mt5_sdk

        pos = self._position(hours_open=custom_max + 1, ticket="T8")
        monkeypatch.setattr(
            cr, "TARGETS",
            [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade(max_hold_hours=custom_max)}],
        )
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": [pos]})

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")
        calls = {"close": []}
        monkeypatch.setattr(mt5_sdk, "close_position", lambda config, *, ticket: calls["close"].append(ticket) or {"status": "ok"})

        cr._profit_protection_check()

        assert calls["close"] == ["T8"]  # would NOT have fired yet under the global MAX_HOLD_HOURS

    def test_per_target_early_profit_trigger_usd_override(self, monkeypatch) -> None:
        """Regression for the 2026-09-09 fix: the module default
        (EARLY_PROFIT_TRIGGER_USD=$8) needs an ~80-pip move to arm at
        100k-contract/0.01-lot FX sizing -- unreachable given EURUSDm/
        AUDUSDm's actual realized wins ($0.01-$0.47). A lower per-target
        override must arm the trail on a smaller, realistic favorable move
        instead."""
        import src.trading.service as service
        import src.trading.profiles as profiles_module
        import src.trading.connectors.mt5.sdk as mt5_sdk

        monkeypatch.setattr(cr, "STOP_TRAILING_ENABLED", True)  # exercises rule 2 itself
        custom_trigger = 1.00  # trigger_distance = 1.00 / (100_000 * 0.01) = 0.001
        # Price has moved 0.0015 above entry -- past the custom trigger_
        # distance (0.001) but nowhere near the module default's 0.008, and
        # short of halfway to TP (0.015) so rule 1 stays silent -- isolates
        # rule 2.
        pos = self._position(hours_open=1.0, ticket="T12", side="buy", entry=1.1600, sl=1.1580, tp=1.1900, price=1.1615)
        monkeypatch.setattr(
            cr, "TARGETS",
            [{"committee": "x", "target": "x", "market": "forex",
              "trade": self._trade(early_profit_trigger_usd=custom_trigger)}],
        )
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": [pos]})

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")
        monkeypatch.setattr(mt5_sdk, "point_size", lambda symbol, config=None: 0.00001)
        monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol, config=None: 100_000)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda symbol, connection: 0.0010)
        calls = {"modify": []}
        monkeypatch.setattr(
            mt5_sdk, "modify_position",
            lambda config, *, ticket, stop_loss, take_profit: calls["modify"].append(
                {"ticket": ticket, "stop_loss": stop_loss}
            ) or {"status": "ok"},
        )

        cr._profit_protection_check()

        assert len(calls["modify"]) == 1
        assert calls["modify"][0]["ticket"] == "T12"
        # trail candidate = price - atr_floor = 1.1615 - 0.0010 = 1.1605
        assert calls["modify"][0]["stop_loss"] == pytest.approx(1.1605)

    def test_default_trigger_does_not_arm_on_the_same_fx_move_without_override(self, monkeypatch) -> None:
        """Companion to the override test above: the exact same price move
        that arms the trail WITH the override does nothing under the
        un-overridden module default -- proves the gap the override closes."""
        pos = self._position(hours_open=1.0, ticket="T13", side="buy", entry=1.1600, sl=1.1580, tp=1.1900, price=1.1615)
        calls = self._patch_broker(monkeypatch, positions=[pos], atr_floor=0.0010)

        cr._profit_protection_check()

        assert calls["close"] == []
        assert calls["modify"] == []

    def test_close_position_error_status_does_not_raise(self, monkeypatch) -> None:
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS + 1, ticket="T9")
        self._patch_broker(monkeypatch, positions=[pos], close_result={"status": "error", "error": "broker rejected"})

        cr._profit_protection_check()  # must not raise

    def test_close_position_raises_is_caught(self, monkeypatch) -> None:
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS + 1, ticket="T10")
        calls = self._patch_broker(monkeypatch, positions=[pos])

        import src.trading.connectors.mt5.sdk as mt5_sdk

        def _boom(config, *, ticket):
            raise RuntimeError("MT5 connection dropped")

        monkeypatch.setattr(mt5_sdk, "close_position", _boom)

        cr._profit_protection_check()  # must not raise

    def test_rules_1_and_2_still_work_unaffected_by_rule_3(self, monkeypatch) -> None:
        """Regression: adding decay_candidate to the candidates list must not
        break the pre-existing breakeven/early-profit-trail behavior."""
        # Well before the decay window; price at halfway to TP -> rule 1
        # (breakeven) should fire.
        pos = self._position(
            hours_open=1.0, ticket="T11", side="buy", entry=1.1600, tp=1.1650, sl=1.1580, price=1.1625,
        )
        calls = self._patch_broker(monkeypatch, positions=[pos], atr_floor=0.0010)

        cr._profit_protection_check()

        assert calls["close"] == []
        assert len(calls["modify"]) == 1
        assert calls["modify"][0]["ticket"] == "T11"
        # breakeven candidate = entry - buffer (protective side, just under
        # entry for a buy) -- tighter than the original sl, still below entry.
        new_sl = calls["modify"][0]["stop_loss"]
        assert pos["stop_loss"] < new_sl < pos["price_open"]


class TestStopTrailingDisabled:
    """Production setting (STOP_TRAILING_ENABLED=False): plain stop+target."""

    _fixture = TestProfitProtectionCheckTimeDecay()

    def _position(self, **kwargs):
        return self._fixture._position(**kwargs)

    def _patch_broker(self, monkeypatch, **kwargs):
        calls = self._fixture._patch_broker(monkeypatch, **kwargs)
        monkeypatch.setattr(cr, "STOP_TRAILING_ENABLED", False)
        return calls

    def test_production_default_is_off(self) -> None:
        assert cr.STOP_TRAILING_ENABLED is False

    def test_never_moves_stop_even_deep_in_profit_and_late(self, monkeypatch) -> None:
        # Past halfway to target, past the early-profit trigger, and inside
        # the time-decay window -- all three old rules would have fired.
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS * 0.9, ticket="T9")
        pos["price_current"] = pos["take_profit"] - (pos["take_profit"] - pos["price_open"]) * 0.1
        calls = self._patch_broker(monkeypatch, positions=[pos])

        cr._profit_protection_check()

        assert calls["modify"] == [] and calls["close"] == []

    def test_time_stop_still_flattens(self, monkeypatch) -> None:
        pos = self._position(hours_open=cr.MAX_HOLD_HOURS + 1, ticket="T10")
        calls = self._patch_broker(monkeypatch, positions=[pos])

        cr._profit_protection_check()

        assert calls["close"] == [{"ticket": "T10"}]


class TestProfitProtectionCheckSilentLookupFailures:
    """2026-09-21: found while investigating two gold reversal trades that
    moved well past every protection trigger and still closed at a near-
    full loss. point_size/contract_size used to fail completely silently
    (bare except, no log) inside the two rules that need them -- these
    confirm a lookup failure now logs a warning AND the position still
    gets whatever protection the OTHER rule can still provide (fails open,
    not fails silent-and-total). Own copy of TestProfitProtectionCheckTimeDecay's
    small helpers (not inherited) so this class's test output doesn't also
    silently re-run that class's unrelated tests under a new name."""

    SYMBOL = "EURUSDm"
    CONNECTION = "mt5-live-trade"

    def _trade(self, **overrides) -> dict:
        base = {"symbol": self.SYMBOL, "connection": self.CONNECTION, "lots": 0.01, "max_stack": 1}
        base.update(overrides)
        return base

    def _position(self, *, hours_open: float, side="buy", entry=1.1600, sl=1.1580, tp=1.1650,
                   price=1.1605, ticket="1", profit=0.0) -> dict:
        opened = (datetime.now(timezone.utc) - timedelta(hours=hours_open)).isoformat()
        return {
            "ticket": ticket, "symbol": self.SYMBOL, "magic": cr.OUR_MAGIC, "side": side,
            "price_open": entry, "stop_loss": sl, "take_profit": tp, "price_current": price,
            "time": opened, "profit": profit,
        }

    def _patch_broker(self, monkeypatch, *, positions, atr_floor=0.0010, modify_result=None, trade_overrides=None) -> dict:
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles_module
        import src.trading.service as service

        trade = self._trade(**(trade_overrides or {}))
        # These tests exercise the stop-moving rules themselves, which are
        # off in production (STOP_TRAILING_ENABLED) but kept behind the switch.
        monkeypatch.setattr(cr, "STOP_TRAILING_ENABLED", True)
        monkeypatch.setattr(cr, "TARGETS", [{"committee": "x", "target": "x", "market": "forex", "trade": trade}])
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")
        monkeypatch.setattr(mt5_sdk, "point_size", lambda symbol, config=None: 0.00001)
        monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol, config=None: 100_000)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda symbol, connection: atr_floor)

        calls = {"modify": [], "close": []}

        def _modify(config, *, ticket, stop_loss, take_profit):
            calls["modify"].append({"ticket": ticket, "stop_loss": stop_loss, "take_profit": take_profit})
            return modify_result or {"status": "ok"}

        def _close(config, *, ticket):
            calls["close"].append({"ticket": ticket})
            return {"status": "ok", "fill_price": 1.0, "closed_volume": 0.01}

        monkeypatch.setattr(mt5_sdk, "modify_position", _modify)
        monkeypatch.setattr(mt5_sdk, "close_position", _close)
        return calls

    def test_point_size_failure_is_logged_and_trail_rule_still_applies(self, monkeypatch, caplog) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk

        def _raise_point_size(symbol, config=None):
            raise RuntimeError("symbol not found")

        # entry 1.1600, tp 1.1620 -> halfway 1.1610; price 1.1615 is past
        # halfway (arms the point_size call/warning) AND, with a $1
        # early_profit_trigger_usd override (trigger_distance = 1 /
        # (100_000 * 0.01) = 0.001), past the trail's own $ trigger too --
        # so trail_candidate can independently protect this position even
        # with point_size broken.
        pos = self._position(hours_open=1.0, ticket="T4", side="buy", entry=1.1600, sl=1.1580, tp=1.1620, price=1.1615)
        calls = self._patch_broker(
            monkeypatch, positions=[pos], atr_floor=0.0010, trade_overrides={"early_profit_trigger_usd": 1.00},
        )
        monkeypatch.setattr(mt5_sdk, "point_size", _raise_point_size)

        with caplog.at_level("WARNING"):
            cr._profit_protection_check()

        assert any("point_size lookup failed" in r.message for r in caplog.records)
        assert calls["modify"], "trail rule should still have protected the position"

    def test_contract_size_failure_is_logged_and_breakeven_rule_still_applies(self, monkeypatch, caplog) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk

        def _raise_contract_size(symbol, config=None):
            raise RuntimeError("symbol not found")

        # Past the breakeven halfway point (halfway to 1.1650 tp is
        # 1.1625) so breakeven_candidate can independently protect this
        # position even with contract_size broken.
        pos = self._position(hours_open=1.0, ticket="T5", side="buy", entry=1.1600, sl=1.1580, tp=1.1650, price=1.1630)
        calls = self._patch_broker(monkeypatch, positions=[pos])
        monkeypatch.setattr(mt5_sdk, "contract_size", _raise_contract_size)

        with caplog.at_level("WARNING"):
            cr._profit_protection_check()

        assert any("contract_size lookup failed" in r.message for r in caplog.records)
        assert calls["modify"], "breakeven rule should still have protected the position"


# ---------------------------------------------------------------------------
# _live_circuit_breaker_check
# ---------------------------------------------------------------------------


class TestLiveCircuitBreakerCheck:
    """Regression coverage for the 2026-09-09 fix: a tripped drawdown breaker
    used to flatten only the ONE symbol whose pass detected it, leaving every
    other live target's exposure open even though the kill switch blocked new
    orders everywhere. It must now flatten every one of OUR positions across
    all live TARGETS on the tripped connection."""

    CONNECTION = "mt5-live-trade"

    def _trade(self, symbol: str, **overrides) -> dict:
        base = {"symbol": symbol, "connection": self.CONNECTION, "lots": 0.01, "max_stack": 1}
        base.update(overrides)
        return base

    def _position(self, symbol: str, ticket: str, *, magic=None, **overrides) -> dict:
        pos = {"ticket": ticket, "symbol": symbol, "magic": magic if magic is not None else cr.OUR_MAGIC}
        pos.update(overrides)
        return pos

    def _patch_broker(
        self, monkeypatch, tmp_path, *,
        targets, positions, equity, baseline=None, halted=False, close_result=None,
    ) -> dict:
        """Wires every MT5/service/halt call _live_circuit_breaker_check makes
        to a fake, and returns dicts recording each trip_halt/close_position
        call for assertions."""
        import src.live.halt as halt_module
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles_module
        import src.trading.service as service

        monkeypatch.setattr(cr, "TARGETS", targets)
        monkeypatch.setattr(cr, "LIVE_BASELINE_PATH", tmp_path / "live_baseline.json")
        if baseline is not None:
            (tmp_path / "live_baseline.json").write_text(json.dumps(baseline), encoding="utf-8")

        monkeypatch.setattr(halt_module, "halt_flag_set", lambda broker=None: halted)
        calls = {"trip": [], "close": []}

        def _trip(by, reason, broker=None):
            calls["trip"].append({"by": by, "reason": reason, "broker": broker})
            return tmp_path / "HALT"

        monkeypatch.setattr(halt_module, "trip_halt", _trip)

        monkeypatch.setattr(service, "get_account", lambda conn: {"account": {"equity": equity}})
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")

        def _close(config, *, ticket):
            calls["close"].append(ticket)
            return close_result or {"status": "ok"}

        monkeypatch.setattr(mt5_sdk, "close_position", _close)
        return calls

    def test_no_trip_when_drawdown_below_threshold(self, monkeypatch, tmp_path) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        targets = [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade("EURUSDm")}]
        calls = self._patch_broker(
            monkeypatch, tmp_path, targets=targets, positions=[],
            equity=98.5, baseline={"date": today, "equity": 100.0},  # 1.5%: under both the 2% daily stop and 50% halt
        )

        result = cr._live_circuit_breaker_check(targets[0]["trade"])

        assert result is None
        assert calls["trip"] == []
        assert calls["close"] == []

    def test_daily_loss_stop_blocks_without_tripping_kill_switch(self, monkeypatch, tmp_path) -> None:
        # Rulebook 2026-09-30: 2%+ below today's baseline -> no new trades today,
        # but the persistent kill switch (50%) is NOT tripped and nothing is flattened.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        targets = [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade("EURUSDm")}]
        calls = self._patch_broker(
            monkeypatch, tmp_path, targets=targets, positions=[],
            equity=90.0, baseline={"date": today, "equity": 100.0},
        )

        result = cr._live_circuit_breaker_check(targets[0]["trade"])

        assert result is not None and "[DAILY LOSS STOP]" in result
        assert calls["trip"] == []
        assert calls["close"] == []

    def test_daily_loss_stop_persists_after_equity_recovers_same_day(self, monkeypatch, tmp_path) -> None:
        """Real bug: the daily loss stop's own comment promises "research-only
        for the rest of the day," but the check used to just re-compare the
        INSTANTANEOUS drawdown every call with nothing persisted -- so a
        later same-day check (a second scheduled pass, a manual re-run) that
        catches equity having recovered above the 2% threshold would
        silently let live trading resume the same day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        targets = [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade("EURUSDm")}]
        calls = self._patch_broker(
            monkeypatch, tmp_path, targets=targets, positions=[],
            equity=90.0, baseline={"date": today, "equity": 100.0},  # 10%: trips the daily stop
        )
        first = cr._live_circuit_breaker_check(targets[0]["trade"])
        assert first is not None and "[DAILY LOSS STOP]" in first

        # Equity recovers above the 2% threshold on a later same-day check --
        # must STILL be research-only, not silently resume live trading.
        import src.trading.service as service
        monkeypatch.setattr(service, "get_account", lambda conn: {"account": {"equity": 99.0}})
        second = cr._live_circuit_breaker_check(targets[0]["trade"])

        assert second is not None and "[DAILY LOSS STOP]" in second
        assert calls["trip"] == []  # still just the soft stop, not the persistent kill switch
        saved = json.loads((tmp_path / "live_baseline.json").read_text(encoding="utf-8"))
        assert saved["daily_loss_stop_tripped"] is True

    def test_already_halted_returns_message_without_re_tripping(self, monkeypatch, tmp_path) -> None:
        targets = [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade("EURUSDm")}]
        calls = self._patch_broker(monkeypatch, tmp_path, targets=targets, positions=[], equity=100.0, halted=True)

        result = cr._live_circuit_breaker_check(targets[0]["trade"])

        assert result is not None and "HALTED" in result
        assert calls["trip"] == []
        assert calls["close"] == []

    def test_non_live_connection_is_a_no_op(self, monkeypatch, tmp_path) -> None:
        trade = self._trade("EURUSDm", connection="mt5-demo-trade")
        targets = [{"committee": "x", "target": "x", "market": "forex", "trade": trade}]
        calls = self._patch_broker(monkeypatch, tmp_path, targets=targets, positions=[], equity=100.0)

        assert cr._live_circuit_breaker_check(trade) is None
        assert calls["trip"] == []

    def test_trip_flattens_every_live_symbol_on_the_connection_not_just_the_triggering_one(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The actual bug: previously only EURUSDm (the triggering pass's own
        symbol) was closed -- AUDUSDm and XAGUSDm stayed open."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        eurusd_trade = self._trade("EURUSDm")
        targets = [
            {"committee": "x", "target": "EURUSD", "market": "forex", "trade": eurusd_trade},
            {"committee": "x", "target": "AUDUSD", "market": "forex", "trade": self._trade("AUDUSDm")},
            {"committee": "x", "target": "XAGUSD", "market": "commodity/forex", "trade": self._trade("XAGUSDm")},
        ]
        positions = [
            self._position("EURUSDm", "T1"),
            self._position("AUDUSDm", "T2"),
            self._position("XAGUSDm", "T3"),
        ]
        calls = self._patch_broker(
            monkeypatch, tmp_path, targets=targets, positions=positions,
            equity=50.0, baseline={"date": today, "equity": 200.0},  # 75% drawdown, over 50%
        )

        result = cr._live_circuit_breaker_check(eurusd_trade)

        assert result is not None and "TRIPPED" in result
        assert len(calls["trip"]) == 1
        assert sorted(calls["close"]) == ["T1", "T2", "T3"]

    def test_trip_does_not_close_a_position_carrying_a_foreign_magic(self, monkeypatch, tmp_path) -> None:
        """A separately-running signal-service EA's own position on the same
        symbol must not be swept up by our flatten."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        eurusd_trade = self._trade("EURUSDm")
        targets = [{"committee": "x", "target": "EURUSD", "market": "forex", "trade": eurusd_trade}]
        positions = [
            self._position("EURUSDm", "OURS"),
            self._position("EURUSDm", "NOT_OURS", magic=999),
        ]
        calls = self._patch_broker(
            monkeypatch, tmp_path, targets=targets, positions=positions,
            equity=50.0, baseline={"date": today, "equity": 200.0},
        )

        cr._live_circuit_breaker_check(eurusd_trade)

        assert calls["close"] == ["OURS"]

    def test_flatten_failure_does_not_raise_and_is_reported(self, monkeypatch, tmp_path) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        eurusd_trade = self._trade("EURUSDm")
        targets = [{"committee": "x", "target": "EURUSD", "market": "forex", "trade": eurusd_trade}]
        positions = [self._position("EURUSDm", "T1")]
        self._patch_broker(
            monkeypatch, tmp_path, targets=targets, positions=positions,
            equity=50.0, baseline={"date": today, "equity": 200.0},
            close_result={"status": "error", "error": "broker rejected"},
        )

        result = cr._live_circuit_breaker_check(eurusd_trade)  # must not raise

        assert result is not None and "TRIPPED" in result

    def test_baseline_established_on_first_call_of_day_does_not_trip(self, monkeypatch, tmp_path) -> None:
        """No baseline file yet -> today's baseline is set to current equity
        itself, so drawdown is 0 and nothing trips on the very first check."""
        targets = [{"committee": "x", "target": "x", "market": "forex", "trade": self._trade("EURUSDm")}]
        calls = self._patch_broker(monkeypatch, tmp_path, targets=targets, positions=[], equity=100.0, baseline=None)

        result = cr._live_circuit_breaker_check(targets[0]["trade"])

        assert result is None
        assert calls["trip"] == []
        saved = json.loads((tmp_path / "live_baseline.json").read_text(encoding="utf-8"))
        assert saved["equity"] == 100.0


class TestPostTradeSpecCheck:
    """Ported 2026-09-18 from the identical guardrail built for
    fundednext_reporter.py first, after a real incident there (wrong
    symbol, 25x mandated lot size, limit instead of market order). This
    account has its own documented history of the same failure mode (see
    run_committee's comment on the hallucinated 1.0-lot order)."""

    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}
    NEW_TICKET = "999"

    def _patch(self, monkeypatch, *, positions=None):
        import src.live.halt as halt
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.service as service

        tripped: dict = {}
        closed_tickets: list = []
        monkeypatch.setattr(halt, "trip_halt", lambda by, reason, broker: tripped.update(by=by, reason=reason, broker=broker))
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions if positions is not None else []})
        monkeypatch.setattr(cr, "_mt5_config_for", lambda connection: {})

        def _close(config, ticket):
            closed_tickets.append(ticket)
            return {"status": "ok"}

        monkeypatch.setattr(mt5_sdk, "close_position", _close)
        return tripped, closed_tickets

    def test_matching_order_is_a_no_op(self, monkeypatch) -> None:
        tripped, closed_tickets = self._patch(monkeypatch)
        order = {"symbol": "EURUSDm", "quantity": 0.01, "order_type": "market", "order_id": self.NEW_TICKET}
        assert cr._post_trade_spec_check(self.TRADE, order) == ""
        assert tripped == {} and closed_tickets == []

    def test_wrong_symbol_closes_and_halts(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDcm", "magic": cr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSDcm", "quantity": 0.01, "order_type": "market", "order_id": self.NEW_TICKET}
        note = cr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "AUTO-CLOSED" in note
        assert tripped.get("broker") == "mt5"
        assert closed_tickets == [self.NEW_TICKET]

    def test_wrong_quantity_closes_and_halts(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSDm", "quantity": 1.0, "order_type": "market", "order_id": self.NEW_TICKET}
        note = cr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "1.0" in note
        assert tripped.get("broker") == "mt5"
        assert closed_tickets == [self.NEW_TICKET]

    def test_limit_order_closes_and_halts(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSDm", "quantity": 0.01, "order_type": "limit", "order_id": self.NEW_TICKET}
        note = cr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "not a market order" in note
        assert tripped.get("broker") == "mt5"
        assert closed_tickets == [self.NEW_TICKET]

    def test_does_not_close_other_legitimate_stacked_positions(self, monkeypatch) -> None:
        """Real bug found by /code-review: matching by symbol+magic alone
        force-closed ALL positions on the symbol, including healthy
        pre-existing stacked ones (a real, expected state under
        max_stack > 1) -- must only touch the new violating fill."""
        positions = [
            {"ticket": "111", "symbol": "EURUSDm", "magic": cr.OUR_MAGIC},  # pre-existing, healthy
            {"ticket": "222", "symbol": "EURUSDm", "magic": cr.OUR_MAGIC},  # pre-existing, healthy
            {"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC},  # the new, bad fill
        ]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSDm", "quantity": 1.0, "order_type": "market", "order_id": self.NEW_TICKET}
        note = cr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note
        assert closed_tickets == [self.NEW_TICKET]

    def test_missing_order_id_refuses_to_close_anything(self, monkeypatch) -> None:
        """Without a way to identify which position is actually the new
        one, blindly closing everything on the symbol would repeat the
        same bug -- must refuse and surface for manual review instead."""
        positions = [{"ticket": "111", "symbol": "EURUSDm", "magic": cr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSDm", "quantity": 1.0, "order_type": "market"}  # no order_id
        note = cr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "refusing to blindly close" in note
        assert closed_tickets == []


class TestResolveFillPrice:
    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01}

    def test_uses_placed_order_fill_price_when_valid(self, monkeypatch) -> None:
        import src.trading.service as service

        def _boom(conn):
            raise AssertionError("get_positions should not be called when fill_price is already valid")

        monkeypatch.setattr(service, "get_positions", _boom)
        assert cr._resolve_fill_price(self.TRADE, {"fill_price": 1.1234, "symbol": "EURUSDm"}) == 1.1234

    def test_falls_back_when_fill_price_is_zero(self, monkeypatch) -> None:
        import src.trading.service as service

        positions = [{"symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "price_open": 1.1500}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})
        assert cr._resolve_fill_price(self.TRADE, {"fill_price": 0.0, "symbol": "EURUSDm"}) == 1.1500

    def test_returns_none_when_no_source_has_a_price(self, monkeypatch) -> None:
        import src.trading.service as service

        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": []})
        assert cr._resolve_fill_price(self.TRADE, {"fill_price": 0.0, "symbol": "EURUSDm"}) is None

    def test_matches_new_ticket_not_older_stacked_position(self, monkeypatch) -> None:
        """Real bug: with more than one same-symbol position open (this
        module's own default cap is 4), matching by symbol+magic alone
        could silently return an OLDER position's entry price instead of
        the new fill's -- must match by the new order's own ticket."""
        import src.trading.service as service

        positions = [
            {"ticket": "111", "symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "price_open": 1.1000},  # older, unrelated
            {"ticket": "999", "symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "price_open": 1.1050},  # the new fill
        ]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})
        order = {"fill_price": 0.0, "symbol": "EURUSDm", "order_id": "999"}
        assert cr._resolve_fill_price(self.TRADE, order) == 1.1050

    def test_falls_back_to_broad_match_when_order_has_no_ticket(self, monkeypatch) -> None:
        import src.trading.service as service

        positions = [{"symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "price_open": 1.1500}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})
        order = {"fill_price": 0.0, "symbol": "EURUSDm"}  # no order_id
        assert cr._resolve_fill_price(self.TRADE, order) == 1.1500


class TestPostTradeRewardRiskCheck:
    """Ported 2026-09-18 -- see MIN_REWARD_RISK_RATIO's own comment for the
    real AUDUSDm data (net-negative despite a positive win rate) that
    motivated this on the FundedNext side first."""

    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01}
    NEW_TICKET = "999"

    def _patch(self, monkeypatch, *, atr_floor=0.0002, spread_floor=0.0001, positions=None, modify_result=None):
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.service as service

        calls = {}
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.1000, "ask": 1.1001})
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda symbol, connection: atr_floor)
        monkeypatch.setattr(cr, "_spread_stop_floor", lambda quote: spread_floor)
        default_positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions if positions is not None else default_positions})
        monkeypatch.setattr(cr, "_mt5_config_for", lambda connection: {})

        def _modify(config, ticket=None, stop_loss=None, take_profit=None):
            calls.update(ticket=ticket, stop_loss=stop_loss, take_profit=take_profit)
            return modify_result or {"status": "ok"}

        monkeypatch.setattr(mt5_sdk, "modify_position", _modify)
        return calls

    def test_ratio_already_healthy_is_a_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1020, "order_id": self.NEW_TICKET}
        assert cr._post_trade_reward_risk_check(self.TRADE, order) == ""
        assert calls == {}

    def test_tightens_stop_when_floor_allows(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["ticket"] == self.NEW_TICKET
        assert calls["take_profit"] == 1.1010
        assert calls["stop_loss"] == pytest.approx(1.0995)
        assert order["stop_loss"] == pytest.approx(1.0995)
        assert order["take_profit"] == 1.1010

    def test_widens_target_when_tightening_would_violate_floor(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, atr_floor=0.0009, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "widened take-profit" in note
        assert calls["stop_loss"] == 1.0990
        assert calls["take_profit"] == pytest.approx(1.10175)  # (0.0010 + s) * 1.5 + s, s = 0.0001

    def test_zero_fill_price_falls_back_to_live_position(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "price_open": 1.1000}]
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001, positions=positions)
        order = {"side": "buy", "fill_price": 0.0, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note

    def test_no_matching_position_reports_without_crashing(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, positions=[])
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "no matching open position" in note
        assert calls == {}

    def test_does_not_correct_other_legitimate_stacked_positions(self, monkeypatch) -> None:
        """Real bug found by /code-review: matching ours[0] (whatever the
        broker happened to return first) could silently modify an older,
        already-compliant stacked position while leaving the actual
        sub-floor new fill uncorrected -- must only touch the new ticket."""
        positions = [
            {"ticket": "111", "symbol": "EURUSDm", "magic": cr.OUR_MAGIC},  # pre-existing, healthy
            {"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC},  # the new, sub-floor one
        ]
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001, positions=positions)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note
        assert calls["ticket"] == self.NEW_TICKET

    def test_missing_order_id_reports_without_crashing(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC}]
        calls = self._patch(monkeypatch, positions=positions)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010}  # no order_id
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "no matching open position" in note
        assert calls == {}

    def test_missing_fields_is_a_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        assert cr._post_trade_reward_risk_check(self.TRADE, {"side": "buy", "fill_price": 1.1}) == ""
        assert calls == {}

    def test_widens_target_instead_of_tightening_when_floor_unreadable(self, monkeypatch) -> None:
        """Real bug: _atr_stop_floor/_spread_stop_floor both fail open to
        None (e.g. a transient bars/quote read error right after the fill --
        the rest of this file treats that as routine), which used to make
        floor_distance 0.0 and let the stop get tightened with NO floor
        check at all -- the opposite of what the docstring promises ("never
        past the ATR/spread volatility floor"), and inconsistent with
        _post_trade_stop_floor_check's own `if floor_distance <= 0` guard
        for the identical case. Must fall back to widening the target
        instead, which never needs the floor."""
        calls = self._patch(monkeypatch, atr_floor=None, spread_floor=None)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "widened take-profit" in note
        assert calls["stop_loss"] == 1.0990  # stop left untouched -- no floor to safely tighten against
        assert calls["take_profit"] == pytest.approx(1.10175)  # (0.0010 + s) * 1.5 + s, s = 0.0001
        assert order["stop_loss"] == 1.0990


class TestHandleFilledOrder:
    """Real bug found by /code-review: the journal write used to live only in
    run_committee's "no violation" branch, so a fill a guardrail immediately
    closed (spec/trend/stop-floor-budget violation) silently never reached
    the trade journal -- see _handle_filled_order's own docstring."""

    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def test_journals_even_when_spec_violation_closes_it(self, monkeypatch) -> None:
        journaled = []
        monkeypatch.setattr(cr, "_enforce_trend_rule", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spec_check", lambda *a, **k: "\n\n[CRITICAL -- SPEC VIOLATION, AUTO-CLOSED] ...")
        monkeypatch.setattr(cr, "_journal_record_open", lambda symbol, connection, order: journaled.append((symbol, connection, order)))
        order = {"symbol": "EURUSDcm", "side": "buy", "quantity": 0.25, "order_id": "1",
                 "stop_loss": 1.0, "take_profit": 1.1}

        note, traded = cr._handle_filled_order(self.TRADE, order, None)

        assert traded is False
        assert "SPEC VIOLATION" in note
        # Journaled under the order's OWN (wrong) symbol, not trade["symbol"]
        # -- misfiling it into "EURUSDm"'s history would be its own bug.
        assert journaled == [("EURUSDcm", "mt5-live-trade", order)]

    def test_journals_even_when_trend_gate_closes_it(self, monkeypatch) -> None:
        journaled = []
        monkeypatch.setattr(cr, "_enforce_trend_rule", lambda *a, **k: "\n\n[TREND GATE -- ORDER CLOSED] ...")
        monkeypatch.setattr(cr, "_journal_record_open", lambda symbol, connection, order: journaled.append((symbol, connection, order)))
        order = {"symbol": "EURUSDm", "side": "sell", "quantity": 0.01, "order_id": "2",
                 "stop_loss": 1.1, "take_profit": 1.0}

        note, traded = cr._handle_filled_order(self.TRADE, order, {"buy"})

        assert traded is False
        assert "TREND GATE" in note
        assert journaled == [("EURUSDm", "mt5-live-trade", order)]

    def test_journals_normally_when_no_violation(self, monkeypatch) -> None:
        journaled = []
        monkeypatch.setattr(cr, "_enforce_trend_rule", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spec_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_stop_floor_check", lambda *a, **k: ("", False))
        monkeypatch.setattr(cr, "_post_trade_max_stop_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_cap_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spread_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_reward_risk_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_journal_record_open", lambda symbol, connection, order: journaled.append((symbol, connection, order)))
        order = {"symbol": "EURUSDm", "side": "buy", "quantity": 0.01, "order_id": "3",
                 "stop_loss": 1.0, "take_profit": 1.1}

        note, traded = cr._handle_filled_order(self.TRADE, order, {"buy"})

        assert traded is True
        assert journaled == [("EURUSDm", "mt5-live-trade", order)]
        assert order["trend_alignment"] == "with"
        # D13 (2026-10-08) -- see fundednext_reporter.py's identical test
        # for the full rationale.
        assert order["regime"] == "NORMAL"

    def test_journals_the_trade_s_regime_label_when_present(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_enforce_trend_rule", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spec_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_stop_floor_check", lambda *a, **k: ("", False))
        monkeypatch.setattr(cr, "_post_trade_max_stop_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_cap_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spread_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_reward_risk_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_journal_record_open", lambda symbol, connection, order: None)
        order = {"symbol": "EURUSDm", "side": "buy", "quantity": 0.01, "order_id": "4",
                 "stop_loss": 1.0, "take_profit": 1.1}
        trade = {**self.TRADE, "regime_label": "VOLATILE"}

        cr._handle_filled_order(trade, order, {"buy"})

        assert order["regime"] == "VOLATILE"

    def test_research_only_fill_is_closed_and_journaled_as_violation(self, monkeypatch) -> None:
        """D3 (2026-10-06): a research-only pass's prompt never offers
        trading_place_order, but the tool is still technically reachable. If
        it fires anyway, this is the code-level backstop -- close it, trip
        the kill switch, journal it as a violation (not a trade), and this
        takes priority over the normal spec/trend checks, not alongside
        them (they're never even called)."""
        journaled = []
        spec_called = []
        monkeypatch.setattr(cr, "_post_trade_research_only_violation",
                             lambda *a, **k: "\n\n[CRITICAL — RESEARCH-ONLY VIOLATION, AUTO-CLOSED] ...")
        monkeypatch.setattr(cr, "_enforce_trend_rule", lambda *a, **k: spec_called.append(1) or "")
        monkeypatch.setattr(cr, "_post_trade_spec_check", lambda *a, **k: spec_called.append(1) or "")
        monkeypatch.setattr(cr, "_journal_record_open", lambda symbol, connection, order: journaled.append((symbol, connection, order)))
        order = {"symbol": "EURUSDm", "side": "buy", "quantity": 0.01, "order_id": "4",
                 "stop_loss": 1.0, "take_profit": 1.1}

        note, traded = cr._handle_filled_order(
            self.TRADE, order, None, research_only=True, research_only_reason="[NEWS BLACKOUT] ...",
        )

        assert traded is False
        assert "RESEARCH-ONLY VIOLATION" in note
        assert journaled == [("EURUSDm", "mt5-live-trade", order)]
        assert spec_called == []  # normal spec/trend checks never run for a research-only violation


class TestJournalAnyPlacedOrders:
    """Real incident 2026-10-05 (FundedNext GBPUSD empty_model_response),
    found by the daily-repair agent's 2026-10-06 review: a run that places
    an order and THEN fails on a later step used to skip every guardrail and
    the journal entirely, because run_committee's non-success/empty-answer
    branches returned before ever looking at the trace. This incident itself
    happened not to have placed an order, but the gap was real and live --
    _journal_any_placed_orders is now shared by every return point."""

    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def test_empty_when_run_id_is_none(self, monkeypatch) -> None:
        # The "could not parse CLI output" branch has no run_id at all --
        # genuinely nothing to recover there.
        called = []
        monkeypatch.setattr(cr, "_extract_placed_orders", lambda run_id: called.append(run_id) or [])
        notes, traded, any_placed = cr._journal_any_placed_orders(self.TRADE, None, None)
        assert (notes, traded, any_placed) == ("", False, False)
        assert called == []  # never even looked -- there's no run_id to look with

    def test_empty_when_trade_is_none(self, monkeypatch) -> None:
        # A research-only pass (trade=None) has nothing to journal against.
        called = []
        monkeypatch.setattr(cr, "_extract_placed_orders", lambda run_id: called.append(run_id) or [])
        notes, traded, any_placed = cr._journal_any_placed_orders(None, "run-1", None)
        assert (notes, traded, any_placed) == ("", False, False)
        assert called == []

    def test_journals_an_order_placed_before_a_later_failure(self, monkeypatch) -> None:
        """The actual regression: a run that places an order and then fails
        (empty_model_response, max-iterations, ...) must still run that
        order through the guardrail chain and journal it -- any_placed=True
        is what lets run_committee's error branches append the
        [POST-FAILURE GUARDRAIL CHECK] note instead of silently dropping it."""
        order = {"status": "ok", "ticket": "1", "symbol": "EURUSDm"}
        monkeypatch.setattr(cr, "_extract_placed_orders", lambda run_id: [order])
        monkeypatch.setattr(cr, "_resolve_fill_price", lambda trade, o: 1.1234)
        monkeypatch.setattr(cr, "_handle_filled_order", lambda trade, o, trend, **kw: ("\n\n[JOURNALED]", True))

        notes, traded, any_placed = cr._journal_any_placed_orders(self.TRADE, "run-1", None)

        assert any_placed is True
        assert traded is True
        assert "[JOURNALED]" in notes
        assert order["fill_price"] == 1.1234  # patched in before _handle_filled_order ran

    def test_any_placed_true_even_when_every_order_gets_closed(self, monkeypatch) -> None:
        # A placed order that a guardrail immediately closes still counts as
        # "placed" for the caller's post-failure note, even though traded
        # ends up False -- the point is that it was journaled, not that it
        # survived.
        order = {"status": "ok", "ticket": "1", "symbol": "EURUSDcm"}
        monkeypatch.setattr(cr, "_extract_placed_orders", lambda run_id: [order])
        monkeypatch.setattr(cr, "_resolve_fill_price", lambda trade, o: None)
        monkeypatch.setattr(cr, "_handle_filled_order", lambda trade, o, trend, **kw: ("\n\n[SPEC VIOLATION]", False))

        notes, traded, any_placed = cr._journal_any_placed_orders(self.TRADE, "run-1", None)

        assert any_placed is True
        assert traded is False
        assert "[SPEC VIOLATION]" in notes

    def test_multiple_orders_all_run_and_aggregated(self, monkeypatch) -> None:
        first = {"status": "ok", "ticket": "1", "symbol": "EURUSDm"}
        second = {"status": "ok", "ticket": "2", "symbol": "EURUSDm"}
        monkeypatch.setattr(cr, "_extract_placed_orders", lambda run_id: [first, second])
        monkeypatch.setattr(cr, "_resolve_fill_price", lambda trade, o: None)
        calls = []

        def _fake_handle(trade, o, trend, **kw):
            calls.append(o["ticket"])
            return f"\n\n[{o['ticket']}]", o["ticket"] == "2"

        monkeypatch.setattr(cr, "_handle_filled_order", _fake_handle)
        notes, traded, any_placed = cr._journal_any_placed_orders(self.TRADE, "run-1", None)

        assert calls == ["1", "2"]  # both orders run, not just the first
        assert any_placed is True
        assert traded is True  # True if ANY order ended up traded
        assert "[1]" in notes and "[2]" in notes


class TestLlmBalanceAlert:
    def _patch(self, monkeypatch, tmp_path, *, provider: str, balance):
        monkeypatch.setenv("LANGCHAIN_PROVIDER", provider)
        monkeypatch.setattr(cr, "LLM_BALANCE_ALERT_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setitem(cr._LLM_BALANCE_READERS, "openrouter", ("OpenRouter", lambda: balance))
        monkeypatch.setitem(cr._LLM_BALANCE_READERS, "deepseek", ("DeepSeek", lambda: balance))
        sent = []
        monkeypatch.setattr(cr, "send_email", lambda subject, text, **kw: sent.append(subject))
        return sent

    def test_openrouter_low_balance_alerts_once(self, monkeypatch, tmp_path) -> None:
        sent = self._patch(monkeypatch, tmp_path, provider="openrouter", balance=0.11)

        cr._check_llm_balance_alert()
        cr._check_llm_balance_alert()

        assert sent == ["[Vibe-Trading] LOW BALANCE — OpenRouter $0.11"]

    def test_stale_alert_flag_from_other_provider_does_not_mute(self, monkeypatch, tmp_path) -> None:
        # The real on-disk state at switch-over was a DeepSeek-era {"alerted": true}
        # with no provider key -- it must not suppress OpenRouter's first alert.
        sent = self._patch(monkeypatch, tmp_path, provider="openrouter", balance=0.11)
        (tmp_path / "state.json").write_text('{"alerted": true}', encoding="utf-8")

        cr._check_llm_balance_alert()

        assert len(sent) == 1

    def test_healthy_balance_does_not_alert(self, monkeypatch, tmp_path) -> None:
        sent = self._patch(monkeypatch, tmp_path, provider="openrouter", balance=15.0)

        cr._check_llm_balance_alert()

        assert sent == []

    def test_unknown_provider_is_not_checked(self, monkeypatch, tmp_path) -> None:
        sent = self._patch(monkeypatch, tmp_path, provider="anthropic", balance=0.0)

        cr._check_llm_balance_alert()

        assert sent == []

    def test_openrouter_reader_computes_remaining_credit(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(cr, "_fetch_json_or_none", lambda url, key: {"data": {"total_credits": 20, "total_usage": 19.8866347}})

        assert abs(cr._openrouter_balance_usd() - 0.1133653) < 1e-9

    def test_openrouter_reader_fails_open(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(cr, "_fetch_json_or_none", lambda url, key: None)

        assert cr._openrouter_balance_usd() is None


class TestExclusiveGroup:
    def _patch(self, monkeypatch, open_by_symbol: dict):
        calls, sent = [], []
        for name in ("_check_trade_drought", "_check_cap_fit_alert", "_check_llm_balance_alert", "_log_cap_gap"):
            monkeypatch.setattr(cr, name, lambda: None)
        monkeypatch.setattr(
            cr, "TARGETS",
            [
                {"committee": "investment_committee", "target": "EURUSD", "market": "forex",
                 "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}},
                {"committee": "investment_committee", "target": "GBPUSD", "market": "forex",
                 "trade": {"symbol": "GBPUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}},
            ],
        )
        monkeypatch.setattr(
            cr, "_symbol_position_summary",
            lambda symbol, connection: open_by_symbol.get(symbol, {"count": 0, "side": None}),
        )
        monkeypatch.setattr(cr, "is_reportable", lambda result: False)
        monkeypatch.setattr(cr, "_status_header", lambda: "")
        monkeypatch.setattr(cr, "send_email", lambda subject, text, **kw: sent.append((subject, text)))

        def _fake_run_committee(**kwargs):
            calls.append(kwargs["target"])
            return cr.CommitteeResult(
                committee=kwargs["committee"], target=kwargs["target"], market=kwargs["market"],
                status="success", run_id="r1", report_text="", traded=False,
            )

        monkeypatch.setattr(cr, "run_committee", _fake_run_committee)
        return calls, sent

    def test_gbpusd_skipped_while_eurusd_open(self, monkeypatch) -> None:
        calls, sent = self._patch(monkeypatch, {"EURUSDm": {"count": 1, "side": "buy"}})

        cr.run_once("new_york")

        assert calls == ["EURUSD"]
        assert len(sent) == 1 and sent[0][0].endswith("GBPUSD (SKIPPED)") and "EURUSDm" in sent[0][1]

    def test_eurusd_skipped_while_gbpusd_open(self, monkeypatch) -> None:
        calls, sent = self._patch(monkeypatch, {"GBPUSDm": {"count": 1, "side": "sell"}})

        cr.run_once("new_york")

        assert calls == ["GBPUSD"]
        assert sent[0][0].endswith("EURUSD (SKIPPED)")

    def test_both_run_when_nothing_open(self, monkeypatch) -> None:
        calls, sent = self._patch(monkeypatch, {})

        cr.run_once("new_york")

        assert calls == ["EURUSD", "GBPUSD"]
        assert sent == []

    def test_fails_closed_when_positions_unreadable(self, monkeypatch) -> None:
        def _boom(symbol, connection):
            raise RuntimeError("terminal not connected")
        monkeypatch.setattr(cr, "_symbol_position_summary", _boom)

        note = cr._exclusive_group_conflict({"symbol": "GBPUSDm", "connection": "mt5-live-trade"})

        assert note is not None and "could not read" in note

    def test_symbol_outside_group_is_never_blocked(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda symbol, connection: {"count": 1, "side": "buy"})

        assert cr._exclusive_group_conflict({"symbol": "XAUUSDm", "connection": "mt5-live-trade"}) is None

    def test_live_targets_are_eurusd_and_gbpusd(self) -> None:
        assert [t["trade"]["symbol"] for t in cr.TARGETS] == ["EURUSDm", "GBPUSDm"]


class TestWeekendFlattenTiming:
    def test_friday_after_cutoff_flattens_between_passes(self, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(cr, "_weekend_flatten_and_notify", lambda: calls.append("flatten"))
        monkeypatch.setattr(cr, "_profit_protection_check", lambda: calls.append("protect"))

        # Fri 2026-09-25 20:05 UTC -- the gap the old boundary-only check missed.
        cr._between_passes_tick(datetime(2026, 9, 25, 20, 5, tzinfo=timezone.utc))

        assert calls == ["flatten"]

    def test_weekday_runs_profit_protection(self, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(cr, "_weekend_flatten_and_notify", lambda: calls.append("flatten"))
        monkeypatch.setattr(cr, "_profit_protection_check", lambda: calls.append("protect"))

        cr._between_passes_tick(datetime(2026, 9, 25, 19, 55, tzinfo=timezone.utc))

        assert calls == ["protect"]

    def test_flatten_crash_does_not_propagate(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("terminal gone")
        monkeypatch.setattr(cr, "_weekend_flatten_and_notify", _boom)

        cr._between_passes_tick(datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc))

    def _patch_flatten(self, monkeypatch, tmp_path, close_result):
        import types
        monkeypatch.setattr(cr, "WEEKEND_STATE_PATH", tmp_path / "weekend.json")
        monkeypatch.setattr(
            cr, "TARGETS",
            [{"committee": "investment_committee", "target": "EURUSD", "market": "forex",
              "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}}],
        )
        pos = {"ticket": 1, "symbol": "EURUSDm", "magic": cr.OUR_MAGIC, "side": "buy", "volume": 0.01, "profit": -1.0}
        import src.trading.service as service
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles
        monkeypatch.setattr(service, "get_positions", lambda connection: {"positions": [pos]})
        monkeypatch.setattr(profiles, "profile_by_id", lambda cid: types.SimpleNamespace(config={}))
        monkeypatch.setattr(mt5_sdk, "build_config", lambda a, b: {})
        monkeypatch.setattr(mt5_sdk, "close_position", lambda config, ticket: close_result)
        monkeypatch.setattr(cr, "_status_header", lambda: "")
        sent = []
        monkeypatch.setattr(cr, "send_email", lambda subject, text, **kw: sent.append(text))
        return sent

    def test_repeated_failed_close_emails_once_per_week(self, monkeypatch, tmp_path) -> None:
        sent = self._patch_flatten(monkeypatch, tmp_path, {"status": "error", "error": "retcode=10018 Market closed"})

        for _ in range(3):
            cr._weekend_flatten_and_notify()

        assert len(sent) == 1 and "FAILED to close" in sent[0]

    def test_successful_close_always_emails(self, monkeypatch, tmp_path) -> None:
        sent = self._patch_flatten(monkeypatch, tmp_path, {"status": "ok", "closed_volume": 0.01, "fill_price": 1.139})

        cr._weekend_flatten_and_notify()
        cr._weekend_flatten_and_notify()

        assert len(sent) == 2 and all("Closed BUY" in text for text in sent)


# ---------------------------------------------------------------------------
# D2 (2026-10-06): no fresh data pack -> hard PASS, LLM never invoked
# ---------------------------------------------------------------------------


class TestBuildPromptDataPackGate:
    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def _patch_facts(self, monkeypatch) -> None:
        """Everything _build_prompt's trade branch reads, stubbed inert --
        only market_data_pack.write_data_pack's return value matters here."""
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda s, c: {"count": 0, "side": None})
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda s, c: None)
        for name in ("_signal_service_activity", "_journal_summary_text", "_session_bias_fact"):
            monkeypatch.setattr(cr, name, lambda *a, **k: "")
        monkeypatch.setattr(cr, "_journal_reconcile_closed", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(cr, "_effective_max_loss_usd", lambda c: 4.0)
        monkeypatch.setattr(cr, "_max_stop_distance", lambda *a, **k: 0.004)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda *a, **k: 0.001)
        # v5.1 (D6): isolate from this machine's real FINNHUB_API_KEY/cache --
        # news_api_status() makes no network call but does read real state.
        monkeypatch.setattr(cr.fn_news, "news_api_status", lambda: "OK")

    def test_returns_none_when_data_pack_unavailable(self, monkeypatch) -> None:
        self._patch_facts(monkeypatch)
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: None)
        assert cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", self.TRADE) is None

    def test_returns_a_prompt_when_data_pack_available(self, monkeypatch) -> None:
        self._patch_facts(monkeypatch)
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: Path("fake_pack.md"))
        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", self.TRADE)
        assert prompt is not None and "DATA PACK FILE" in prompt

    def test_research_only_pass_is_unaffected(self, monkeypatch) -> None:
        # trade=None never reaches the data-pack gate at all (D3's job, not
        # D2's) -- write_data_pack must not even be called.
        called = []
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: called.append(1) or None)
        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", None)
        assert prompt is not None
        assert called == []


class TestRunCommitteeDataPackGate:
    """The actual deliverable: no fresh data pack -> the LLM subprocess is
    never spawned at all, and the pass is recorded as a PASS with reason
    data_pack_unavailable, not an error."""

    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def _patch_pre_checks_inert(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_live_circuit_breaker_check", lambda trade: None)
        monkeypatch.setattr(cr, "_exclusive_group_conflict", lambda trade: None)
        monkeypatch.setattr(cr.fn_news, "is_news_blackout", lambda *a, **k: (False, None))
        monkeypatch.setattr(cr, "TREND_FILTER_ENABLED", False)
        # D12 (2026-10-08): without this, run_committee's regime gate makes a
        # REAL live MT5 call before ever reaching the data-pack gate this
        # class actually tests -- flaky (depends on real-time market
        # conditions; caught live 2026-10-09 when the real market happened
        # to classify CALM and short-circuited before _build_prompt's mock
        # was ever reached). Force NORMAL so the gate this class is testing
        # is the one that actually runs.
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: cr.strategy_tracking.RegimeResult("NORMAL", 50.0, 1.0, "INVOKE_LLM", "normal_regime"))

    def test_no_llm_subprocess_spawned_when_data_pack_unavailable(self, monkeypatch) -> None:
        self._patch_pre_checks_inert(monkeypatch)
        monkeypatch.setattr(cr, "_build_prompt", lambda *a, **k: None)
        popen_calls = []
        monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("Popen must not be called")))

        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert popen_calls == []
        assert result.status == "success"
        assert result.traded is False
        assert "DECISION: PASS" in result.report_text
        assert "data_pack_unavailable" in result.report_text


class TestRunCommitteeRegimeGate:
    """D12 (2026-10-08) -- see fundednext_reporter.py's identical test class
    for the full rationale."""

    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def _patch_pre_checks_inert(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_live_circuit_breaker_check", lambda trade: None)
        monkeypatch.setattr(cr, "_exclusive_group_conflict", lambda trade: None)
        monkeypatch.setattr(cr.fn_news, "is_news_blackout", lambda *a, **k: (False, None))
        monkeypatch.setattr(cr, "TREND_FILTER_ENABLED", False)

    def _regime(self, label, action, size_mult=1.0, pct=50.0, reason="x"):
        return cr.strategy_tracking.RegimeResult(label, pct, size_mult, action, reason)

    @pytest.mark.parametrize("label,reason", [("CALM", "calm_regime"), ("EXTREME", "extreme_volatility")])
    def test_pass_no_llm_regime_spawns_no_subprocess(self, monkeypatch, label, reason) -> None:
        self._patch_pre_checks_inert(monkeypatch)
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: self._regime(label, "PASS_NO_LLM", 0.0, 92.0, reason))
        monkeypatch.setattr(cr.fn_news, "news_api_status", lambda: "OK")
        popen_calls = []
        monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("Popen must not be called")))

        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert popen_calls == []
        assert result.status == "success"
        assert result.traded is False
        assert "DECISION: PASS" in result.report_text
        assert reason in result.report_text

    @pytest.mark.parametrize("label,size_mult", [("NORMAL", 1.0), ("VOLATILE", 0.5)])
    def test_invoke_llm_regime_proceeds_to_build_prompt(self, monkeypatch, label, size_mult) -> None:
        self._patch_pre_checks_inert(monkeypatch)
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: self._regime(label, "INVOKE_LLM", size_mult, 50.0, "x"))

        class _Stop(Exception):
            pass

        reached = {}

        def _fake_build_prompt(committee, target, market, trade, *, research_only=False, research_only_reason=""):
            reached["called"] = True
            reached["trade"] = trade
            raise _Stop

        monkeypatch.setattr(cr, "_build_prompt", _fake_build_prompt)

        with pytest.raises(_Stop):
            cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert reached.get("called") is True
        assert f"regime: {label}" in reached["trade"]["regime_rule"]
        # D-repair (2026-10-09) -- see fundednext_reporter.py's identical
        # test for the full rationale. At this account's 0.01-lot minimum,
        # a VOLATILE 0.5x multiplier floors back to 0.01 (no smaller size
        # exists), but it must still be computed via regime_adjusted_lots
        # rather than silently left untouched.
        assert reached["trade"]["lots"] == cr.strategy_tracking.regime_adjusted_lots(
            self.TRADE["lots"], self._regime(label, "INVOKE_LLM", size_mult, 50.0, "x"),
        )

    @pytest.mark.parametrize("label,toggle_name", [("CALM", "REGIME_SKIP_CALM"), ("EXTREME", "REGIME_SKIP_EXTREME")])
    def test_skip_toggle_off_invokes_llm_anyway(self, monkeypatch, label, toggle_name) -> None:
        # D17 (2026-10-08) -- see fundednext_reporter.py's identical test
        # for the full rationale.
        self._patch_pre_checks_inert(monkeypatch)
        monkeypatch.setattr(cr.strategy_tracking, toggle_name, False)
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: self._regime(label, "PASS_NO_LLM", 0.0, 92.0, "x"))

        class _Stop(Exception):
            pass

        reached = {}
        monkeypatch.setattr(cr, "_build_prompt", lambda *a, **k: reached.update(called=True) or (_ for _ in ()).throw(_Stop))

        with pytest.raises(_Stop):
            cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert reached.get("called") is True

    def test_regime_gate_skipped_when_already_research_only(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_live_circuit_breaker_check", lambda trade: "[LIVE CIRCUIT BREAKER] ...")
        called = []
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: called.append(1) or self._regime("CALM", "PASS_NO_LLM"))

        class _Stop(Exception):
            pass

        monkeypatch.setattr(cr, "_build_prompt", lambda *a, **k: (_ for _ in ()).throw(_Stop))

        with pytest.raises(_Stop):
            cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert called == []

    @pytest.mark.parametrize("label,reason", [("CALM", "calm_regime"), ("EXTREME", "extreme_volatility")])
    def test_regime_short_circuit_satisfies_d4_schema(self, monkeypatch, label, reason) -> None:
        # D15 (2026-10-08) -- see fundednext_reporter.py's identical test
        # for the full rationale.
        self._patch_pre_checks_inert(monkeypatch)
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: self._regime(label, "PASS_NO_LLM", 0.0, 92.0, reason))
        monkeypatch.setattr(cr.fn_news, "news_api_status", lambda: "OK")
        monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen must not be called")))

        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert cr.strategy_tracking.validate_committee_fields(result.report_text) == []


# ---------------------------------------------------------------------------
# D3 (2026-10-06): research-only passes get the v4 schema + data pack,
# trading_place_order is never offered, and `trade` is no longer nulled out
# by a pre-check trip (research_only is its own flag now).
# ---------------------------------------------------------------------------


class TestBuildPromptResearchOnly:
    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def _patch_facts(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda s, c: {"count": 0, "side": None})
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda s, c: None)
        for name in ("_signal_service_activity", "_journal_summary_text", "_session_bias_fact"):
            monkeypatch.setattr(cr, name, lambda *a, **k: "")
        monkeypatch.setattr(cr, "_journal_reconcile_closed", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(cr, "_effective_max_loss_usd", lambda c: 4.0)
        monkeypatch.setattr(cr, "_max_stop_distance", lambda *a, **k: 0.004)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda *a, **k: 0.001)
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: Path("fake_pack.md"))
        monkeypatch.setattr(cr.fn_news, "news_api_status", lambda: "OK")

    def test_research_only_prompt_has_mode_and_no_order_instructions(self, monkeypatch) -> None:
        self._patch_facts(monkeypatch)
        prompt = cr._build_prompt(
            "fx_commodity_day_desk", "EURUSD", "forex", self.TRADE,
            research_only=True, research_only_reason="[NEWS BLACKOUT] EURUSD unreachable.",
        )
        assert prompt is not None
        assert "DATA PACK FILE" in prompt  # still gets the data pack, unlike the old no-trade path
        assert "MODE: RESEARCH_ONLY" in prompt
        assert "DECISION: PASS" in prompt
        assert "trading_place_order(\n" not in prompt  # the order-call template is never offered
        assert "[NEWS BLACKOUT] EURUSD unreachable." in prompt

    def test_live_prompt_has_mode_live_and_order_instructions(self, monkeypatch) -> None:
        self._patch_facts(monkeypatch)
        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", self.TRADE)
        assert prompt is not None
        assert "MODE: LIVE" in prompt
        assert "trading_place_order(\n" in prompt

    def test_data_pack_gate_still_applies_in_research_only_mode(self, monkeypatch) -> None:
        self._patch_facts(monkeypatch)
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: None)
        assert cr._build_prompt(
            "fx_commodity_day_desk", "EURUSD", "forex", self.TRADE, research_only=True,
        ) is None

    def test_prompt_carries_v51_schema_fields_and_rules(self, monkeypatch) -> None:
        """D6/v5.1 (2026-10-06): DATA_MISSING/NEWS_API_STATUS/INPUT_PROVENANCE
        fields, the verified NEWS_API_STATUS fact echoed from fn_news (not
        asked of the LLM), and the "PASS is a success" framing rule."""
        self._patch_facts(monkeypatch)
        monkeypatch.setattr(cr.fn_news, "news_api_status", lambda: "STALE")
        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", self.TRADE)
        assert prompt is not None
        assert "DATA_MISSING:" in prompt
        assert "NEWS_API_STATUS:" in prompt
        assert "INPUT_PROVENANCE:" in prompt
        assert "NEWS_API_STATUS verified fact: STALE." in prompt
        assert "INPUT PROVENANCE verified facts" in prompt
        assert "PASS is a successful outcome" in prompt

    def test_prompt_carries_regime_rule_when_present(self, monkeypatch) -> None:
        # D13 (2026-10-08) -- see fundednext_reporter.py's identical test
        # for the full rationale.
        self._patch_facts(monkeypatch)
        regime = cr.strategy_tracking.RegimeResult("VOLATILE", 82.0, 0.5, "INVOKE_LLM", "volatile_regime")
        trade = {**self.TRADE, "regime_rule": cr.strategy_tracking.regime_rule_prompt(regime)}
        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", trade)
        assert prompt is not None
        assert "regime: VOLATILE" in prompt
        assert "size_multiplier: 0.5" in prompt

    def test_prompt_omits_regime_rule_when_absent(self, monkeypatch) -> None:
        self._patch_facts(monkeypatch)
        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", self.TRADE)
        assert prompt is not None
        assert "regime:" not in prompt


class TestRunCommitteePreChecksPreserveTrade:
    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}

    def test_circuit_breaker_trip_keeps_trade_and_sets_research_only(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_live_circuit_breaker_check", lambda trade: "[LIVE CIRCUIT BREAKER] ...")
        seen = {}

        class _Stop(Exception):
            pass

        def _fake_build_prompt(committee, target, market, trade, *, research_only=False, research_only_reason=""):
            seen["trade"] = trade
            seen["research_only"] = research_only
            seen["research_only_reason"] = research_only_reason
            raise _Stop

        monkeypatch.setattr(cr, "_build_prompt", _fake_build_prompt)

        try:
            cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)
        except _Stop:
            pass

        assert seen["trade"] is not None and seen["trade"]["symbol"] == "EURUSDm"
        assert seen["research_only"] is True
        assert "LIVE CIRCUIT BREAKER" in seen["research_only_reason"]


# ---------------------------------------------------------------------------
# D4 (2026-10-06): malformed committee output (missing a required field) is
# rejected as status="error" rather than trusted as a clean decision record.
# ---------------------------------------------------------------------------


class _FakePopen:
    """Minimal stand-in for subprocess.Popen: communicate() returns a canned
    (stdout, stderr), matching what run_committee reads via _last_json_line."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode
        self.pid = 12345

    def communicate(self, timeout=None):
        return self._stdout, ""


class TestRunCommitteeMalformedOutputGate:
    TRADE = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}
    VALID_REPORT = (
        "DECISION: PASS\nCONFIDENCE: 50\nMODE: LIVE\nDATA_MISSING: none\nNEWS_API_STATUS: OK\n"
        "EDGE: none\nCHECKLIST: n/a\n"
        "ORDER: none\nINVALIDATION: n/a\nINPUT_PROVENANCE: mt5-live-trade, quote 12:00:00 UTC, pack 12:00:00 UTC\n"
        "PROPOSAL: none\nREASON FOR PASS: no setup qualified"
    )

    def _patch_run(self, monkeypatch, *, report_text: str, placed_orders: list | None = None) -> None:
        import json as _json
        monkeypatch.setattr(cr, "_live_circuit_breaker_check", lambda trade: None)
        monkeypatch.setattr(cr, "_exclusive_group_conflict", lambda trade: None)
        monkeypatch.setattr(cr.fn_news, "is_news_blackout", lambda *a, **k: (False, None))
        monkeypatch.setattr(cr, "TREND_FILTER_ENABLED", False)
        # D12 -- see TestRunCommitteeDataPackGate's identical mock for the
        # full rationale (without it, this hits real live MT5 and is flaky).
        monkeypatch.setattr(cr.strategy_tracking, "regime_for_symbol",
                             lambda s, c, **k: cr.strategy_tracking.RegimeResult("NORMAL", 50.0, 1.0, "INVOKE_LLM", "normal_regime"))
        monkeypatch.setattr(cr, "_build_prompt", lambda *a, **k: "fake prompt")
        payload = _json.dumps({"status": "success", "run_id": "fake-run-1"})
        monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: _FakePopen(payload))
        monkeypatch.setattr(cr, "_read_final_answer", lambda run_id: report_text)
        monkeypatch.setattr(cr, "_extract_placed_orders", lambda run_id: placed_orders or [])
        monkeypatch.setattr(cr, "_blocked_order_note", lambda run_id: None)

    def test_valid_output_is_accepted(self, monkeypatch) -> None:
        self._patch_run(monkeypatch, report_text=self.VALID_REPORT)
        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)
        assert result.status == "success"

    def test_missing_field_is_rejected(self, monkeypatch) -> None:
        broken = self.VALID_REPORT.replace("MODE: LIVE\n", "")  # drop a required field
        self._patch_run(monkeypatch, report_text=broken)
        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)
        assert result.status == "error"
        assert "MALFORMED_OUTPUT" in result.error
        assert "MODE" in result.error

    def test_rejection_with_no_order_placed_journals_nothing(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        broken = self.VALID_REPORT.replace("MODE: LIVE\n", "")
        self._patch_run(monkeypatch, report_text=broken, placed_orders=[])
        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)
        assert result.status == "error"
        assert result.traded is False
        assert cr._read_journal() == []

    def test_rejection_with_a_real_fill_still_journals_it(self, monkeypatch, tmp_path) -> None:
        """Deliberate: a malformed REPORT still gets rejected as a decision
        record, but a REAL fill is never dropped because of it -- same
        principle as the empty_model_response fix (D3/the 2026-10-05
        incident). Flagged for the user: this is a chosen interpretation of
        D4's "do NOT write to journal," not a literal block-everything
        reading, since the latter would reopen the exact gap D3 just closed."""
        monkeypatch.setattr(cr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        order = {"status": "ok", "ticket": "1", "symbol": "EURUSDm", "side": "buy", "quantity": 0.01,
                 "order_id": "1", "stop_loss": 1.1, "take_profit": 1.2, "order_type": "market"}
        monkeypatch.setattr(cr, "_enforce_trend_rule", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spec_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_stop_floor_check", lambda *a, **k: ("", False))
        monkeypatch.setattr(cr, "_post_trade_max_stop_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_cap_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_spread_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_post_trade_reward_risk_check", lambda *a, **k: "")
        monkeypatch.setattr(cr, "_resolve_fill_price", lambda trade, o: None)
        broken = self.VALID_REPORT.replace("MODE: LIVE\n", "")
        self._patch_run(monkeypatch, report_text=broken, placed_orders=[order])

        result = cr.run_committee("fx_commodity_day_desk", "EURUSD", "forex", trade=self.TRADE)

        assert result.status == "error"  # the report itself is still rejected
        assert "MALFORMED_OUTPUT" in result.error
        assert result.traded is True  # but the real fill is not lost
        assert len(cr._read_journal()) == 1


class TestLogEmailedRegimeParsing:
    """D16 (2026-10-08) -- see fundednext_reporter.py's identical test
    class for the full rationale."""

    def test_regex_captures_tag_alone(self) -> None:
        line = "2026-10-08 15:11:04,712 INFO emailed report: [Vibe-Trading] new_york: fx_commodity_day_desk — EURUSD (ERROR)"
        m = cr._LOG_EMAILED_RE.match(line)
        assert m is not None
        assert m.group(3) == "ERROR"

    def test_regex_captures_tag_with_regime_suffix(self) -> None:
        line = "2026-10-08 15:11:04,712 INFO emailed report: [Vibe-Trading] new_york: fx_commodity_day_desk — EURUSD (PASS, EXTREME)"
        m = cr._LOG_EMAILED_RE.match(line)
        assert m is not None
        assert m.group(3) == "PASS, EXTREME"

    def test_status_summary_surfaces_regime_in_last_result_tag(self, monkeypatch, tmp_path) -> None:
        log_path = tmp_path / "reporter.log"
        log_path.write_text(
            "2026-10-08 15:16:32,738 INFO emailed report: [Vibe-Trading] new_york: fx_commodity_day_desk — "
            "GBPUSD (OK, VOLATILE)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cr, "REPORTER_LOG_PATH", log_path)
        summary = cr._status_log_summary()
        assert summary["last_result_tag"] == "OK, VOLATILE"


class TestRunOnceRecordsRegime:
    def test_record_decision_receives_result_regime(self, monkeypatch) -> None:
        trade = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}
        monkeypatch.setattr(cr, "TARGETS", [{"target": "EURUSD", "committee": "fx_commodity_day_desk",
                                              "market": "forex", "trade": trade}])
        monkeypatch.setattr(cr, "_exclusive_group_conflict", lambda t: None)
        monkeypatch.setattr(cr, "_rulebook_skip_reason", lambda s: None)
        fake_result = cr.CommitteeResult(
            "fx_commodity_day_desk", "EURUSD", "forex", "success", "run1",
            "DECISION: PASS\n...", traded=False, regime="EXTREME",
        )
        monkeypatch.setattr(cr, "run_committee", lambda **k: fake_result)
        monkeypatch.setattr(cr, "is_reportable", lambda r: False)
        seen = {}
        monkeypatch.setattr(cr.strategy_tracking, "record_decision",
                             lambda *a, **k: seen.update(k))

        cr.run_once("new_york")

        assert seen.get("regime") == "EXTREME"
