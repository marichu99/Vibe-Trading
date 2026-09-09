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
        # Stop distance 0.0020; a favorable excursion of 0.0018 is >= 50% of it.
        monkeypatch.setattr(
            mt5_sdk, "get_historical_bars_range",
            lambda symbol, start, end, period: {"bars": [{"high": 1.1638, "low": 1.1605, "close": 1.1610}]},
        )
        result = cr._classify_excursion(self._entry(), self._deal())
        assert result["excursion_tag"] == "reversal"
        # max_favorable_pts is round(favorable, 3) -- 0.0018 rounds to 0.002.
        assert result["max_favorable_pts"] == 0.002

    def test_tags_clean_when_no_meaningful_favorable_move(self, monkeypatch) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk
        monkeypatch.setattr(
            mt5_sdk, "get_historical_bars_range",
            lambda symbol, start, end, period: {"bars": [{"high": 1.1622, "low": 1.1600, "close": 1.1610}]},
        )
        result = cr._classify_excursion(self._entry(), self._deal())
        assert result["excursion_tag"] == "clean"

    def test_win_never_tagged_reversal(self, monkeypatch) -> None:
        """Reversal only makes sense for a loss/breakeven that came close to
        working before turning -- a win with a big favorable excursion is
        just... winning."""
        import src.trading.connectors.mt5.sdk as mt5_sdk
        monkeypatch.setattr(
            mt5_sdk, "get_historical_bars_range",
            lambda symbol, start, end, period: {"bars": [{"high": 1.1660, "low": 1.1605, "close": 1.1650}]},
        )
        result = cr._classify_excursion(self._entry(outcome="win"), self._deal())
        assert result["excursion_tag"] == "clean"

    def test_empty_dict_on_read_failure(self, monkeypatch) -> None:
        import src.trading.connectors.mt5.sdk as mt5_sdk

        def _boom(symbol, start, end, period):
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

    def test_parses_interval_last_start_and_last_emailed(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "reporter.log"
        log.write_text(
            "2026-09-07 19:17:45,724 INFO starting loop mode, interval=7200s (profit protection checked every 300s)\n"
            "2026-09-07 19:17:46,606 INFO running investment_committee on EURUSD (forex) [trade-enabled]\n"
            "2026-09-07 19:31:12,000 INFO emailed report: [Vibe-Trading] investment_committee — EURUSD (OK)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cr, "REPORTER_LOG_PATH", log)

        result = cr._status_log_summary()

        assert result["interval"] == 7200
        assert result["last_start_ts"] == "2026-09-07 19:17:46"
        assert result["last_result_ts"] == "2026-09-07 19:31:12"
        assert result["last_result_tag"] == "OK"
        assert result["next_due"] == "2026-09-07 21:31:12"

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
        monkeypatch.setattr(mt5_sdk, "point_size", lambda symbol: 0.00001)
        monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol: 100_000)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda symbol: atr_floor)

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
        monkeypatch.setattr(mt5_sdk, "point_size", lambda symbol: 0.00001)
        monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol: 100_000)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda symbol: 0.0010)
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
