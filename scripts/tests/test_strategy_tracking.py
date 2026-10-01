"""Tests for scripts/strategy_tracking.py and its wiring into both reporters."""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import committee_reporter as cr
import fundednext_reporter as fr
import strategy_tracking as st


class TestParseDecision:
    @pytest.mark.parametrize("line, expected", [
        ("Decision: Short EURUSD at market, targeting 1.1350", "short"),
        ("Decision: long — buy the pullback", "long"),
        ("Decision: Wait — no clean setup on H1", "wait"),
        ("Decision: HOLD / no trade this session", "wait"),
        ("Decision: wait; would go long above 1.1400", "wait"),
        ("no decision line at all", "unknown"),
        ("**Direction:** SHORT — H4 and D1 both in confirmed downtrend", "short"),
        ("| **Side** | SELL (SHORT) |", "short"),
        ("### Committee Decision: **CONDITIONAL LONG (Limit-Only)**", "long"),
        ("**Verdict:** **HARD DOWNTREND across all timeframes. SHORT (SELL) is the only valid direction.**", "short"),
    ])
    def test_classifies(self, line, expected) -> None:
        assert st.parse_decision(f"blah\n{line}\nReasoning: x") == expected


class TestTrendRulePrompt:
    def test_names_the_forbidden_direction(self) -> None:
        text = st.trend_rule_prompt({"sell"}, "H4 and D1 are both in a DOWNTREND")
        assert "HARD TREND RULE" in text and "long/buy" in text and "trading_place_order" in text


class TestRecordAndOutcomes:
    def test_record_appends_versioned_row(self) -> None:
        st.record_decision("exness", "EURUSDm", "mt5-live-trade", decision="wait", traded=False, status="success")
        rows = st.read_decisions()
        assert len(rows) == 1 and rows[0]["version"] == st.STRATEGY_VERSION and rows[0]["decision"] == "wait"

    def _fake_bars(self, monkeypatch, bars):
        fake_sdk = types.SimpleNamespace(get_historical_bars_range=lambda *a, **k: {"bars": bars})
        monkeypatch.setattr(st.mdp, "_config_for", lambda connection: (fake_sdk, None))

    def test_fills_outcome_after_window_using_server_clock(self, monkeypatch) -> None:
        t0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        st._append({"ts": t0.isoformat(), "bot": "fundednext", "symbol": "EURUSD",
                    "connection": "mt5fn-live-trade", "decision": "wait", "price": 1.1400})
        eest = ZoneInfo("Europe/Bucharest")
        # Server wall clock = UTC+3: 15:00 "UTC" label is really 12:00 UTC (inside the window);
        # 23:30 label is 20:30 UTC (outside the 8h window, must be ignored).
        bars = [
            {"time": "2026-09-28T15:00:00+00:00", "high": 1.1420, "low": 1.1395, "close": 1.1410},
            {"time": "2026-09-28T19:55:00+00:00", "high": 1.1415, "low": 1.1380, "close": 1.1390},
            {"time": "2026-09-28T23:30:00+00:00", "high": 1.1600, "low": 1.1300, "close": 1.1500},
        ]
        self._fake_bars(monkeypatch, bars)

        assert st.fill_decision_outcomes("fundednext", eest, t0 + timedelta(hours=9)) == 1
        out = st.read_decisions()[0]["outcome"]
        assert out == {"hours": 8, "close_move_pips": -10.0, "max_up_pips": 20.0, "max_down_pips": 20.0}

    def test_not_due_yet_is_left_alone(self, monkeypatch) -> None:
        t0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        st._append({"ts": t0.isoformat(), "bot": "exness", "symbol": "EURUSDm",
                    "connection": "mt5-live-trade", "decision": "wait", "price": 1.14})
        self._fake_bars(monkeypatch, [])

        assert st.fill_decision_outcomes("exness", timezone.utc, t0 + timedelta(hours=7)) == 0
        assert "outcome" not in st.read_decisions()[0]

    def test_gold_pip_size_matches_market_data_pack(self, monkeypatch) -> None:
        """Real bug: this used to use pip=0.1 for XAU, 10x market_data_pack.py's
        pip=0.01 (digits=2) convention for the same instrument -- gold is
        currently paused in both bots' TARGETS, but the moment it resumes
        this would silently understate every close/max-up/max-down pip
        outcome by 10x."""
        t0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        st._append({"ts": t0.isoformat(), "bot": "exness", "symbol": "XAUUSDm",
                    "connection": "mt5-live-trade", "decision": "wait", "price": 2000.0})
        bars = [{"time": "2026-09-28T15:00:00+00:00", "high": 2001.0, "low": 1999.0, "close": 2000.5}]
        self._fake_bars(monkeypatch, bars)

        assert st.fill_decision_outcomes("exness", timezone.utc, t0 + timedelta(hours=9)) == 1
        out = st.read_decisions()[0]["outcome"]
        assert out == {"hours": 8, "close_move_pips": 50.0, "max_up_pips": 100.0, "max_down_pips": 100.0}


class TestWeeklyReport:
    def test_groups_by_version_in_r(self) -> None:
        journal = [
            {"status": "closed", "side": "buy", "entry_price": 1.0, "stop_loss": 0.99, "exit_price": 1.02, "profit": 2.0},
            {"status": "closed", "side": "sell", "entry_price": 1.0, "stop_loss": 1.01, "exit_price": 1.01,
             "profit": -1.0, "strategy_version": st.STRATEGY_VERSION},
        ]
        text = st.weekly_version_report(journal, "exness", decisions=[])
        assert f"{st.LEGACY_VERSION}: 1 trades, win rate 100%" in text and "+2.00R" in text
        assert f"{st.STRATEGY_VERSION}: 1 trades, win rate 0%" in text and "-1.00R" in text


@pytest.mark.parametrize("mod", [cr, fr], ids=["exness", "fundednext"])
class TestEnforceTrendRule:
    def _patch(self, monkeypatch, mod, symbol):
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.service as service

        closed = []
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": [
            {"ticket": 77, "symbol": symbol, "magic": mod.OUR_MAGIC, "side": "buy"}]})
        monkeypatch.setattr(mod, "_mt5_config_for", lambda conn: "CFG")
        monkeypatch.setattr(mt5_sdk, "close_position", lambda config, ticket: closed.append(ticket) or {"status": "ok"})
        return closed

    def test_closes_counter_trend_fill(self, monkeypatch, mod) -> None:
        closed = self._patch(monkeypatch, mod, "EURUSD")
        note = mod._enforce_trend_rule({"symbol": "EURUSD", "connection": "c"}, {"side": "buy", "order_id": "77"}, {"sell"})
        assert closed == [77] and "TREND GATE" in note

    def test_allowed_side_untouched(self, monkeypatch, mod) -> None:
        closed = self._patch(monkeypatch, mod, "EURUSD")
        assert mod._enforce_trend_rule({"symbol": "EURUSD", "connection": "c"}, {"side": "sell", "order_id": "77"}, {"sell"}) == ""
        assert mod._enforce_trend_rule({"symbol": "EURUSD", "connection": "c"}, {"side": "buy", "order_id": "77"}, None) == ""
        assert closed == []

    def test_switches_default_on(self, monkeypatch, mod) -> None:
        assert mod.TREND_FILTER_ENABLED is True


class TestRunOnceRecordsDecisions:
    def test_exness_records_committee_and_skip(self, monkeypatch) -> None:
        for name in ("_check_trade_drought", "_check_cap_fit_alert", "_check_llm_balance_alert", "_log_cap_gap"):
            monkeypatch.setattr(cr, name, lambda: None)
        monkeypatch.setattr(cr, "TARGETS", [
            {"committee": "fx_commodity_day_desk", "target": "EURUSD", "market": "forex",
             "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}},
            {"committee": "fx_commodity_day_desk", "target": "GBPUSD", "market": "forex",
             "trade": {"symbol": "GBPUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1}},
        ])
        opened = {"EURUSDm": {"count": 0, "side": None}}

        def _summary(symbol, connection):
            return opened.get(symbol, {"count": 0, "side": None})

        def _fake_run(**kw):
            opened["EURUSDm"] = {"count": 1, "side": "sell"}  # EURUSD trades, so GBPUSD is then skipped
            return cr.CommitteeResult(kw["committee"], kw["target"], kw["market"], "success", "r1",
                                      "Decision: Short EURUSD\nReasoning: trend", traded=True)

        monkeypatch.setattr(cr, "_symbol_position_summary", _summary)
        monkeypatch.setattr(cr, "run_committee", _fake_run)
        monkeypatch.setattr(cr, "is_reportable", lambda r: False)
        monkeypatch.setattr(cr, "send_email", lambda *a, **k: None)
        monkeypatch.setattr(cr, "_status_header", lambda: "")

        cr.run_once("new_york")

        rows = st.read_decisions()
        assert [(r["symbol"], r["decision"], r["traded"]) for r in rows] == [
            ("EURUSDm", "short", True), ("GBPUSDm", "skipped", False)]


class TestPromptCarriesTrendRule:
    def test_trend_block_reaches_prompt(self, monkeypatch) -> None:
        monkeypatch.setattr(cr, "_symbol_position_summary", lambda s, c: {"count": 0, "side": None})
        monkeypatch.setattr(cr, "_symbol_live_quote", lambda s, c: None)
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: None)
        for name in ("_signal_service_activity", "_journal_summary_text", "_session_bias_fact"):
            monkeypatch.setattr(cr, name, lambda *a, **k: "")
        monkeypatch.setattr(cr, "_journal_reconcile_closed", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(cr, "_effective_max_loss_usd", lambda c: 4.0)
        monkeypatch.setattr(cr, "_max_stop_distance", lambda *a, **k: 0.004)
        monkeypatch.setattr(cr, "_atr_stop_floor", lambda *a, **k: 0.001)
        trade = {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1,
                 "trend_rule": st.trend_rule_prompt({"sell"}, "both down")}

        prompt = cr._build_prompt("fx_commodity_day_desk", "EURUSD", "forex", trade)

        assert "HARD TREND RULE" in prompt
