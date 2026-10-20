"""Tests for _post_trade_stop_floor_check (both reporters), added 2026-09-29."""
from __future__ import annotations

import pytest

import committee_reporter as cr
import fundednext_reporter as fr


def _setup(monkeypatch, mod, *, sl, budget, floor=0.0010, lots=0.12):
    import src.trading.connectors.mt5.sdk as mt5_sdk
    import src.trading.service as service

    calls = {"modify": [], "close": []}
    monkeypatch.setattr(mod, "_resolve_fill_price", lambda trade, order: 1.1000)
    monkeypatch.setattr(mod, "_symbol_live_quote", lambda s, c: None)
    monkeypatch.setattr(mod, "_atr_stop_floor", lambda s, c: floor)
    monkeypatch.setattr(mod, "_spread_stop_floor", lambda q: None)
    monkeypatch.setattr(mod, "_mt5_config_for", lambda c: "CFG")
    monkeypatch.setattr(mt5_sdk, "contract_size", lambda symbol, config=None: 100_000)
    monkeypatch.setattr(service, "get_positions", lambda c: {"positions": [
        {"ticket": 5, "symbol": "EURUSD", "magic": mod.OUR_MAGIC, "side": "buy"}]})
    monkeypatch.setattr(mt5_sdk, "modify_position",
                        lambda config, *, ticket, stop_loss, take_profit: calls["modify"].append((ticket, stop_loss, take_profit)) or {"status": "ok"})
    monkeypatch.setattr(mt5_sdk, "close_position",
                        lambda config, *, ticket: calls["close"].append(ticket) or {"status": "ok"})
    if mod is cr:
        monkeypatch.setattr(cr, "_effective_max_loss_usd", lambda c: budget)
    else:
        monkeypatch.setattr(fr.fn_guard, "effective_max_loss_usd", lambda c: budget)
    trade = {"symbol": "EURUSD", "connection": "c", "lots": lots}
    order = {"side": "buy", "order_id": "5", "stop_loss": sl, "take_profit": 1.1030}
    return trade, order, calls


@pytest.mark.parametrize("mod", [cr, fr], ids=["exness", "fundednext"])
class TestStopFloorCheck:
    def test_tight_stop_is_widened_to_floor_within_budget(self, monkeypatch, mod) -> None:
        # 2-pip stop, 10-pip floor: 0.0010 * 100k * 0.12 = $12 risk, inside a $58 cap.
        trade, order, calls = _setup(monkeypatch, mod, sl=1.0998, budget=58.0)

        note, closed = mod._post_trade_stop_floor_check(trade, order)

        assert closed is False and "STOP WIDENED" in note
        assert calls["modify"] == [(5, pytest.approx(1.0990), 1.1030)] and calls["close"] == []
        assert order["stop_loss"] == pytest.approx(1.0990)  # the reward:risk check that runs next sees it

    def test_closed_when_floor_stop_exceeds_budget(self, monkeypatch, mod) -> None:
        trade, order, calls = _setup(monkeypatch, mod, sl=1.0998, budget=5.0)

        note, closed = mod._post_trade_stop_floor_check(trade, order)

        assert closed is True and "ORDER CLOSED" in note
        assert calls["close"] == [5] and calls["modify"] == []

    def test_stop_already_outside_floor_is_untouched(self, monkeypatch, mod) -> None:
        trade, order, calls = _setup(monkeypatch, mod, sl=1.0980, budget=58.0)

        assert mod._post_trade_stop_floor_check(trade, order) == ("", False)
        assert calls == {"modify": [], "close": []}

    def test_unknown_floor_fails_open(self, monkeypatch, mod) -> None:
        trade, order, calls = _setup(monkeypatch, mod, sl=1.0998, budget=58.0, floor=None)
        monkeypatch.setattr(mod, "_atr_stop_floor", lambda s, c: None)

        assert mod._post_trade_stop_floor_check(trade, order) == ("", False)
        assert calls == {"modify": [], "close": []}
