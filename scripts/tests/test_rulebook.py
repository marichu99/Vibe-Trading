"""Tests for the prop-firm rulebook (2026-09-30), both bots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import committee_reporter as cr
import fundednext_guardrails as fn_guard
import fundednext_reporter as fr
import strategy_tracking as st

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _closed(outcome, hours_ago, *, symbol="EURUSD", exit_price=1.0990, review=None, stop_adjusted=False):
    t = {"symbol": symbol, "status": "closed", "outcome": outcome, "side": "buy", "entry_price": 1.1000,
         "stop_loss": 1.0990, "take_profit": 1.1020, "exit_price": exit_price,
         "closed_at": (NOW - timedelta(hours=hours_ago)).isoformat(), "stop_adjusted": stop_adjusted}
    if review is not None:
        t["review"] = review
    return t


class TestReview:
    def test_exit_at_stop_or_target_followed_plan(self) -> None:
        assert st.review_closed_trade(_closed("loss", 1, exit_price=1.0990))["deviation"] == "none"
        assert st.review_closed_trade(_closed("win", 1, exit_price=1.1020))["deviation"] == "none"

    def test_exit_between_levels_is_early_exit(self) -> None:
        r = st.review_closed_trade(_closed("win", 1, exit_price=1.1005))
        assert r["deviation"] == "early_exit" and r["lines"][0].endswith("No") and len(r["lines"]) == 3

    def test_stop_adjusted_flag_wins(self) -> None:
        assert st.review_closed_trade(_closed("loss", 1, stop_adjusted=True))["deviation"] == "stop_adjusted"

    def test_missing_exit_is_unknown(self) -> None:
        assert st.review_closed_trade({"entry_price": 1.1})["deviation"] == "unknown_exit"


class TestBench:
    def test_three_losses_within_24h_benches(self) -> None:
        j = [_closed("loss", 50), _closed("loss", 30), _closed("loss", 5)]
        assert "[BENCHED] EURUSD" in st.bench_reason(j, "EURUSD", NOW)

    def test_bench_expires_after_24h(self) -> None:
        j = [_closed("loss", 80), _closed("loss", 60), _closed("loss", 25)]
        assert st.bench_reason(j, "EURUSD", NOW) is None

    def test_a_win_in_the_last_three_clears_it(self) -> None:
        j = [_closed("loss", 30), _closed("win", 20), _closed("loss", 5)]
        assert st.bench_reason(j, "EURUSD", NOW) is None

    def test_other_symbols_ignored(self) -> None:
        j = [_closed("loss", 30, symbol="GBPUSD"), _closed("loss", 20, symbol="GBPUSD"), _closed("loss", 5, symbol="GBPUSD")]
        assert st.bench_reason(j, "EURUSD", NOW) is None


class TestRepeatDeviation:
    def test_same_deviation_twice_passes_next_setup_once(self) -> None:
        j = [_closed("win", 30, review={"deviation": "early_exit"}), _closed("loss", 5, review={"deviation": "early_exit"})]
        assert "[REVIEW PASS]" in st.consume_repeat_deviation(j, "EURUSD")
        assert st.consume_repeat_deviation(j, "EURUSD") is None  # served once

    def test_no_deviation_never_passes(self) -> None:
        j = [_closed("loss", 30, review={"deviation": "none"}), _closed("loss", 5, review={"deviation": "none"})]
        assert st.consume_repeat_deviation(j, "EURUSD") is None

    def test_different_deviations_dont_pass(self) -> None:
        j = [_closed("win", 30, review={"deviation": "early_exit"}), _closed("loss", 5, review={"deviation": "stop_adjusted"})]
        assert st.consume_repeat_deviation(j, "EURUSD") is None


def test_fundednext_limits() -> None:
    assert fn_guard.RISK_PER_TRADE_FRACTION == 0.0075
    assert fn_guard.DAILY_LOSS_HALT_PCT == 0.020
    assert fn_guard.MAX_DRAWDOWN_HALT_PCT == 0.048  # kept: stricter than the rulebook's 5%
    assert fr.NEWS_BLACKOUT_WINDOW_MINUTES == 120 and cr.NEWS_BLACKOUT_WINDOW_MINUTES == 120


@pytest.mark.parametrize("mod", [cr, fr], ids=["exness", "fundednext"])
class TestMaxStop:
    def _setup(self, monkeypatch, mod):
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.service as service

        calls = []
        monkeypatch.setattr(mod, "_resolve_fill_price", lambda trade, order: 1.1000)
        monkeypatch.setattr(mod, "_mt5_config_for", lambda c: "CFG")
        monkeypatch.setattr(service, "get_positions", lambda c: {"positions": [
            {"ticket": 9, "symbol": "EURUSD", "magic": mod.OUR_MAGIC}]})
        monkeypatch.setattr(mt5_sdk, "modify_position",
                            lambda config, *, ticket, stop_loss, take_profit: calls.append(stop_loss) or {"status": "ok"})
        return calls

    def test_stop_wider_than_one_percent_is_tightened(self, monkeypatch, mod) -> None:
        calls = self._setup(monkeypatch, mod)
        order = {"side": "buy", "order_id": "9", "stop_loss": 1.0850, "take_profit": 1.1300}  # 150 pips > 110
        note = mod._post_trade_max_stop_check({"symbol": "EURUSD", "connection": "c"}, order)
        assert "STOP TIGHTENED" in note and calls == [pytest.approx(1.0890)]
        assert order["stop_loss"] == pytest.approx(1.0890) and order["stop_adjusted"] is True

    def test_normal_stop_untouched(self, monkeypatch, mod) -> None:
        calls = self._setup(monkeypatch, mod)
        order = {"side": "sell", "order_id": "9", "stop_loss": 1.1015, "take_profit": 1.0970}
        assert mod._post_trade_max_stop_check({"symbol": "EURUSD", "connection": "c"}, order) == ""
        assert calls == []


@pytest.mark.parametrize("mod", [cr, fr], ids=["exness", "fundednext"])
def test_rulebook_skip_reason_benches_and_persists_review_pass(monkeypatch, mod) -> None:
    journal = [_closed("win", 30, review={"deviation": "early_exit"}),
               _closed("loss", 5, review={"deviation": "early_exit"})]
    written = []
    monkeypatch.setattr(mod, "_read_journal", lambda: journal)
    monkeypatch.setattr(mod, "_write_journal", lambda entries: written.append(entries))

    note = mod._rulebook_skip_reason("EURUSD")

    assert "[REVIEW PASS]" in note and written and written[0][-1]["review"]["pass_served"] is True
