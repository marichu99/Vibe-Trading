"""Broker symbol suffix normalization for MT5 profiles (added 2026-09-29)."""
from __future__ import annotations

import types

from src.trading import service
from src.trading.profiles import profile_by_id


def test_exness_profile_appends_m() -> None:
    profile = profile_by_id("mt5-live-trade")
    assert service._apply_symbol_suffix(profile, "EURUSD") == "EURUSDm"
    assert service._apply_symbol_suffix(profile, " GBPUSD ") == "GBPUSDm"


def test_already_suffixed_symbol_unchanged() -> None:
    profile = profile_by_id("mt5-live-trade")
    assert service._apply_symbol_suffix(profile, "EURUSDm") == "EURUSDm"
    assert service._apply_symbol_suffix(profile, "XAUUSD247m") == "XAUUSD247m"


def test_fundednext_profile_has_no_suffix() -> None:
    profile = profile_by_id("mt5fn-live-trade")
    assert service._apply_symbol_suffix(profile, "EURUSD") == "EURUSD"


def test_profile_without_config_is_passthrough() -> None:
    assert service._apply_symbol_suffix(types.SimpleNamespace(config=None), "AAPL") == "AAPL"


def test_place_order_forwards_suffixed_symbol(monkeypatch) -> None:
    seen = {}

    def fake_execute_live_order(**kwargs):
        seen["intent_symbol"] = kwargs["intent"].symbol
        return {"status": "ok"}

    import src.live.sdk_order_gate as gate

    monkeypatch.setattr(gate, "execute_live_order", fake_execute_live_order)
    service.place_order("EURUSD", "mt5-live-trade", side="sell", quantity=0.01,
                        stop_loss=1.1400, take_profit=1.1300)

    assert seen["intent_symbol"] == "EURUSDm"
