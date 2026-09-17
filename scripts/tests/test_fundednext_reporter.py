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

import fundednext_guardrails as fn_guard
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


class TestPostTradeSpecCheck:
    """Real incident 2026-09-17: the committee filled an order with the
    wrong symbol, 25x the mandated lot size, as a limit order -- directly
    contradicting the prompt's hardcoded trading_place_order() template.
    This is the backstop: any filled order that doesn't match TARGETS'
    exact spec gets closed immediately and halts further trading, rather
    than just being noted in the email afterward.
    """

    TRADE = {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.01, "max_stack": 1}

    def _patch(self, monkeypatch, *, closed=None):
        import src.live.halt as halt

        tripped = {}
        monkeypatch.setattr(halt, "trip_halt", lambda by, reason, broker: tripped.update(by=by, reason=reason, broker=broker))
        monkeypatch.setattr(fn_guard, "_flatten_positions", lambda conn, magic, symbols: closed or [])
        return tripped

    def test_matching_order_is_a_no_op(self, monkeypatch) -> None:
        tripped = self._patch(monkeypatch)
        order = {"symbol": "EURUSD", "quantity": 0.01, "order_type": "market"}
        assert fr._post_trade_spec_check(self.TRADE, order) == ""
        assert tripped == {}

    def test_wrong_symbol_closes_and_halts(self, monkeypatch) -> None:
        tripped = self._patch(monkeypatch, closed=["EURUSDm ticket 123: ok"])
        order = {"symbol": "EURUSDm", "quantity": 0.01, "order_type": "market"}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "AUTO-CLOSED" in note and "EURUSDm" in note
        assert tripped.get("broker") == fn_guard.BROKER

    def test_wrong_quantity_closes_and_halts(self, monkeypatch) -> None:
        tripped = self._patch(monkeypatch, closed=["EURUSD ticket 124: ok"])
        order = {"symbol": "EURUSD", "quantity": 0.25, "order_type": "market"}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "0.25" in note
        assert tripped.get("broker") == fn_guard.BROKER

    def test_limit_order_closes_and_halts(self, monkeypatch) -> None:
        tripped = self._patch(monkeypatch, closed=["EURUSD ticket 125: ok"])
        order = {"symbol": "EURUSD", "quantity": 0.01, "order_type": "limit"}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "not a market order" in note
        assert tripped.get("broker") == fn_guard.BROKER

    def test_multiple_mismatches_all_reported_in_one_pass(self, monkeypatch) -> None:
        tripped = self._patch(monkeypatch, closed=[])
        order = {"symbol": "EURUSDm", "quantity": 0.25, "order_type": "limit"}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "EURUSDm" in note and "0.25" in note and "not a market order" in note
        assert "no position found to close" in note
        assert tripped.get("broker") == fn_guard.BROKER


class TestPostTradeRewardRiskCheck:
    """MIN_REWARD_RISK_RATIO enforcement -- see the constant's own comment
    for the real Exness trade data (avg loss $2.02 vs avg win $1.17, a
    ~1.7:1 skew) that motivated this."""

    TRADE = {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.01}

    def _patch(self, monkeypatch, *, atr_floor=0.0002, spread_floor=0.0001, positions=None, modify_result=None):
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles
        import src.trading.service as service

        calls = {}
        monkeypatch.setattr(fr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.1000, "ask": 1.1001})
        monkeypatch.setattr(fr, "_atr_stop_floor", lambda symbol, connection: atr_floor)
        monkeypatch.setattr(fr, "_spread_stop_floor", lambda quote: spread_floor)
        default_positions = [{"ticket": "999", "symbol": "EURUSD", "magic": fr.OUR_MAGIC}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions if positions is not None else default_positions})
        monkeypatch.setattr(profiles, "profile_by_id", lambda conn: type("P", (), {"config": {}})())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: {})

        def _modify(config, ticket=None, stop_loss=None, take_profit=None):
            calls.update(ticket=ticket, stop_loss=stop_loss, take_profit=take_profit)
            return modify_result or {"status": "ok"}

        monkeypatch.setattr(mt5_sdk, "modify_position", _modify)
        return calls

    def test_ratio_already_healthy_is_a_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        # buy: risk 0.0010 (1.1000->1.0990), reward 0.0020 (1.1000->1.1020) = 2:1, above the 1.5 floor
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1020}
        assert fr._post_trade_reward_risk_check(self.TRADE, order) == ""
        assert calls == {}

    def test_tightens_stop_when_floor_allows(self, monkeypatch) -> None:
        # buy: risk 0.0010, reward 0.0010 = 1:1, below floor. Desired risk
        # for 1.5:1 = 0.0010/1.5 = 0.000667, which is >= atr_floor (0.0002)
        # -- safe to tighten the stop instead of touching the target.
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["ticket"] == "999"
        assert calls["take_profit"] == 1.1010  # target left untouched
        assert calls["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)

    def test_widens_target_when_tightening_would_violate_floor(self, monkeypatch) -> None:
        # buy: risk 0.0010, reward 0.0010 = 1:1. Desired risk for 1.5:1 =
        # 0.000667, but the ATR floor is 0.0009 -- tightening that far
        # would put the stop inside normal noise, so widen the target
        # instead: new reward = 0.0010 * 1.5 = 0.0015 -> new tp = 1.1015.
        calls = self._patch(monkeypatch, atr_floor=0.0009, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "widened take-profit" in note
        assert calls["stop_loss"] == 1.0990  # stop left untouched
        assert calls["take_profit"] == pytest.approx(1.1015)

    def test_sell_side_mirrors_the_math(self, monkeypatch) -> None:
        # sell: risk 0.0010 (1.1000->1.1010), reward 0.0010 (1.1000->1.0990) = 1:1
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001)
        order = {"side": "sell", "fill_price": 1.1000, "stop_loss": 1.1010, "take_profit": 1.0990}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["stop_loss"] == pytest.approx(1.1000 + 0.0010 / 1.5)

    def test_no_matching_position_reports_without_crashing(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, positions=[])
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "no matching open position" in note
        assert calls == {}

    def test_missing_fields_is_a_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        assert fr._post_trade_reward_risk_check(self.TRADE, {"side": "buy", "fill_price": 1.1}) == ""
        assert calls == {}

    def test_zero_fill_price_falls_back_to_live_position(self, monkeypatch) -> None:
        """Real incident 2026-09-17: trading_place_order's own response had
        fill_price=0.0 (MT5 order_send()/deal-fill propagation gap), which
        made the ratio math nonsensical and silently no-op'd -- the
        guardrail never actually ran on the first real trade. Must fall
        back to the live position's price_open instead of trusting 0.0."""
        positions = [{"ticket": "999", "symbol": "EURUSD", "magic": fr.OUR_MAGIC, "price_open": 1.1000}]
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001, positions=positions)
        # Same 1:1 shape as test_tightens_stop_when_floor_allows, but with
        # the real-incident's fill_price=0.0 instead of a valid one.
        order = {"side": "buy", "fill_price": 0.0, "stop_loss": 1.0990, "take_profit": 1.1010}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)

    def test_missing_fill_price_and_no_position_is_a_safe_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, positions=[])
        order = {"side": "buy", "fill_price": 0.0, "stop_loss": 1.0990, "take_profit": 1.1010}
        assert fr._post_trade_reward_risk_check(self.TRADE, order) == ""
        assert calls == {}


class TestResolveFillPrice:
    TRADE = {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.01}

    def test_uses_placed_order_fill_price_when_valid(self, monkeypatch) -> None:
        import src.trading.service as service

        def _boom(conn):
            raise AssertionError("get_positions should not be called when fill_price is already valid")

        monkeypatch.setattr(service, "get_positions", _boom)
        assert fr._resolve_fill_price(self.TRADE, {"fill_price": 1.1234, "symbol": "EURUSD"}) == 1.1234

    def test_falls_back_when_fill_price_is_zero(self, monkeypatch) -> None:
        import src.trading.service as service

        positions = [{"symbol": "EURUSD", "magic": fr.OUR_MAGIC, "price_open": 1.1500}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})
        assert fr._resolve_fill_price(self.TRADE, {"fill_price": 0.0, "symbol": "EURUSD"}) == 1.1500

    def test_falls_back_when_fill_price_missing(self, monkeypatch) -> None:
        import src.trading.service as service

        positions = [{"symbol": "EURUSD", "magic": fr.OUR_MAGIC, "price_open": 1.1500}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})
        assert fr._resolve_fill_price(self.TRADE, {"symbol": "EURUSD"}) == 1.1500

    def test_returns_none_when_no_source_has_a_price(self, monkeypatch) -> None:
        import src.trading.service as service

        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": []})
        assert fr._resolve_fill_price(self.TRADE, {"fill_price": 0.0, "symbol": "EURUSD"}) is None
