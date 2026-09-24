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

import json
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


# ---------------------------------------------------------------------------
# _next_session_boundary -- mirrored from committee_reporter.py's identical
# feature/tests (2026-09-21). See that file's test class for the full
# DST-transition rationale; kept here in full (not abbreviated) since a
# regression in either copy is equally capable of misfiring a real trade.
# ---------------------------------------------------------------------------


class TestNextSessionBoundary:
    def test_winter_standard_time_offsets(self) -> None:
        now = datetime(2026, 1, 15, 0, 0, 1, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc), "london")

        now = boundary + timedelta(seconds=1)
        boundary, session = fr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc), "new_york")

    def test_summer_dst_offsets(self) -> None:
        now = datetime(2026, 6, 15, 0, 0, 1, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc), "london")

        now = boundary + timedelta(seconds=1)
        boundary, session = fr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), "new_york")

    def test_before_asia_open_returns_asia(self) -> None:
        now = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc) - timedelta(seconds=1)
        boundary, session = fr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc), "asia")

    def test_after_last_boundary_rolls_to_tomorrows_asia(self) -> None:
        now = datetime(2026, 9, 21, 12, 0, 1, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(now)
        assert (boundary, session) == (datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc), "asia")

    def test_exactly_at_a_boundary_is_not_returned_again(self) -> None:
        asia_open = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(asia_open)
        assert session != "asia"

    def test_america_new_york_dst_spring_forward_2026(self) -> None:
        before = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(before)
        assert (boundary, session) == (datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc), "new_york")

        after = datetime(2026, 3, 9, 11, 59, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(after)
        assert (boundary, session) == (datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc), "new_york")

    def test_europe_london_dst_spring_forward_2026(self) -> None:
        before = datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(before)
        assert (boundary, session) == (datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc), "london")

        after = datetime(2026, 3, 30, 1, 0, tzinfo=timezone.utc)
        boundary, session = fr._next_session_boundary(after)
        assert (boundary, session) == (datetime(2026, 3, 30, 7, 0, tzinfo=timezone.utc), "london")


class TestParseDecisionReasoning:
    def test_parses_well_formed_report(self) -> None:
        text = "Decision: long, momentum favors EURUSD\nReasoning: broke resistance on volume\nConfidence: high"
        assert fr._parse_decision_reasoning(text) == ("long, momentum favors EURUSD", "broke resistance on volume")

    def test_missing_decision_returns_none(self) -> None:
        assert fr._parse_decision_reasoning("Reasoning: because\nConfidence: high") is None

    def test_garbage_text_returns_none(self) -> None:
        assert fr._parse_decision_reasoning("the committee had an error") is None


class TestSessionBiasReadWrite:
    def test_record_then_fact_round_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        fr._record_session_bias("EURUSD", "asia", "Decision: long\nReasoning: broke resistance")

        fact = fr._session_bias_fact("EURUSD")

        assert "Asia session read on EURUSD today" in fact
        assert "long" in fact and "broke resistance" in fact

    def test_no_entries_returns_empty_string(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fr, "SESSION_BIAS_STATE_PATH", tmp_path / "session_bias.json")
        assert fr._session_bias_fact("EURUSD") == ""

    def test_stale_prior_day_entry_is_ignored(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "session_bias.json"
        monkeypatch.setattr(fr, "SESSION_BIAS_STATE_PATH", path)
        path.write_text(
            json.dumps({"EURUSD": {"asia": {"date": "2020-01-01", "decision": "long", "reasoning": "old"}}}),
            encoding="utf-8",
        )
        assert fr._session_bias_fact("EURUSD") == ""


class TestRunOnceSessionGating:
    def _patch(self, monkeypatch, *, report_text: str = "Decision: long\nReasoning: because") -> list[dict]:
        calls: list[dict] = []
        # Existing tests cover the research-pass path itself; the kill
        # switch (off in production) is covered by the tests below.
        monkeypatch.setattr(fr, "RESEARCH_PASSES_ENABLED", True)
        monkeypatch.setattr(fr, "is_reportable", lambda result: False)
        monkeypatch.setattr(
            fr, "TARGETS",
            [{
                "committee": "investment_committee", "target": "EURUSD", "market": "forex",
                "trade": {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.24, "max_stack": 1},
            }],
        )

        def _fake_run_committee(**kwargs):
            calls.append(kwargs)
            return fr.CommitteeResult(
                committee=kwargs["committee"], target=kwargs["target"], market=kwargs["market"],
                status="success", run_id="r1", report_text=report_text, traded=False,
            )

        monkeypatch.setattr(fr, "run_committee", _fake_run_committee)
        return calls

    def test_asia_session_strips_trade_and_records_bias(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        recorded = []
        monkeypatch.setattr(fr, "_record_session_bias", lambda symbol, session, text: recorded.append((symbol, session, text)))

        fr.run_once("asia")

        assert calls[0]["trade"] is None
        assert recorded == [("EURUSD", "asia", "Decision: long\nReasoning: because")]

    def test_new_york_session_keeps_trade_and_does_not_record_bias(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        recorded = []
        monkeypatch.setattr(fr, "_record_session_bias", lambda *a: recorded.append(a))

        fr.run_once("new_york")

        assert calls[0]["trade"] == {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.24, "max_stack": 1}
        assert recorded == []

    def test_research_passes_disabled_skips_committee_on_asia_and_london(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        monkeypatch.setattr(fr, "RESEARCH_PASSES_ENABLED", False)
        recorded = []
        monkeypatch.setattr(fr, "_record_session_bias", lambda *a: recorded.append(a))

        fr.run_once("asia")
        fr.run_once("london")

        assert calls == []
        assert recorded == []

    def test_research_passes_disabled_still_runs_new_york(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        monkeypatch.setattr(fr, "RESEARCH_PASSES_ENABLED", False)
        monkeypatch.setattr(fr, "_record_session_bias", lambda *a: None)

        fr.run_once("new_york")

        assert len(calls) == 1
        assert calls[0]["trade"] is not None

    def test_targets_list_itself_is_never_mutated(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        monkeypatch.setattr(fr, "_record_session_bias", lambda *a: None)
        original_trade = fr.TARGETS[0]["trade"]

        fr.run_once("asia")

        assert fr.TARGETS[0]["trade"] is original_trade


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


class TestProfitProtectionCheckSilentLookupFailures:
    """Mirrored from committee_reporter.py's identical test class
    (2026-09-21): point_size/contract_size used to fail completely
    silently (bare except, no log) inside _profit_protection_check's
    breakeven/trail rules -- found while investigating a gold reversal
    trade on this account that moved 89% of the way to target, past every
    protection trigger, and still closed at a near-full loss ($39.82).
    These confirm a lookup failure now logs a warning AND the position
    still gets whatever protection the OTHER rule can still provide."""

    SYMBOL = "EURUSD"
    CONNECTION = "mt5fn-live-trade"

    def _trade(self, **overrides) -> dict:
        base = {"symbol": self.SYMBOL, "connection": self.CONNECTION, "lots": 0.24, "max_stack": 1}
        base.update(overrides)
        return base

    def _position(self, *, hours_open: float, side="buy", entry=1.1600, sl=1.1580, tp=1.1650,
                   price=1.1605, ticket="1", profit=0.0) -> dict:
        opened = (datetime.now(timezone.utc) - timedelta(hours=hours_open)).isoformat()
        return {
            "ticket": ticket, "symbol": self.SYMBOL, "magic": fr.OUR_MAGIC, "side": side,
            "price_open": entry, "stop_loss": sl, "take_profit": tp, "price_current": price,
            "time": opened, "profit": profit,
        }

    def _patch_broker(self, monkeypatch, *, positions, atr_floor=0.0010, modify_result=None, trade_overrides=None) -> dict:
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles_module
        import src.trading.service as service

        trade = self._trade(**(trade_overrides or {}))
        monkeypatch.setattr(fr, "TARGETS", [{"committee": "x", "target": "x", "market": "forex", "trade": trade}])
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions})

        class _FakeProfile:
            config: dict = {}

        monkeypatch.setattr(profiles_module, "profile_by_id", lambda conn: _FakeProfile())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda profile_config, overrides: "FAKE_CONFIG")
        monkeypatch.setattr(mt5_sdk, "point_size", lambda symbol, config=None: 0.00001)
        monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol, config=None: 100_000)
        monkeypatch.setattr(fr, "_atr_stop_floor", lambda symbol, connection: atr_floor)

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
        # (100_000 * 0.24) = 0.0000417), past the trail's own $ trigger
        # too -- so trail_candidate can independently protect this
        # position even with point_size broken.
        pos = self._position(hours_open=1.0, ticket="T4", side="buy", entry=1.1600, sl=1.1580, tp=1.1620, price=1.1615)
        calls = self._patch_broker(
            monkeypatch, positions=[pos], atr_floor=0.0010, trade_overrides={"early_profit_trigger_usd": 1.00},
        )
        monkeypatch.setattr(mt5_sdk, "point_size", _raise_point_size)

        with caplog.at_level("WARNING"):
            fr._profit_protection_check()

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
            fr._profit_protection_check()

        assert any("contract_size lookup failed" in r.message for r in caplog.records)
        assert calls["modify"], "breakeven rule should still have protected the position"


class TestPostTradeSpecCheck:
    """Real incident 2026-09-17: the committee filled an order with the
    wrong symbol, 25x the mandated lot size, as a limit order -- directly
    contradicting the prompt's hardcoded trading_place_order() template.
    This is the backstop: any filled order that doesn't match TARGETS'
    exact spec gets closed immediately and halts further trading, rather
    than just being noted in the email afterward.
    """

    TRADE = {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.01, "max_stack": 1}
    NEW_TICKET = "999"

    def _patch(self, monkeypatch, *, positions=None):
        import src.live.halt as halt
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.service as service

        tripped: dict = {}
        closed_tickets: list = []
        monkeypatch.setattr(halt, "trip_halt", lambda by, reason, broker: tripped.update(by=by, reason=reason, broker=broker))
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions if positions is not None else []})
        monkeypatch.setattr(fr, "_mt5_config_for", lambda connection: {})

        def _close(config, ticket):
            closed_tickets.append(ticket)
            return {"status": "ok"}

        monkeypatch.setattr(mt5_sdk, "close_position", _close)
        return tripped, closed_tickets

    def test_matching_order_is_a_no_op(self, monkeypatch) -> None:
        tripped, closed_tickets = self._patch(monkeypatch)
        order = {"symbol": "EURUSD", "quantity": 0.01, "order_type": "market", "order_id": self.NEW_TICKET}
        assert fr._post_trade_spec_check(self.TRADE, order) == ""
        assert tripped == {} and closed_tickets == []

    def test_wrong_symbol_closes_and_halts(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSDm", "magic": fr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSDm", "quantity": 0.01, "order_type": "market", "order_id": self.NEW_TICKET}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "AUTO-CLOSED" in note and "EURUSDm" in note
        assert tripped.get("broker") == fn_guard.BROKER
        assert closed_tickets == [self.NEW_TICKET]

    def test_wrong_quantity_closes_and_halts(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSD", "quantity": 0.25, "order_type": "market", "order_id": self.NEW_TICKET}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "0.25" in note
        assert tripped.get("broker") == fn_guard.BROKER
        assert closed_tickets == [self.NEW_TICKET]

    def test_limit_order_closes_and_halts(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSD", "quantity": 0.01, "order_type": "limit", "order_id": self.NEW_TICKET}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "not a market order" in note
        assert tripped.get("broker") == fn_guard.BROKER
        assert closed_tickets == [self.NEW_TICKET]

    def test_multiple_mismatches_all_reported_in_one_pass(self, monkeypatch) -> None:
        tripped, closed_tickets = self._patch(monkeypatch, positions=[])
        order = {"symbol": "EURUSDm", "quantity": 0.25, "order_type": "limit", "order_id": self.NEW_TICKET}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "EURUSDm" in note and "0.25" in note and "not a market order" in note
        assert "no open position found with ticket" in note
        assert tripped.get("broker") == fn_guard.BROKER

    def test_does_not_close_other_legitimate_stacked_positions(self, monkeypatch) -> None:
        """Real bug found by /code-review: fn_guard._flatten_positions
        matches by symbol+magic alone, which force-closed ALL positions on
        the symbol, including healthy pre-existing stacked ones -- must
        only touch the new violating fill."""
        positions = [
            {"ticket": "111", "symbol": "EURUSD", "magic": fr.OUR_MAGIC},  # pre-existing, healthy
            {"ticket": "222", "symbol": "EURUSD", "magic": fr.OUR_MAGIC},  # pre-existing, healthy
            {"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC},  # the new, bad one
        ]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSD", "quantity": 0.25, "order_type": "market", "order_id": self.NEW_TICKET}
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note
        assert closed_tickets == [self.NEW_TICKET]

    def test_missing_order_id_refuses_to_close_anything(self, monkeypatch) -> None:
        positions = [{"ticket": "111", "symbol": "EURUSD", "magic": fr.OUR_MAGIC}]
        tripped, closed_tickets = self._patch(monkeypatch, positions=positions)
        order = {"symbol": "EURUSD", "quantity": 0.25, "order_type": "market"}  # no order_id
        note = fr._post_trade_spec_check(self.TRADE, order)
        assert "CRITICAL" in note and "refusing to blindly close" in note
        assert closed_tickets == []


class TestPostTradeRewardRiskCheck:
    """MIN_REWARD_RISK_RATIO enforcement -- see the constant's own comment
    for the real Exness trade data (avg loss $2.02 vs avg win $1.17, a
    ~1.7:1 skew) that motivated this."""

    TRADE = {"symbol": "EURUSD", "connection": "mt5fn-live-trade", "lots": 0.01}
    NEW_TICKET = "999"

    def _patch(self, monkeypatch, *, atr_floor=0.0002, spread_floor=0.0001, positions=None, modify_result=None):
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.service as service

        calls = {}
        monkeypatch.setattr(fr, "_symbol_live_quote", lambda symbol, conn: {"bid": 1.1000, "ask": 1.1001})
        monkeypatch.setattr(fr, "_atr_stop_floor", lambda symbol, connection: atr_floor)
        monkeypatch.setattr(fr, "_spread_stop_floor", lambda quote: spread_floor)
        default_positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC}]
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": positions if positions is not None else default_positions})
        monkeypatch.setattr(fr, "_mt5_config_for", lambda connection: {})

        def _modify(config, ticket=None, stop_loss=None, take_profit=None):
            calls.update(ticket=ticket, stop_loss=stop_loss, take_profit=take_profit)
            return modify_result or {"status": "ok"}

        monkeypatch.setattr(mt5_sdk, "modify_position", _modify)
        return calls

    def test_ratio_already_healthy_is_a_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch)
        # buy: risk 0.0010 (1.1000->1.0990), reward 0.0020 (1.1000->1.1020) = 2:1, above the 1.5 floor
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1020, "order_id": self.NEW_TICKET}
        assert fr._post_trade_reward_risk_check(self.TRADE, order) == ""
        assert calls == {}

    def test_tightens_stop_when_floor_allows(self, monkeypatch) -> None:
        # buy: risk 0.0010, reward 0.0010 = 1:1, below floor. Desired risk
        # for 1.5:1 = 0.0010/1.5 = 0.000667, which is >= atr_floor (0.0002)
        # -- safe to tighten the stop instead of touching the target.
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["ticket"] == self.NEW_TICKET
        assert calls["take_profit"] == 1.1010  # target left untouched
        assert calls["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)
        assert order["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)
        assert order["take_profit"] == 1.1010

    def test_widens_target_when_tightening_would_violate_floor(self, monkeypatch) -> None:
        # buy: risk 0.0010, reward 0.0010 = 1:1. Desired risk for 1.5:1 =
        # 0.000667, but the ATR floor is 0.0009 -- tightening that far
        # would put the stop inside normal noise, so widen the target
        # instead: new reward = 0.0010 * 1.5 = 0.0015 -> new tp = 1.1015.
        calls = self._patch(monkeypatch, atr_floor=0.0009, spread_floor=0.0001)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "widened take-profit" in note
        assert calls["stop_loss"] == 1.0990  # stop left untouched
        assert calls["take_profit"] == pytest.approx(1.1015)

    def test_sell_side_mirrors_the_math(self, monkeypatch) -> None:
        # sell: risk 0.0010 (1.1000->1.1010), reward 0.0010 (1.1000->1.0990) = 1:1
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001)
        order = {"side": "sell", "fill_price": 1.1000, "stop_loss": 1.1010, "take_profit": 1.0990, "order_id": self.NEW_TICKET}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["stop_loss"] == pytest.approx(1.1000 + 0.0010 / 1.5)

    def test_no_matching_position_reports_without_crashing(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, positions=[])
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
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
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC, "price_open": 1.1000}]
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001, positions=positions)
        # Same 1:1 shape as test_tightens_stop_when_floor_allows, but with
        # the real-incident's fill_price=0.0 instead of a valid one.
        order = {"side": "buy", "fill_price": 0.0, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note and "tightened stop-loss" in note
        assert calls["stop_loss"] == pytest.approx(1.1000 - 0.0010 / 1.5)

    def test_missing_fill_price_and_no_position_is_a_safe_no_op(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, positions=[])
        order = {"side": "buy", "fill_price": 0.0, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        assert fr._post_trade_reward_risk_check(self.TRADE, order) == ""
        assert calls == {}

    def test_does_not_correct_other_legitimate_stacked_positions(self, monkeypatch) -> None:
        """Real bug found by /code-review: matching ours[0] (whatever the
        broker happened to return first) could silently modify an older,
        already-compliant stacked position while leaving the actual
        sub-floor new fill uncorrected -- must only touch the new ticket."""
        positions = [
            {"ticket": "111", "symbol": "EURUSD", "magic": fr.OUR_MAGIC},  # pre-existing, healthy
            {"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC},  # the new, sub-floor one
        ]
        calls = self._patch(monkeypatch, atr_floor=0.0002, spread_floor=0.0001, positions=positions)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010, "order_id": self.NEW_TICKET}
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "CORRECTED" in note
        assert calls["ticket"] == self.NEW_TICKET

    def test_missing_order_id_reports_without_crashing(self, monkeypatch) -> None:
        positions = [{"ticket": self.NEW_TICKET, "symbol": "EURUSD", "magic": fr.OUR_MAGIC}]
        calls = self._patch(monkeypatch, positions=positions)
        order = {"side": "buy", "fill_price": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1010}  # no order_id
        note = fr._post_trade_reward_risk_check(self.TRADE, order)
        assert "no matching open position" in note
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
