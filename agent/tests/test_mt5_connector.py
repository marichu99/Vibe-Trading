"""Tests for the MetaTrader 5 local-terminal connector.

MT5 attaches to a terminal the user already has installed and signed into —
there is no API key pair to fake, so these tests cover config resolution, the
demo/live account guard, order-sizing math, and request validation using a
minimal in-memory stand-in for the ``MetaTrader5`` module rather than the real
(Windows-only) package.
"""

from __future__ import annotations

import pytest

from src.trading import profiles, service
from src.trading.connectors.mt5 import sdk as mt5

pytestmark = pytest.mark.unit


class _NT:
    """Attribute-style stand-in for the namedtuples MetaTrader5 returns."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeMT5:
    """Minimal stand-in for the ``MetaTrader5`` module used by ``_connect``."""

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_TIME_DAY = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_INVALID_FILL = 10030
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_D1 = 16408
    TIMEFRAME_M15 = 15

    def __init__(self, *, trade_mode=0):
        self.trade_mode = trade_mode
        self.sent_requests: list[dict] = []
        self.shutdown_calls = 0

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        self.shutdown_calls += 1

    def last_error(self):
        return (0, "no error")

    def account_info(self):
        return _NT(login=555, server="Demo-Server", company="Broker", currency="USD",
                    trade_mode=self.trade_mode, balance=10000.0, equity=10000.0, margin=0.0,
                    margin_free=10000.0, margin_level=0.0, leverage=100, trade_allowed=True)

    def symbol_info(self, symbol):
        return _NT(visible=True, volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_contract_size=100000.0)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return _NT(bid=1.0995, ask=1.1000, last=1.0998, volume=10, time=1700000000)

    def positions_get(self, symbol=None, ticket=None):
        rows = (_NT(ticket=1, symbol="EURUSD", type=0, volume=0.01, price_open=1.10,
                     price_current=1.101, swap=0.0, profit=1.0, magic=1, comment="", time=1700000000),)
        if ticket is None:
            return rows
        return tuple(row for row in rows if row.ticket == ticket)

    def orders_get(self, symbol=None, ticket=None):
        if ticket == 999888:
            return (_NT(ticket=999888, symbol="EURUSD", type=0, state=1,
                         volume_current=0.01, price_open=1.10, time_setup=1700000000),)
        return ()

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        return (
            {"time": 1700000000, "open": 1.10, "high": 1.101, "low": 1.098, "close": 1.099, "tick_volume": 100},
            {"time": 1700000900, "open": 1.099, "high": 1.102, "low": 1.097, "close": 1.101, "tick_volume": 120},
        )

    def history_deals_get(self, date_from, date_to):
        return (_NT(ticket=2, order="222", symbol="EURUSD", type=0, volume=0.02, price=1.11,
                     profit=5.0, time=1700000000, magic=777, comment="signal-ea",
                     position_id=222, entry=0),)

    def order_send(self, request):
        self.sent_requests.append(request)
        if request.get("action") == self.TRADE_ACTION_REMOVE:
            return _NT(retcode=self.TRADE_RETCODE_DONE, comment="removed")
        if request.get("action") == self.TRADE_ACTION_SLTP:
            return _NT(retcode=self.TRADE_RETCODE_DONE, comment="sltp modified")
        if request.get("type_filling") == self.ORDER_FILLING_IOC:
            return _NT(retcode=self.TRADE_RETCODE_INVALID_FILL, comment="invalid fill", order=0, deal=0, volume=0, price=0)
        return _NT(retcode=self.TRADE_RETCODE_DONE, comment="done", order=999888, deal=111222,
                    volume=request["volume"], price=request["price"])


@pytest.fixture
def fake_terminal(monkeypatch):
    fake = _FakeMT5()
    monkeypatch.setattr(mt5, "_require_mt5", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# Connection retry (stale IPC handle recovery)
# --------------------------------------------------------------------------- #


def test_mt5_connect_recovers_from_one_stale_initialize_failure(fake_terminal, monkeypatch) -> None:
    """A long-lived process's connection can go stale (real incidents
    2026-09-05/07: 'Authorization failed' for hours while a brand-new
    process against the same terminal/account connected instantly) --
    shutdown() + a fresh initialize() should recover within the same call,
    with no propagated error."""
    monkeypatch.setattr(mt5.time, "sleep", lambda seconds: None)
    calls = {"n": 0}
    real_initialize = fake_terminal.initialize

    def flaky_initialize(**kwargs):
        calls["n"] += 1
        return False if calls["n"] == 1 else real_initialize(**kwargs)

    monkeypatch.setattr(fake_terminal, "initialize", flaky_initialize)

    module = mt5._connect(mt5.MT5Config(profile="paper"))

    assert module is fake_terminal
    assert calls["n"] == 2
    assert fake_terminal.shutdown_calls == 1


def test_mt5_connect_raises_when_second_initialize_also_fails(fake_terminal, monkeypatch) -> None:
    """A second consecutive failure is a real connection problem (terminal
    not running, not signed in, wrong account, ...), not staleness -- must
    still raise, not retry forever."""
    monkeypatch.setattr(mt5.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(fake_terminal, "initialize", lambda **kwargs: False)

    with pytest.raises(mt5.MT5ConnectionError):
        mt5._connect(mt5.MT5Config(profile="paper"))

    assert fake_terminal.shutdown_calls == 1


# --------------------------------------------------------------------------- #
# Profile registration
# --------------------------------------------------------------------------- #


def test_mt5_profiles_registered() -> None:
    ids = {p.id for p in profiles.list_profiles()}
    assert {"mt5-demo-trade", "mt5-live-readonly", "mt5-live-trade"} <= ids


def test_mt5_live_trade_profile_is_broker_sdk_and_tradable() -> None:
    profile = profiles.profile_by_id("mt5-live-trade")
    assert profile.connector == "mt5"
    assert profile.environment == "live"
    assert profile.transport == "broker_sdk"
    assert profile.readonly is False
    assert "orders.place" in profile.capabilities


def test_mt5_demo_profile_is_broker_sdk_and_tradable() -> None:
    profile = profiles.profile_by_id("mt5-demo-trade")
    assert profile.connector == "mt5"
    assert profile.environment == "paper"
    assert profile.transport == "broker_sdk"
    assert profile.readonly is False
    assert "orders.place" in profile.capabilities


def test_mt5_live_profile_is_readonly_and_advertises_no_placement() -> None:
    profile = profiles.profile_by_id("mt5-live-readonly")
    assert profile.environment == "live"
    assert profile.readonly is True
    assert not any(".place" in cap or "requires_mandate" in cap for cap in profile.capabilities)


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #


def test_mt5_build_config_merges_profile_then_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mt5, "get_runtime_root", lambda: tmp_path)
    cfg = mt5.build_config({"profile": "paper"}, {"account": "12345"})
    assert cfg.profile == "paper"
    assert cfg.login == 12345


def test_mt5_invalid_profile_rejected() -> None:
    with pytest.raises(ValueError):
        mt5.MT5Config.from_mapping({"profile": "live-trade-now"})


def test_mt5_no_secrets_in_config() -> None:
    """MT5 stores no broker credentials at all — the config is never redacted."""
    cfg = mt5.MT5Config(login=12345, server="Broker-Demo")
    payload = mt5.asdict(cfg)
    assert "password" not in payload
    assert "api_key" not in payload
    assert "secret_key" not in payload


# --------------------------------------------------------------------------- #
# Demo/live account guard
# --------------------------------------------------------------------------- #


def test_mt5_paper_profile_rejects_real_account(fake_terminal) -> None:
    fake_terminal.trade_mode = mt5._TRADE_MODE_REAL
    cfg = mt5.MT5Config(profile="paper")
    with pytest.raises(mt5.MT5ProfileMismatchError):
        mt5.get_account_snapshot(cfg)


def test_mt5_paper_profile_accepts_demo_account(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.get_account_snapshot(cfg)
    assert result["status"] == "ok"
    assert result["account"]["trade_mode"] == mt5._TRADE_MODE_DEMO


def test_mt5_live_readonly_tolerates_any_account(fake_terminal) -> None:
    fake_terminal.trade_mode = mt5._TRADE_MODE_REAL
    cfg = mt5.MT5Config(profile="live-readonly")
    result = mt5.get_account_snapshot(cfg)
    assert result["status"] == "ok"


def test_mt5_live_trade_profile_rejects_demo_account(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="live-trade")
    with pytest.raises(mt5.MT5ProfileMismatchError):
        mt5.get_account_snapshot(cfg)


def test_mt5_live_trade_profile_accepts_real_account(fake_terminal) -> None:
    fake_terminal.trade_mode = mt5._TRADE_MODE_REAL
    cfg = mt5.MT5Config(profile="live-trade")
    result = mt5.get_account_snapshot(cfg)
    assert result["status"] == "ok"


# --------------------------------------------------------------------------- #
# Order sizing math
# --------------------------------------------------------------------------- #


def test_mt5_resolve_volume_from_quantity_passes_through() -> None:
    info = {"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01, "trade_contract_size": 100000.0}
    assert mt5._resolve_volume(info, 0.5, None, 1.10) == 0.5


def test_mt5_resolve_volume_from_notional_uses_contract_size() -> None:
    info = {"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01, "trade_contract_size": 100000.0}
    # 11000 notional / (1.10 price * 100000 contract size) = 0.10 lots
    assert mt5._resolve_volume(info, None, 11000.0, 1.10) == pytest.approx(0.10)


def test_mt5_executions_carry_magic_and_comment(fake_terminal) -> None:
    """Needed to tell a separate signal-service EA's fills apart from ours."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.get_open_orders(cfg, include_executions=True)
    executions = result["executions"]
    assert len(executions) == 1
    assert executions[0]["magic"] == 777
    assert executions[0]["comment"] == "signal-ea"


def test_mt5_executions_carry_position_id_and_entry(fake_terminal) -> None:
    """Regression: a closing deal's `order` field is 0 in practice (no usable
    back-reference to the opening order) — only `position_id` reliably links
    a position's opening and closing deals. Without exposing `position_id`/
    `entry`, a caller matching by order_id silently grabs the OPENING deal
    (profit always 0.0) instead of the closing one and reports a real win as
    a false "breakeven" (see committee_reporter.py's trade journal bug)."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.get_open_orders(cfg, include_executions=True)
    executions = result["executions"]
    assert executions[0]["position_id"] == 222
    assert executions[0]["entry"] == 0


def test_mt5_historical_bars_range_returns_bars_in_window(fake_terminal) -> None:
    """Needed for committee_reporter.py's trade-outcome excursion analysis —
    unlike get_historical_bars (most-recent-N), this answers a specific past
    window."""
    from datetime import datetime, timezone

    cfg = mt5.MT5Config(profile="paper")
    result = mt5.get_historical_bars_range(
        "EURUSD",
        datetime(2023, 11, 14, tzinfo=timezone.utc),
        datetime(2023, 11, 15, tzinfo=timezone.utc),
        config=cfg,
        period="15m",
    )
    assert result["status"] == "ok"
    assert len(result["bars"]) == 2
    assert result["bars"][0]["high"] == 1.101


def test_mt5_contract_size_reads_symbol_info(fake_terminal) -> None:
    """The live mandate gate's notional check depends on this to scale lots correctly."""
    cfg = mt5.MT5Config(profile="paper")
    assert mt5.contract_size("EURUSD", config=cfg) == pytest.approx(100000.0)


def test_mt5_contract_size_none_when_symbol_unreadable(fake_terminal, monkeypatch) -> None:
    monkeypatch.setattr(fake_terminal, "symbol_info", lambda symbol: None)
    cfg = mt5.MT5Config(profile="paper")
    assert mt5.contract_size("BOGUS", config=cfg) is None


# --------------------------------------------------------------------------- #
# Mandate-gate asset classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "symbol,expected_asset",
    [
        ("XAUUSDm", "commodity"),
        ("XAGUSDm", "commodity"),
        ("EURUSDm", "forex"),
        ("GBPJPYm", "forex"),
        ("USTECm", "us_index"),
        ("US500m", "us_index"),
    ],
)
def test_mt5_order_classification_infers_asset_class(symbol, expected_asset) -> None:
    """The live mandate gate denies unless this correctly buckets each symbol."""
    from src.live.mandate.model import AssetClass, InstrumentType

    instrument, asset_class = service._order_classification("mt5", symbol)
    assert instrument == InstrumentType.CFD
    assert asset_class == AssetClass(expected_asset)


def test_mt5_resolve_volume_clamps_to_symbol_bounds() -> None:
    info = {"volume_min": 0.10, "volume_max": 5.0, "volume_step": 0.10, "trade_contract_size": 100000.0}
    assert mt5._resolve_volume(info, 0.01, None, 1.10) == pytest.approx(0.10)  # floored to volume_min
    assert mt5._resolve_volume(info, 50.0, None, 1.10) == pytest.approx(5.0)   # capped to volume_max


def test_mt5_resolve_volume_rejects_bad_input() -> None:
    info = {"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01, "trade_contract_size": 100000.0}
    with pytest.raises(ValueError):
        mt5._resolve_volume(info, -1.0, None, 1.10)
    with pytest.raises(ValueError):
        mt5._resolve_volume(info, None, 100.0, None)  # no price to size by notional


# --------------------------------------------------------------------------- #
# place_order / cancel_order — validation and end-to-end (mocked terminal)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs, expected_error",
    [
        ({"symbol": "", "side": "buy", "quantity": 0.01}, "symbol is required"),
        ({"symbol": "EURUSD", "side": "up", "quantity": 0.01}, "side must be"),
        ({"symbol": "EURUSD", "side": "buy", "order_type": "stop", "quantity": 0.01}, "order_type must be"),
        ({"symbol": "EURUSD", "side": "buy", "quantity": 0.01, "notional": 100.0}, "exactly one of"),
        ({"symbol": "EURUSD", "side": "buy"}, "exactly one of"),
        ({"symbol": "EURUSD", "side": "buy", "quantity": 0.01, "order_type": "limit"}, "requires limit_price"),
    ],
)
def test_mt5_place_order_validates_input(kwargs, expected_error) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, **kwargs)
    assert result["status"] == "error"
    assert expected_error in result["error"]


def test_mt5_place_order_market_buy_succeeds_with_filling_fallback(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, symbol="EURUSD", side="buy", quantity=0.01, order_type="market")
    assert result["status"] == "ok", result
    assert result["order_id"] == "999888"
    assert result["symbol"] == "EURUSD"
    # IOC was tried and rejected (invalid fill), FOK succeeded next.
    fillings_tried = [r.get("type_filling") for r in fake_terminal.sent_requests]
    assert fillings_tried[0] == fake_terminal.ORDER_FILLING_IOC
    assert fillings_tried[1] == fake_terminal.ORDER_FILLING_FOK


def test_mt5_place_order_preserves_symbol_case(fake_terminal) -> None:
    """MT5 symbol names are case-sensitive (e.g. Exness's 'XAUUSDm' suffix) —
    the connector must never coerce case, unlike a plain ticker symbol."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, symbol="XAUUSDm", side="buy", quantity=0.01, order_type="market")
    assert result["status"] == "ok", result
    assert result["symbol"] == "XAUUSDm"
    assert fake_terminal.sent_requests[-1]["symbol"] == "XAUUSDm"


# --------------------------------------------------------------------------- #
# Stop-loss / take-profit — attached to the same order (MT5 native sl/tp)
# --------------------------------------------------------------------------- #


def test_mt5_place_order_attaches_stop_loss_and_take_profit(fake_terminal) -> None:
    """A committee's stop/target levels must actually reach the broker, not be
    silently dropped at execution — buy fills at ask (1.1000 in the fake)."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(
        cfg, symbol="EURUSD", side="buy", quantity=0.01, order_type="market",
        stop_loss=1.0950, take_profit=1.1050,
    )
    assert result["status"] == "ok", result
    assert result["stop_loss"] == 1.0950
    assert result["take_profit"] == 1.1050
    sent = fake_terminal.sent_requests[-1]
    assert sent["sl"] == 1.0950
    assert sent["tp"] == 1.1050


def test_mt5_place_order_without_sl_tp_omits_them_from_request(fake_terminal) -> None:
    """No sl/tp given -> the request must not carry sl=0.0/tp=0.0, which MT5
    would otherwise (mis)interpret as an explicit zero level."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, symbol="EURUSD", side="buy", quantity=0.01, order_type="market")
    assert result["status"] == "ok", result
    assert result["stop_loss"] is None
    assert result["take_profit"] is None
    sent = fake_terminal.sent_requests[-1]
    assert "sl" not in sent
    assert "tp" not in sent


@pytest.mark.parametrize(
    "side, kwargs, expected_error",
    [
        # Buy fills at ask=1.1000: stop must be below, target above.
        ("buy", {"stop_loss": 1.1050}, "stop_loss"),
        ("buy", {"take_profit": 1.0950}, "take_profit"),
        # Sell fills at bid=1.0995: stop must be above, target below.
        ("sell", {"stop_loss": 1.0900}, "stop_loss"),
        ("sell", {"take_profit": 1.1050}, "take_profit"),
    ],
)
def test_mt5_place_order_rejects_wrong_side_sl_tp(fake_terminal, side, kwargs, expected_error) -> None:
    """A stop/target on the wrong side of entry is a real broker-rejection
    risk (and a real mistake to have missed) — fail closed before order_send."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, symbol="EURUSD", side=side, quantity=0.01, order_type="market", **kwargs)
    assert result["status"] == "error"
    assert expected_error in result["error"]


@pytest.mark.parametrize(
    "side, stop_loss, take_profit",
    [
        # Buy fills at ask=1.1000: 50-pip stop, only 20-pip target -> 0.4:1.
        ("buy", 1.0950, 1.1020),
        # Sell fills at bid=1.0995: 50-pip stop, only 20-pip target -> 0.4:1.
        ("sell", 1.1045, 1.0975),
    ],
)
def test_mt5_place_order_rejects_reward_below_risk(fake_terminal, side, stop_loss, take_profit) -> None:
    """A target closer than the stop is a losing floor even at 100% would-be
    win rate on the stop side — reject before order_send, not after the fact."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(
        cfg, symbol="EURUSD", side=side, quantity=0.01, order_type="market",
        stop_loss=stop_loss, take_profit=take_profit,
    )
    assert result["status"] == "error"
    assert "reward:risk" in result["error"]


def test_mt5_place_order_allows_exact_1to1_reward_risk(fake_terminal) -> None:
    """The floor is >= 1:1, not > 1:1 — an exact match must still be allowed."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(
        cfg, symbol="EURUSD", side="buy", quantity=0.01, order_type="market",
        stop_loss=1.0950, take_profit=1.1050,
    )
    assert result["status"] == "ok", result


def test_mt5_place_order_rejects_non_numeric_sl_tp(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, symbol="EURUSD", side="buy", quantity=0.01, stop_loss="not-a-number")
    assert result["status"] == "error"
    assert "numeric" in result["error"]


def test_mt5_place_order_rejects_on_real_account_when_paper_configured(fake_terminal) -> None:
    fake_terminal.trade_mode = mt5._TRADE_MODE_REAL
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.place_order(cfg, symbol="EURUSD", side="buy", quantity=0.01)
    assert result["status"] == "error"
    assert "demo" in result["error"].lower()


def test_mt5_cancel_order_removes_pending_order(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.cancel_order(cfg, "999888", symbol="EURUSD")
    assert result["status"] == "ok"
    assert result["cancelled"] is True


def test_mt5_cancel_order_missing_ticket_fails_closed(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.cancel_order(cfg, "111")
    assert result["status"] == "error"
    assert "no resting pending order" in result["error"]


def test_mt5_cancel_order_points_at_close_position_for_filled_tickets(fake_terminal) -> None:
    """A ticket that already filled must not be told to use a plain opposite
    order — that only closes on a netting account, not MT5's common hedging
    mode. The error should point at close_position instead."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.cancel_order(cfg, "1")  # ticket 1 is an open position, not a pending order
    assert result["status"] == "error"
    assert "close_position" in result["error"]


# --------------------------------------------------------------------------- #
# close_position — closes by ticket, safe on hedging accounts
# --------------------------------------------------------------------------- #


def test_mt5_close_position_full_volume(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.close_position(cfg, "1")
    assert result["status"] == "ok", result
    assert result["symbol"] == "EURUSD"
    assert result["side"] == "sell"  # opposite of the open buy position
    assert result["closed_volume"] == 0.01

    sent = fake_terminal.sent_requests[-1]
    assert sent["action"] == fake_terminal.TRADE_ACTION_DEAL
    assert sent["position"] == 1
    assert sent["type"] == fake_terminal.ORDER_TYPE_SELL


def test_mt5_close_position_rejects_volume_exceeding_position(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.close_position(cfg, "1", volume=10.0)
    assert result["status"] == "error"
    assert "exceeds open position volume" in result["error"]


def test_mt5_close_position_missing_ticket_fails_closed(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.close_position(cfg, "does-not-exist")
    assert result["status"] == "error"


def test_mt5_close_position_no_such_position(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.close_position(cfg, "999999")
    assert result["status"] == "error"
    assert "no open position" in result["error"]


# --------------------------------------------------------------------------- #
# modify_position — attach/change SL/TP on an already-open position
# --------------------------------------------------------------------------- #


def test_mt5_modify_position_sets_both_levels(fake_terminal) -> None:
    """Fake position (ticket 1) is a long opened at 1.10: stop below, target above."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.modify_position(cfg, "1", stop_loss=1.08, take_profit=1.15)
    assert result["status"] == "ok", result
    assert result["stop_loss"] == 1.08
    assert result["take_profit"] == 1.15
    sent = fake_terminal.sent_requests[-1]
    assert sent["action"] == fake_terminal.TRADE_ACTION_SLTP
    assert sent["position"] == 1
    assert sent["sl"] == 1.08
    assert sent["tp"] == 1.15


def test_mt5_modify_position_accepts_stop_loss_only(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.modify_position(cfg, "1", stop_loss=1.08)
    assert result["status"] == "ok", result
    assert result["stop_loss"] == 1.08


def test_mt5_modify_position_requires_at_least_one_level(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.modify_position(cfg, "1")
    assert result["status"] == "error"
    assert "at least one" in result["error"]


def test_mt5_modify_position_rejects_wrong_side_stop(fake_terminal) -> None:
    """The fake position is a long at 1.10 — a stop above entry is nonsense."""
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.modify_position(cfg, "1", stop_loss=1.20)
    assert result["status"] == "error"
    assert "stop_loss" in result["error"]


def test_mt5_modify_position_no_such_position(fake_terminal) -> None:
    cfg = mt5.MT5Config(profile="paper")
    result = mt5.modify_position(cfg, "999999", stop_loss=1.0)
    assert result["status"] == "error"
    assert "no open position" in result["error"]


# --------------------------------------------------------------------------- #
# Service dispatch degrades cleanly when the package isn't installed
# --------------------------------------------------------------------------- #


def test_service_check_connection_mt5_missing_dependency(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mt5, "get_runtime_root", lambda: tmp_path)
    # Force the "package not installed" branch explicitly rather than relying on
    # the environment lacking MetaTrader5 — a dev machine with the real package
    # (and a running terminal) installed must still exercise this path.
    monkeypatch.setattr(mt5, "mt5_available", lambda: False)
    result = service.check_connection("mt5-demo-trade")
    assert result["status"] == "error"
    assert "MetaTrader5" in result["error"]
    assert result["connector"] == "mt5"
    assert result["transport"] == "broker_sdk"


def test_service_place_order_unsupported_for_readonly_mt5_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mt5, "get_runtime_root", lambda: tmp_path)
    result = service.place_order("EURUSD", "mt5-live-readonly", side="buy", quantity=0.01)
    assert result["status"] == "error"
    assert "does not support" in result["error"]
