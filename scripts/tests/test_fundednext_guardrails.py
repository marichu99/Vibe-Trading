"""Tests for scripts/fundednext_guardrails.py.

Every test that touches DAILY_BASELINE_PATH or fundednext_state's STATE_PATH
monkeypatches it to a tmp_path location first. No network/MT5 call is ever
made for real: get_account/get_positions, halt_flag_set/trip_halt, and
read_daily_count are all monkeypatched.
"""

from __future__ import annotations

import pytest

import fundednext_guardrails as fn_guard
import fundednext_state as fn_state

pytestmark = pytest.mark.unit


def _patch_get_account(monkeypatch, *, balance: float, equity: float | None = None) -> None:
    import src.trading.service as service

    monkeypatch.setattr(
        service, "get_account",
        lambda conn: {"account": {"balance": balance, "equity": equity if equity is not None else balance}},
    )


class TestEffectiveMaxLossUsd:
    def test_one_percent_of_balance_below_ceiling(self, monkeypatch) -> None:
        _patch_get_account(monkeypatch, balance=3000.0)
        assert fn_guard.effective_max_loss_usd("mt5fn-live-trade", mandate_ceiling=60.0) == pytest.approx(30.0)

    def test_ceiling_binds_once_balance_grows(self, monkeypatch) -> None:
        _patch_get_account(monkeypatch, balance=10000.0)
        assert fn_guard.effective_max_loss_usd("mt5fn-live-trade", mandate_ceiling=60.0) == pytest.approx(60.0)

    def test_fails_open_to_ceiling_on_read_error(self, monkeypatch) -> None:
        import src.trading.service as service

        def _boom(conn):
            raise RuntimeError("no connection")

        monkeypatch.setattr(service, "get_account", _boom)
        assert fn_guard.effective_max_loss_usd("mt5fn-live-trade", mandate_ceiling=60.0) == 60.0

    def test_fails_open_on_non_positive_balance(self, monkeypatch) -> None:
        _patch_get_account(monkeypatch, balance=0.0)
        assert fn_guard.effective_max_loss_usd("mt5fn-live-trade", mandate_ceiling=60.0) == 60.0


class TestDailyTradeCountCheck:
    def test_none_below_limit(self, monkeypatch) -> None:
        import src.live.daily_count as daily_count

        monkeypatch.setattr(daily_count, "read_daily_count", lambda broker: 3)
        assert fn_guard.daily_trade_count_check(limit=10) is None

    def test_message_at_limit(self, monkeypatch) -> None:
        import src.live.daily_count as daily_count

        monkeypatch.setattr(daily_count, "read_daily_count", lambda broker: 10)
        note = fn_guard.daily_trade_count_check(limit=10)
        assert note is not None and "10" in note

    def test_message_past_limit(self, monkeypatch) -> None:
        import src.live.daily_count as daily_count

        monkeypatch.setattr(daily_count, "read_daily_count", lambda broker: 15)
        assert fn_guard.daily_trade_count_check(limit=10) is not None


class TestDailyLossCheck:
    def test_first_check_of_day_sets_baseline_no_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_guard, "DAILY_BASELINE_PATH", tmp_path / "daily.json")
        monkeypatch.setattr(fn_state, "server_today", lambda now=None: "2026-09-12")
        assert fn_guard.daily_loss_check(equity=6000.0) is None
        assert fn_guard._read_daily_baseline() == {"date": "2026-09-12", "equity": 6000.0}

    def test_no_trip_below_threshold(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_guard, "DAILY_BASELINE_PATH", tmp_path / "daily.json")
        monkeypatch.setattr(fn_state, "server_today", lambda now=None: "2026-09-12")
        fn_guard.daily_loss_check(equity=6000.0)  # sets baseline
        # 3% drawdown -- below the 4% halt.
        assert fn_guard.daily_loss_check(equity=5820.0) is None

    def test_trips_at_threshold(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_guard, "DAILY_BASELINE_PATH", tmp_path / "daily.json")
        monkeypatch.setattr(fn_state, "server_today", lambda now=None: "2026-09-12")
        fn_guard.daily_loss_check(equity=6000.0)  # sets baseline
        # Exactly 4% drawdown.
        reason = fn_guard.daily_loss_check(equity=5760.0)
        assert reason is not None and "4" in reason

    def test_new_server_day_resets_baseline(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_guard, "DAILY_BASELINE_PATH", tmp_path / "daily.json")
        monkeypatch.setattr(fn_state, "server_today", lambda now=None: "2026-09-12")
        fn_guard.daily_loss_check(equity=6000.0)

        # A new server day rolls over even though equity is "down" vs
        # yesterday's stale baseline -- must NOT trip off the old baseline.
        monkeypatch.setattr(fn_state, "server_today", lambda now=None: "2026-09-13")
        assert fn_guard.daily_loss_check(equity=5760.0) is None
        assert fn_guard._read_daily_baseline() == {"date": "2026-09-13", "equity": 5760.0}


class TestStaticDrawdownCheck:
    def test_none_when_uninitialized(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        assert fn_guard.static_drawdown_check(equity=5000.0) is None

    def test_no_trip_below_threshold(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0})
        # 7% drawdown -- below the 8% halt.
        assert fn_guard.static_drawdown_check(equity=5580.0) is None

    def test_trips_at_threshold(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0})
        # Exactly 8% drawdown.
        reason = fn_guard.static_drawdown_check(equity=5520.0)
        assert reason is not None and "breached" in reason

    def test_never_rebaselines_even_after_profit(self, tmp_path, monkeypatch) -> None:
        """The floor is the INITIAL balance forever -- profit must not raise it."""
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0})
        assert fn_guard.static_drawdown_check(equity=6500.0) is None  # up 8.3%, fine
        # Drawdown measured from the ORIGINAL 6000 floor, not the 6500 peak.
        assert fn_guard.static_drawdown_check(equity=5580.0) is None  # 7% off 6000, still fine
        assert fn_guard.static_drawdown_check(equity=5520.0) is not None  # 8% off 6000, trips


class TestGuardrailCheck:
    def _patch_common(self, monkeypatch, tmp_path, *, halted=False, count=0, balance=6000.0, equity=6000.0):
        import src.live.daily_count as daily_count
        import src.live.halt as halt
        import src.trading.service as service

        monkeypatch.setattr(fn_guard, "DAILY_BASELINE_PATH", tmp_path / "daily.json")
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(halt, "halt_flag_set", lambda broker: halted)
        monkeypatch.setattr(daily_count, "read_daily_count", lambda broker: count)
        monkeypatch.setattr(
            service, "get_account",
            lambda conn: {"account": {"balance": balance, "equity": equity}},
        )
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": []})

    def test_already_halted_short_circuits(self, tmp_path, monkeypatch) -> None:
        import src.trading.service as service

        self._patch_common(monkeypatch, tmp_path, halted=True)

        def _boom(conn):
            raise AssertionError("get_account should not be called once already halted")

        monkeypatch.setattr(service, "get_account", _boom)
        trade = {"connection": "mt5fn-live-trade", "symbol": "EURUSDm"}
        note = fn_guard.guardrail_check(trade, {"EURUSDm"}, 20260001)
        assert note is not None and "HALTED" in note

    def test_trade_count_ceiling_short_circuits_before_account_read(self, tmp_path, monkeypatch) -> None:
        import src.trading.service as service

        self._patch_common(monkeypatch, tmp_path, count=10)

        def _boom(conn):
            raise AssertionError("get_account should not be called once the trade-count ceiling is hit")

        monkeypatch.setattr(service, "get_account", _boom)
        trade = {"connection": "mt5fn-live-trade", "symbol": "EURUSDm"}
        note = fn_guard.guardrail_check(trade, {"EURUSDm"}, 20260001)
        assert note is not None and "TRADE-COUNT" in note

    def test_clean_pass_returns_none(self, tmp_path, monkeypatch) -> None:
        self._patch_common(monkeypatch, tmp_path)
        trade = {"connection": "mt5fn-live-trade", "symbol": "EURUSDm"}
        assert fn_guard.guardrail_check(trade, {"EURUSDm"}, 20260001) is None

    def test_daily_loss_breach_trips_halt_and_flattens(self, tmp_path, monkeypatch) -> None:
        import src.live.halt as halt
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles
        import src.trading.service as service

        self._patch_common(monkeypatch, tmp_path, balance=6000.0, equity=6000.0)
        trade = {"connection": "mt5fn-live-trade", "symbol": "EURUSDm"}
        fn_guard.guardrail_check(trade, {"EURUSDm"}, 20260001)  # sets today's baseline at 6000

        tripped = {}
        monkeypatch.setattr(halt, "trip_halt", lambda by, reason, broker: tripped.update(by=by, reason=reason, broker=broker))
        monkeypatch.setattr(
            service, "get_account",
            lambda conn: {"account": {"balance": 5760.0, "equity": 5760.0}},  # 4% down
        )
        monkeypatch.setattr(
            service, "get_positions",
            lambda conn: {"positions": [{"symbol": "EURUSDm", "magic": 20260001, "ticket": "1"}]},
        )
        monkeypatch.setattr(profiles, "profile_by_id", lambda conn: type("P", (), {"config": {}})())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda cfg, overrides: {})
        closed = {}
        monkeypatch.setattr(mt5_sdk, "close_position", lambda cfg, ticket: closed.setdefault(ticket, "ok") or {"status": "ok"})

        note = fn_guard.guardrail_check(trade, {"EURUSDm"}, 20260001)
        assert note is not None and "DAILY-LOSS HALT TRIPPED" in note
        assert tripped["broker"] == "mt5fn"
        assert closed == {"1": "ok"}

    def test_static_drawdown_breach_says_challenge_likely_breached(self, tmp_path, monkeypatch) -> None:
        import src.live.halt as halt
        import src.trading.connectors.mt5.sdk as mt5_sdk
        import src.trading.profiles as profiles
        import src.trading.service as service

        self._patch_common(monkeypatch, tmp_path, balance=6000.0, equity=6000.0)
        trade = {"connection": "mt5fn-live-trade", "symbol": "EURUSDm"}
        fn_state.ensure_initialized("mt5fn-live-trade")  # initial_balance_usd = 6000

        monkeypatch.setattr(halt, "trip_halt", lambda by, reason, broker: None)
        # Equity down 8% from the initial 6000 floor, but NOT down vs
        # today's own daily baseline (also freshly set at 6000 this call) --
        # actually the daily check fires first at exactly the same threshold
        # difference, so use a value that trips static but not daily: set
        # daily baseline first at the (already dropped) 5520 equity, so the
        # daily check sees 0% same-day drawdown while static still sees 8%.
        monkeypatch.setattr(
            service, "get_account",
            lambda conn: {"account": {"balance": 5520.0, "equity": 5520.0}},
        )
        fn_guard._write_daily_baseline({"date": fn_state.server_today(), "equity": 5520.0})
        monkeypatch.setattr(service, "get_positions", lambda conn: {"positions": []})
        monkeypatch.setattr(profiles, "profile_by_id", lambda conn: type("P", (), {"config": {}})())
        monkeypatch.setattr(mt5_sdk, "build_config", lambda cfg, overrides: {})
        monkeypatch.setattr(mt5_sdk, "close_position", lambda cfg, ticket: {"status": "ok"})

        note = fn_guard.guardrail_check(trade, {"EURUSDm"}, 20260001)
        assert note is not None and "CHALLENGE LIKELY BREACHED" in note
