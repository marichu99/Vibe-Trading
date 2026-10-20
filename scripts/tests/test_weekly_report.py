"""Weekly report additions (2026-09-30): scale rule, trend split, missed-move stat,
OpenRouter reminder, and FundedNext's broker-clock close-time fix."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import fundednext_reporter as fr
import strategy_tracking as st


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(st, "openrouter_balance_usd", lambda: None)


@pytest.fixture(autouse=True)
def _empty_journals(monkeypatch, tmp_path):
    """pooled_scale_status reads both bots' journal files directly (not a
    parameter) -- point both at empty, isolated files by default so tests
    never see this machine's real trade history. Tests that want specific
    pooled numbers override one or both paths themselves."""
    monkeypatch.setattr(st, "EXNESS_JOURNAL_PATH", tmp_path / "exness_journal.json")
    monkeypatch.setattr(st, "FUNDEDNEXT_JOURNAL_PATH", tmp_path / "fn_journal.json")


class TestScaleVerdict:
    def test_keeps_collecting_below_ten_trades(self) -> None:
        assert "keep collecting (4/10" in st.scale_verdict(4, 3.0)

    def test_scales_up_after_ten_trades_at_plus_two_r(self) -> None:
        assert "SCALE UP" in st.scale_verdict(10, 2.0)

    def test_holds_after_ten_trades_below_plus_two_r(self) -> None:
        assert "hold current size" in st.scale_verdict(12, 1.5)

    def test_pauses_at_minus_three_r_any_time(self) -> None:
        assert "PAUSE" in st.scale_verdict(3, -3.0)


def _trade(version, r, trend=None):
    return {"status": "closed", "strategy_version": version, "side": "buy", "entry_price": 1.0,
            "stop_loss": 0.99, "exit_price": 1.0 + 0.01 * r, "profit": r, "trend_alignment": trend}


def test_report_counts_v2_and_v3_and_splits_by_trend() -> None:
    journal = [_trade("v2-fxdesk-datapack-plainexit-trendgate", 2.0, "with"),
               _trade(st.STRATEGY_VERSION, -1.0, "neutral"), _trade(st.LEGACY_VERSION, -1.0)]
    st.FUNDEDNEXT_JOURNAL_PATH.write_text(json.dumps(journal))
    text = st.weekly_version_report(journal, "fundednext", decisions=[])
    assert "Scale rule (new setup, v2+v3): 2 closed trades, +1.00R" in text
    assert "neutral 1 trades -1.00R" in text and "with 1 trades +2.00R" in text


def test_pooled_scale_status_sums_both_bots_journals() -> None:
    # The scale rule is pre-committed on the POOLED sample -- a trade logged
    # on one account must count toward the other's scale/pause decision, not
    # just its own half of the history.
    st.EXNESS_JOURNAL_PATH.write_text(json.dumps([_trade(st.STRATEGY_VERSION, 1.5)]))
    st.FUNDEDNEXT_JOURNAL_PATH.write_text(json.dumps([_trade(st.STRATEGY_VERSION, -0.5)]))
    n, total = st.pooled_scale_status()
    assert n == 2
    assert total == pytest.approx(1.0)


def test_strategic_context_prompt_silent_once_a_trigger_fires() -> None:
    st.EXNESS_JOURNAL_PATH.write_text(json.dumps([]))
    st.FUNDEDNEXT_JOURNAL_PATH.write_text(json.dumps(
        [_trade(st.STRATEGY_VERSION, -1.0) for _ in range(2)]))
    assert "STRATEGIC CONTEXT" in st.strategic_context_prompt()
    st.FUNDEDNEXT_JOURNAL_PATH.write_text(json.dumps(
        [_trade(st.STRATEGY_VERSION, -1.0) for _ in range(3)]))
    assert st.strategic_context_prompt() == ""


def test_missed_move_stat_for_waits() -> None:
    decisions = [
        {"bot": "exness", "decision": "wait", "outcome": {"max_up_pips": 35.0, "max_down_pips": 5.0}},
        {"bot": "exness", "decision": "wait", "outcome": {"max_up_pips": 4.0, "max_down_pips": 8.0}},
    ]
    text = st.weekly_version_report([], "exness", decisions=decisions)
    assert "1 of 2 were followed by a 30+ pip one-way move" in text


def test_low_openrouter_balance_reminder(monkeypatch) -> None:
    monkeypatch.setattr(st, "openrouter_balance_usd", lambda: 3.0)
    assert "LOW: top up and enable auto top-up" in st.weekly_version_report([], "exness", decisions=[])


def test_fundednext_deal_time_converted_to_utc() -> None:
    # The live 2026-09-29 close: broker clock 19:45 "UTC" = 16:45 real UTC.
    assert fr._broker_time_to_utc("2026-09-29T19:45:25+00:00") == datetime(2026, 9, 29, 16, 45, 25, tzinfo=timezone.utc)
