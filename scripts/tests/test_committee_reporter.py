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
# _compute_loop_tick -- regression: weekend flatten used to be nested inside
# `if boundary_reached`, so it only ran 3x/day at session-boundary clock
# times, which have nothing to do with WEEKEND_CUTOFF_UTC_HOUR (Fri 20:00
# UTC). That left a multi-hour Friday-evening gap (last NY boundary ~13:00
# UTC to the next boundary, Sat 00:00 UTC) where a live position would sit
# unflattened right through the actual weekend market close.
# ---------------------------------------------------------------------------


class TestComputeLoopTick:
    def test_friday_evening_weekend_start_with_no_boundary_reached_still_flattens(self) -> None:
        # Friday 20:05 UTC: just past WEEKEND_CUTOFF_UTC_HOUR, but nowhere
        # near the next session boundary (Sat 00:00 Asia open) -- this is
        # exactly the gap the old nested check missed.
        now = datetime(2026, 9, 4, 20, 5, tzinfo=timezone.utc)
        next_boundary = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
        tick = cr._compute_loop_tick(now, next_boundary, "asia")
        assert tick.action == "weekend"
        assert tick.boundary_reached is False
        # Not reached yet -- the schedule tracker must be left untouched.
        assert (tick.next_boundary, tick.next_session) == (next_boundary, "asia")

    def test_weekend_and_boundary_reached_still_flattens_not_runs(self) -> None:
        now = datetime(2026, 9, 5, 0, 0, 0, tzinfo=timezone.utc)  # Sat 00:00, Asia boundary
        tick = cr._compute_loop_tick(now, now, "asia")
        assert tick.action == "weekend"
        assert tick.boundary_reached is True
        assert tick.next_session != "asia"  # rolled forward, not repeated

    def test_weekday_boundary_reached_runs(self) -> None:
        now = datetime(2026, 9, 8, 12, 0, 1, tzinfo=timezone.utc)  # Tue, just past NY open
        tick = cr._compute_loop_tick(now, datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc), "new_york")
        assert tick.action == "run"
        assert tick.session == "new_york"
        assert tick.boundary_reached is True

    def test_weekday_boundary_not_reached_polls(self) -> None:
        now = datetime(2026, 9, 8, 5, 0, tzinfo=timezone.utc)  # Tue, between london/new_york
        next_boundary = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        tick = cr._compute_loop_tick(now, next_boundary, "new_york")
        assert tick.action == "poll"
        assert tick.session is None
        assert (tick.next_boundary, tick.next_session) == (next_boundary, "new_york")


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
        monkeypatch.setattr(cr, "_check_trade_drought", lambda: None)
        monkeypatch.setattr(cr, "_check_cap_fit_alert", lambda: None)
        monkeypatch.setattr(cr, "_check_llm_balance_alert", lambda: None)
        monkeypatch.setattr(cr, "_log_cap_gap", lambda: None)
        monkeypatch.setattr(cr, "is_reportable", lambda result: False)
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
            equity=90.0, baseline={"date": today, "equity": 100.0},  # 10% drawdown, under 50%
        )

        result = cr._live_circuit_breaker_check(targets[0]["trade"])

        assert result is None
        assert calls["trip"] == []
        assert calls["close"] == []

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


class TestLiveCircuitBreakerPoll:
    """Regression: session-gated trading now runs the committee (and thus
    _live_circuit_breaker_check, previously the only caller) only ~once/day
    on the new_york pass, so a same-day drawdown after that check would go
    undetected until the next day's check resets the baseline and loses it.
    _live_circuit_breaker_poll runs the same breaker on the fast
    BREAKEVEN_POLL_SECONDS cadence independent of the committee schedule."""

    def test_checks_each_unique_live_connection_once_and_skips_demo(self, monkeypatch) -> None:
        targets = [
            {"committee": "x", "target": "EURUSD", "market": "forex",
             "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade"}},
            {"committee": "x", "target": "AUDUSD", "market": "forex",
             "trade": {"symbol": "AUDUSDm", "connection": "mt5-live-trade"}},
            {"committee": "x", "target": "DEMO", "market": "forex",
             "trade": {"symbol": "EURUSDm", "connection": "mt5-demo-trade"}},
        ]
        monkeypatch.setattr(cr, "TARGETS", targets)
        checked = []
        monkeypatch.setattr(cr, "_live_circuit_breaker_check", lambda trade: checked.append(trade["connection"]) or None)
        monkeypatch.setattr(cr, "send_email", lambda *a, **k: pytest.fail("should not email when nothing tripped"))

        cr._live_circuit_breaker_poll()

        assert checked == ["mt5-live-trade"]  # one call, demo connection never checked

    def test_emails_on_a_fresh_trip(self, monkeypatch) -> None:
        targets = [{"committee": "x", "target": "EURUSD", "market": "forex",
                    "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade"}}]
        monkeypatch.setattr(cr, "TARGETS", targets)
        monkeypatch.setattr(
            cr, "_live_circuit_breaker_check",
            lambda trade: "[LIVE CIRCUIT BREAKER TRIPPED] daily equity drawdown 75%",
        )
        emails = []
        monkeypatch.setattr(cr, "send_email", lambda subject, body, **kwargs: emails.append((subject, body)))

        cr._live_circuit_breaker_poll()

        assert len(emails) == 1
        assert "TRIPPED" in emails[0][0]

    def test_does_not_email_for_an_already_halted_no_op(self, monkeypatch) -> None:
        targets = [{"committee": "x", "target": "EURUSD", "market": "forex",
                    "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade"}}]
        monkeypatch.setattr(cr, "TARGETS", targets)
        monkeypatch.setattr(
            cr, "_live_circuit_breaker_check",
            lambda trade: "[LIVE CIRCUIT BREAKER] mt5 live trading is currently HALTED (kill switch already tripped)",
        )
        monkeypatch.setattr(cr, "send_email", lambda *a, **k: pytest.fail("should not email for an already-halted no-op"))

        cr._live_circuit_breaker_poll()  # must not raise, must not email

    def test_a_crashing_check_does_not_propagate(self, monkeypatch) -> None:
        targets = [{"committee": "x", "target": "EURUSD", "market": "forex",
                    "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade"}}]
        monkeypatch.setattr(cr, "TARGETS", targets)

        def _boom(trade):
            raise RuntimeError("broker read failed")

        monkeypatch.setattr(cr, "_live_circuit_breaker_check", _boom)

        cr._live_circuit_breaker_poll()  # must not raise


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
        assert calls["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)
        assert order["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)
        assert order["take_profit"] == 1.1010

    def test_widens_target_when_tightening_would_violate_floor(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, atr_floor=0.0009, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = cr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "widened take-profit" in note
        assert calls["stop_loss"] == 1.0990
        assert calls["take_profit"] == pytest.approx(1.1015)

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
