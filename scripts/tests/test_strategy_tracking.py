"""Tests for scripts/strategy_tracking.py and its wiring into both reporters."""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


class TestValidateCommitteeFields:
    """D4 (2026-10-06, extended for v5.1 in D6): catch the LLM dropping a
    required field entirely -- something the email/journal pipeline had no
    way to detect before this."""

    VALID_TRADE = (
        "DECISION: LONG\nCONFIDENCE: 72\nMODE: LIVE\nDATA_MISSING: none\nNEWS_API_STATUS: OK\n"
        "EDGE: HTF_TREND_CONTINUATION -- H4/D1 both up\n"
        "CHECKLIST: 1. yes 2. yes\nORDER: EURUSDm buy 0.01 @ 1.1234\nINVALIDATION: 1.1180\n"
        "INPUT_PROVENANCE: mt5-live-trade, quote 12:00:00 UTC, pack 12:00:00 UTC\n"
        "PROPOSAL: none\nREASON FOR PASS: n/a"
    )
    VALID_RESEARCH_ONLY = (
        "DECISION: PASS\nCONFIDENCE: 0\nMODE: RESEARCH_ONLY\nDATA_MISSING: none\nNEWS_API_STATUS: OK\n"
        "EDGE: none\nCHECKLIST: n/a\n"
        "ORDER: none\nINVALIDATION: n/a\nINPUT_PROVENANCE: mt5-live-trade, quote 12:00:00 UTC, pack 12:00:00 UTC\n"
        "PROPOSAL: none\nREASON FOR PASS: news blackout"
    )

    def test_valid_trade_output_has_no_missing_fields(self) -> None:
        assert st.validate_committee_fields(self.VALID_TRADE) == []

    def test_valid_research_only_output_has_no_missing_fields(self) -> None:
        assert st.validate_committee_fields(self.VALID_RESEARCH_ONLY) == []

    def test_missing_single_field_is_reported(self) -> None:
        text = self.VALID_TRADE.replace("MODE: LIVE\n", "")
        assert st.validate_committee_fields(text) == ["MODE"]

    def test_missing_multiple_fields_are_all_reported(self) -> None:
        text = "DECISION: LONG\nCONFIDENCE: 72\n"
        missing = st.validate_committee_fields(text)
        assert missing == ["MODE", "DATA_MISSING", "NEWS_API_STATUS", "EDGE", "CHECKLIST", "ORDER",
                            "INVALIDATION", "INPUT_PROVENANCE", "PROPOSAL", "REASON FOR PASS"]

    def test_empty_report_is_missing_everything(self) -> None:
        assert st.validate_committee_fields("") == list(st.REQUIRED_COMMITTEE_FIELDS)

    def test_field_must_be_at_start_of_line(self) -> None:
        # A field name mentioned mid-sentence (not as its own labeled line)
        # must not count as present.
        text = "Some prose that mentions MODE: in passing, not as a real field.\n" + self.VALID_TRADE.replace("MODE: LIVE\n", "")
        assert "MODE" in st.validate_committee_fields(text)

    def test_markdown_bold_wrapped_fields_are_recognized(self) -> None:
        # Real incident 2026-10-07: a live report had every field correct
        # and complete but wrapped as "**FIELD: value**" -- the bare ^field:
        # anchor didn't match through the leading "**", so all 12 fields
        # registered as missing despite a well-formed report.
        text = "\n".join(f"**{line}**" for line in self.VALID_TRADE.split("\n"))
        assert st.validate_committee_fields(text) == []

    def test_numbered_checklist_line_does_not_false_match_a_field(self) -> None:
        # \W* tolerates punctuation/whitespace prefixes, never a digit --
        # a checklist line like "5. Reward:Risk 0.91:1" must not satisfy
        # any real field's presence check.
        text = self.VALID_TRADE.replace("CHECKLIST: 1. yes 2. yes", "CHECKLIST:\n5. Reward:Risk 0.91:1 below floor")
        assert st.validate_committee_fields(text) == []
        # And with CHECKLIST itself genuinely missing, the numbered line
        # must not be mistaken for it.
        text_missing = text.replace("CHECKLIST:\n5. Reward:Risk 0.91:1 below floor\n", "5. Reward:Risk 0.91:1 below floor\n")
        assert "CHECKLIST" in st.validate_committee_fields(text_missing)


class TestTrendRulePrompt:
    def test_names_the_forbidden_direction(self) -> None:
        text = st.trend_rule_prompt({"sell"}, "H4 and D1 are both in a DOWNTREND")
        assert "HARD TREND RULE" in text and "long/buy" in text and "trading_place_order" in text


class TestComputeAtrPercentile:
    """D10 (2026-10-08): deterministic ATR-percentile helper for regime
    detection. Synthetic OHLC series constructed so the True Range is known
    exactly: constant close (base), high/low symmetric around it by a
    strictly-increasing (or strictly-decreasing) spread per bar, so
    TR[i] = max(spread, spread/2, spread/2) = spread exactly (prev_close
    == close always, since close is constant)."""

    def _bars(self, base: float, spreads: list[float]) -> tuple[list[float], list[float], list[float]]:
        high = [base + s / 2 for s in spreads]
        low = [base - s / 2 for s in spreads]
        close = [base for _ in spreads]
        return high, low, close

    def test_monotonically_increasing_volatility_is_100th_percentile(self) -> None:
        # 7 bars, atr_period=3, lookback=5 -> exactly lookback ATR values
        # (7 - (3-1) = 5), so the single ranked value is the latest ATR,
        # which is also the maximum (TR strictly increasing) -> pct 100.
        high, low, close = self._bars(100.0, [1, 2, 3, 4, 5, 6, 7])
        result = st.compute_atr_percentile(high, low, close, atr_period=3, lookback=5)
        assert result == 100.0

    def test_monotonically_decreasing_volatility_is_lowest_percentile(self) -> None:
        # pandas rank(pct=True) gives the minimum of N values rank 1/N, not
        # 0 -- there is no "rank 0" in a 1-indexed rank, so the lowest
        # achievable pct in a 5-element window is 1/5 = 20%, not 0%.
        high, low, close = self._bars(100.0, [7, 6, 5, 4, 3, 2, 1])
        result = st.compute_atr_percentile(high, low, close, atr_period=3, lookback=5)
        assert result == 20.0

    def test_insufficient_history_returns_none_and_logs(self, caplog) -> None:
        import logging as _logging
        # 5 bars -> only 5-(3-1)=3 ATR values, fewer than lookback=5.
        high, low, close = self._bars(100.0, [1, 2, 3, 4, 5])
        with caplog.at_level(_logging.WARNING, logger="strategy_tracking"):
            result = st.compute_atr_percentile(high, low, close, atr_period=3, lookback=5)
        assert result is None
        assert "REGIME_INSUFFICIENT_HISTORY" in caplog.text

    def test_exact_lookback_count_is_sufficient(self) -> None:
        # len(atr_series) == lookback must pass (spec says "< lookback"
        # fails, not "<=").
        high, low, close = self._bars(100.0, [1, 2, 3, 4, 5, 6, 7])
        result = st.compute_atr_percentile(high, low, close, atr_period=3, lookback=5)
        assert result is not None

    def test_one_short_of_lookback_returns_none(self) -> None:
        high, low, close = self._bars(100.0, [1, 2, 3, 4, 5, 6])  # 6 bars -> 4 ATR values
        result = st.compute_atr_percentile(high, low, close, atr_period=3, lookback=5)
        assert result is None


class TestClassifyRegime:
    """D11 (2026-10-08). Boundary values per the task spec: 24.9/25.0/
    74.9/75.0/89.9/90.0, plus just above 90 for EXTREME. Each named cutoff
    belongs to the band at or above it (see REGIME_*_PERCENTILE docstring
    in strategy_tracking.py)."""

    @pytest.mark.parametrize("pct,label,size_mult,action,reason", [
        (0.0, "CALM", 0.0, "PASS_NO_LLM", "calm_regime"),
        (24.9, "CALM", 0.0, "PASS_NO_LLM", "calm_regime"),
        (25.0, "NORMAL", 1.0, "INVOKE_LLM", "normal_regime"),
        (50.0, "NORMAL", 1.0, "INVOKE_LLM", "normal_regime"),
        (74.9, "NORMAL", 1.0, "INVOKE_LLM", "normal_regime"),
        (75.0, "VOLATILE", 0.5, "INVOKE_LLM", "volatile_regime"),
        (89.9, "VOLATILE", 0.5, "INVOKE_LLM", "volatile_regime"),
        (90.0, "VOLATILE", 0.5, "INVOKE_LLM", "volatile_regime"),
        (90.1, "EXTREME", 0.0, "PASS_NO_LLM", "extreme_volatility"),
        (100.0, "EXTREME", 0.0, "PASS_NO_LLM", "extreme_volatility"),
    ])
    def test_boundary_values(self, pct, label, size_mult, action, reason) -> None:
        result = st.classify_regime(pct)
        assert result.label == label
        assert result.size_multiplier == size_mult
        assert result.action == action
        assert result.reason == reason
        assert result.atr_percentile == pct

    def test_none_maps_to_normal_with_warning(self, caplog) -> None:
        import logging as _logging
        with caplog.at_level(_logging.WARNING, logger="strategy_tracking"):
            result = st.classify_regime(None)
        assert result.label == "NORMAL"
        assert result.action == "INVOKE_LLM"
        assert result.size_multiplier == 1.0
        assert "REGIME_PERCENTILE_UNAVAILABLE" in caplog.text


class TestRegimeForSymbol:
    """D12's wiring: regime_for_symbol fetches bars via mdp._bars/_config_for
    (same pattern as trend_gate) and fails open to NORMAL on any data
    error -- a broker hiccup must never silently become a no-LLM PASS."""

    def _fake_bars_sdk(self, monkeypatch, bars):
        fake_sdk = types.SimpleNamespace(get_historical_bars=lambda *a, **k: {"bars": bars})
        monkeypatch.setattr(st.mdp, "_config_for", lambda connection: (fake_sdk, None))

    def test_classifies_from_fetched_bars(self, monkeypatch) -> None:
        # Monotonically increasing TR over exactly enough bars for a
        # lookback=5/atr_period=3 window -> 100th percentile -> EXTREME
        # at a threshold placed low enough to catch it.
        spreads = [1, 2, 3, 4, 5, 6, 7]
        bars = [{"high": 100 + s / 2, "low": 100 - s / 2, "close": 100.0} for s in spreads]
        self._fake_bars_sdk(monkeypatch, bars)
        result = st.regime_for_symbol("EURUSD", "mt5-live-trade", atr_period=3, lookback=5)
        assert result.atr_percentile == 100.0
        assert result.label == "EXTREME"

    def test_data_error_fails_open_to_normal(self, monkeypatch, caplog) -> None:
        import logging as _logging
        monkeypatch.setattr(st.mdp, "_config_for", lambda connection: (_ for _ in ()).throw(RuntimeError("MT5 down")))
        with caplog.at_level(_logging.WARNING, logger="strategy_tracking"):
            result = st.regime_for_symbol("EURUSD", "mt5-live-trade")
        assert result.label == "NORMAL"
        assert result.action == "INVOKE_LLM"
        assert "REGIME_DATA_UNAVAILABLE" in caplog.text


class TestPooledScaleStatusExcludesVolatile:
    """D13 (2026-10-08): VOLATILE-regime trades must not be folded into the
    pooled NORMAL scale/pause sample -- see pooled_scale_status's own
    docstring for why. A legacy entry with no "regime" field at all must
    still count (only an explicit "VOLATILE" tag is excluded)."""

    def _trade(self, *, regime=None, r=1.0, strategy_version=None):
        # entry/stop/exit chosen so _trade_r(entry) == r exactly (buy side,
        # risk=1.0 price unit, exit = entry + r).
        entry = {
            "status": "closed", "side": "buy", "entry_price": 100.0, "stop_loss": 99.0,
            "exit_price": 100.0 + r, "strategy_version": strategy_version or st.STRATEGY_VERSION,
        }
        if regime is not None:
            entry["regime"] = regime
        return entry

    def _write_journals(self, monkeypatch, tmp_path, exness_trades, fn_trades):
        exness_path = tmp_path / "exness.json"
        fn_path = tmp_path / "fn.json"
        exness_path.write_text(json.dumps(exness_trades), encoding="utf-8")
        fn_path.write_text(json.dumps(fn_trades), encoding="utf-8")
        monkeypatch.setattr(st, "EXNESS_JOURNAL_PATH", exness_path)
        monkeypatch.setattr(st, "FUNDEDNEXT_JOURNAL_PATH", fn_path)

    def test_volatile_trade_excluded_from_pooled_sample(self, monkeypatch, tmp_path) -> None:
        self._write_journals(monkeypatch, tmp_path, [self._trade(regime="NORMAL", r=1.0)], [self._trade(regime="VOLATILE", r=2.0)])
        n, total = st.pooled_scale_status()
        assert n == 1
        assert total == 1.0

    def test_legacy_trade_with_no_regime_field_still_counts(self, monkeypatch, tmp_path) -> None:
        self._write_journals(monkeypatch, tmp_path, [self._trade(regime=None, r=1.5)], [])
        n, total = st.pooled_scale_status()
        assert n == 1
        assert total == 1.5

    def test_volatile_regime_sample_contains_only_volatile(self, monkeypatch, tmp_path) -> None:
        self._write_journals(
            monkeypatch, tmp_path,
            [self._trade(regime="NORMAL", r=1.0), self._trade(regime="VOLATILE", r=-0.5)],
            [self._trade(regime="VOLATILE", r=2.0)],
        )
        n, total = st.volatile_regime_sample()
        assert n == 2
        assert total == 1.5


class TestRegimeRulePrompt:
    def test_carries_all_three_fields(self) -> None:
        regime = st.RegimeResult("VOLATILE", 82.0, 0.5, "INVOKE_LLM", "volatile_regime")
        text = st.regime_rule_prompt(regime)
        assert "regime: VOLATILE" in text
        assert "atr_percentile: 82" in text
        assert "size_multiplier: 0.5" in text

    def test_normal_regime(self) -> None:
        regime = st.RegimeResult("NORMAL", 50.0, 1.0, "INVOKE_LLM", "normal_regime")
        text = st.regime_rule_prompt(regime)
        assert "regime: NORMAL" in text
        assert "size_multiplier: 1.0" in text


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

    def test_groups_trades_by_regime(self) -> None:
        # D16 (2026-10-08): "New setup trades by regime" line.
        journal = [
            {"status": "closed", "side": "buy", "entry_price": 1.0, "stop_loss": 0.99, "exit_price": 1.02,
             "profit": 2.0, "strategy_version": st.STRATEGY_VERSION, "regime": "NORMAL"},
            {"status": "closed", "side": "buy", "entry_price": 1.0, "stop_loss": 0.99, "exit_price": 1.01,
             "profit": 1.0, "strategy_version": st.STRATEGY_VERSION, "regime": "VOLATILE"},
            {"status": "closed", "side": "buy", "entry_price": 1.0, "stop_loss": 0.99, "exit_price": 1.00,
             "profit": 0.0, "strategy_version": st.STRATEGY_VERSION},  # no regime field -> "untagged"
        ]
        text = st.weekly_version_report(journal, "exness", decisions=[])
        assert "New setup trades by regime:" in text
        assert "NORMAL 1 trades" in text
        assert "VOLATILE 1 trades" in text
        assert "untagged 1 trades" in text

    def test_regime_breakdown_of_passes_from_decisions(self) -> None:
        # D16: "Regime breakdown (trade-enabled passes)" line, from the
        # decision log, not the trade journal -- counts CALM/EXTREME
        # no-LLM passes too, which never produce a journal entry at all.
        decisions = [
            {"bot": "exness", "decision": "wait", "regime": "CALM"},
            {"bot": "exness", "decision": "wait", "regime": "CALM"},
            {"bot": "exness", "decision": "long", "regime": "NORMAL"},
            {"bot": "exness", "decision": "wait", "regime": None},  # untagged, excluded
            {"bot": "fundednext", "decision": "wait", "regime": "EXTREME"},  # different bot, excluded
        ]
        text = st.weekly_version_report([], "exness", decisions=decisions)
        assert "Regime breakdown (trade-enabled passes): CALM 2, NORMAL 1" in text
        assert "EXTREME" not in text


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
        # A real (if fake) path -- None now means "no data pack, hard PASS,
        # don't invoke the LLM" (D2, 2026-10-06), which isn't what this test
        # is exercising.
        monkeypatch.setattr(cr.market_data_pack, "write_data_pack", lambda s, c: Path("fake_pack.md"))
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
