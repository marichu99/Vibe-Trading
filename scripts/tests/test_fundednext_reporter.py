"""Tests for scripts/fundednext_reporter.py.

Every test that touches one of the module's *_PATH constants monkeypatches
it to a tmp_path location first. No network/MT5/subprocess/SMTP call is ever
made for real.

Scope note: this focuses on the pure/isolated helpers (journal read/write,
weekend window, symbol-currency parsing, and the one behavioral addition
over committee_reporter.py -- _journal_record_open also marking a trading
day). A full run_committee()/main() end-to-end test would additionally need
to mock subprocess.Popen and the trace-store read path; that level of
integration coverage doesn't exist for committee_reporter.py either (its own
test suite is unit-level on the bug-prone pure functions), so this mirrors
that same scope rather than expanding it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import fundednext_reporter as fr
import fundednext_state as fn_state

pytestmark = pytest.mark.unit


class TestInWeekendWindow:
    def test_saturday(self) -> None:
        assert fr._in_weekend_window(datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc))

    def test_sunday(self) -> None:
        assert fr._in_weekend_window(datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc))

    def test_friday_before_cutoff(self) -> None:
        assert not fr._in_weekend_window(datetime(2026, 9, 4, 19, 59, tzinfo=timezone.utc))

    def test_friday_at_cutoff(self) -> None:
        assert fr._in_weekend_window(datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc))

    def test_weekday(self) -> None:
        assert not fr._in_weekend_window(datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))


class TestJournalReadWrite:
    def test_round_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        entries = [{"ticket": "1", "symbol": "EURUSDm", "status": "open"}]
        fr._write_journal(entries)
        assert fr._read_journal() == entries

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", tmp_path / "does_not_exist.json")
        assert fr._read_journal() == []

    def test_corrupt_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "journal.json"
        path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", path)
        assert fr._read_journal() == []


class TestSymbolCurrencies:
    def test_eurusdm(self) -> None:
        assert fr._symbol_currencies("EURUSDm") == {"EUR", "USD"}

    def test_audusdm(self) -> None:
        assert fr._symbol_currencies("AUDUSDm") == {"AUD", "USD"}

    def test_bare_pair_no_suffix(self) -> None:
        assert fr._symbol_currencies("GBPJPY") == {"GBP", "JPY"}

    def test_too_short_returns_empty(self) -> None:
        assert fr._symbol_currencies("XAU") == set()


class TestJournalRecordOpenMarksTradingDay:
    """The one behavioral addition over committee_reporter.py's version."""

    def test_records_open_and_trading_day(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")

        order = {
            "order_id": "999", "side": "buy", "quantity": 0.01,
            "fill_price": 1.1650, "stop_loss": 1.1600, "take_profit": 1.1700,
        }
        fr._journal_record_open("EURUSDm", "mt5fn-live-trade", order)

        entries = fr._read_journal()
        assert len(entries) == 1
        assert entries[0]["ticket"] == "999"
        assert fn_state.trading_days_count() == 1

    def test_two_opens_same_day_only_count_one_trading_day(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")

        order = {"order_id": "1", "side": "buy", "quantity": 0.01, "fill_price": 1.0, "stop_loss": 0.9, "take_profit": 1.1}
        fr._journal_record_open("EURUSDm", "mt5fn-live-trade", order)
        order2 = {"order_id": "2", "side": "sell", "quantity": 0.01, "fill_price": 0.75, "stop_loss": 0.76, "take_profit": 0.70}
        fr._journal_record_open("AUDUSDm", "mt5fn-live-trade", order2)

        assert len(fr._read_journal()) == 2
        assert fn_state.trading_days_count() == 1


class TestJournalSummaryText:
    def test_none_when_no_closed_trades(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        fr._write_journal([{"symbol": "EURUSDm", "status": "open"}])
        assert fr._journal_summary_text("EURUSDm") is None

    def test_summarizes_wins_and_losses(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "TRADE_JOURNAL_PATH", tmp_path / "journal.json")
        fr._write_journal([
            {"symbol": "EURUSDm", "status": "closed", "outcome": "win", "profit": 1.5, "side": "buy"},
            {"symbol": "EURUSDm", "status": "closed", "outcome": "loss", "profit": -0.5, "side": "sell"},
        ])
        summary = fr._journal_summary_text("EURUSDm")
        assert summary is not None and "1W/1L" in summary
