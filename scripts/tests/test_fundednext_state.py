"""Tests for scripts/fundednext_state.py.

Every test that touches STATE_PATH monkeypatches it to a tmp_path location
first — that constant points at the real logs/ directory otherwise. No
network/MT5 call is ever made for real: src.trading.service.get_account is
monkeypatched with a fake matching the shape fundednext_state.py consumes.

The highest-priority coverage here is TestEnsureInitializedImmutability:
initial_balance_usd is the static 10%-drawdown floor's basis
(fundednext_guardrails.static_drawdown_check) — a bug that let it drift on a
second call would silently misprice the compliance floor.
"""

from __future__ import annotations

import pytest

import fundednext_state as fn_state

pytestmark = pytest.mark.unit


def _patch_get_account(monkeypatch, balance: float) -> None:
    import src.trading.service as service

    monkeypatch.setattr(service, "get_account", lambda conn: {"account": {"balance": balance, "equity": balance}})


class TestServerToday:
    def test_returns_iso_date(self) -> None:
        result = fn_state.server_today()
        assert len(result) == 10 and result.count("-") == 2

    def test_respects_passed_now(self) -> None:
        from datetime import datetime, timezone

        # A UTC noon in January is unambiguously EET (GMT+2, no DST).
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        assert fn_state.server_today(now) == "2026-01-15"


class TestEnsureInitializedImmutability:
    """initial_balance_usd must be set ONCE and never rewritten — see module docstring."""

    def test_first_call_sets_fields(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        _patch_get_account(monkeypatch, 6000.0)

        state = fn_state.ensure_initialized("mt5fn-live-trade")

        assert state["initial_balance_usd"] == 6000.0
        assert state["current_phase"] == 1
        assert state["trading_days"] == []
        assert "challenge_start_date" in state

    def test_second_call_does_not_overwrite_balance(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        _patch_get_account(monkeypatch, 6000.0)
        fn_state.ensure_initialized("mt5fn-live-trade")

        # Balance has since grown to $6,300 -- a second call must NOT treat
        # this as the new floor.
        _patch_get_account(monkeypatch, 6300.0)
        state = fn_state.ensure_initialized("mt5fn-live-trade")

        assert state["initial_balance_usd"] == 6000.0

    def test_second_call_does_not_reset_start_date(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        _patch_get_account(monkeypatch, 6000.0)
        first = fn_state.ensure_initialized("mt5fn-live-trade")

        state = fn_state.ensure_initialized("mt5fn-live-trade")
        assert state["challenge_start_date"] == first["challenge_start_date"]

    def test_missing_fields_backfilled_without_touching_existing(self, tmp_path, monkeypatch) -> None:
        """A state file that already has initial_balance_usd (e.g. from an
        older schema) but lacks trading_days must get trading_days backfilled
        without re-reading the account at all."""
        import src.trading.service as service

        path = tmp_path / "state.json"
        monkeypatch.setattr(fn_state, "STATE_PATH", path)
        fn_state._write_state({"initial_balance_usd": 6000.0, "challenge_start_date": "2026-09-01", "current_phase": 1})

        def _boom(conn):
            raise AssertionError("get_account should not be called when already initialized")

        monkeypatch.setattr(service, "get_account", _boom)
        state = fn_state.ensure_initialized("mt5fn-live-trade")
        assert state["trading_days"] == []
        assert state["initial_balance_usd"] == 6000.0


class TestRecordTradingDay:
    def test_records_new_day(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state.record_trading_day("2026-09-12")
        assert fn_state.trading_days_count() == 1

    def test_idempotent_same_day(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state.record_trading_day("2026-09-12")
        fn_state.record_trading_day("2026-09-12")
        assert fn_state.trading_days_count() == 1

    def test_accumulates_distinct_days(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state.record_trading_day("2026-09-12")
        fn_state.record_trading_day("2026-09-15")
        assert fn_state.trading_days_count() == 2

    def test_default_uses_server_today(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(fn_state, "server_today", lambda now=None: "2026-09-20")
        fn_state.record_trading_day()
        assert fn_state.get_state()["trading_days"] == ["2026-09-20"]


class TestPhaseTargetPct:
    def test_phase_1(self) -> None:
        assert fn_state.phase_target_pct(1) == 8.0

    def test_phase_2(self) -> None:
        assert fn_state.phase_target_pct(2) == 5.0

    def test_unknown_phase_falls_back_to_tighter_figure(self) -> None:
        assert fn_state.phase_target_pct(99) == 5.0


class TestProgressPct:
    def test_none_when_uninitialized(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        assert fn_state.progress_pct(6300.0) is None

    def test_computes_percent_growth(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0})
        assert fn_state.progress_pct(6300.0) == pytest.approx(5.0)

    def test_negative_progress_on_a_loss(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0})
        assert fn_state.progress_pct(5700.0) == pytest.approx(-5.0)


class TestAdvancePhase:
    def test_raises_when_uninitialized(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        with pytest.raises(RuntimeError):
            fn_state.advance_phase()

    def test_advances_1_to_2(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0, "current_phase": 1})
        assert fn_state.advance_phase() == 2
        assert fn_state.get_state()["current_phase"] == 2

    def test_refuses_past_phase_2(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fn_state, "STATE_PATH", tmp_path / "state.json")
        fn_state._write_state({"initial_balance_usd": 6000.0, "current_phase": 2})
        with pytest.raises(RuntimeError):
            fn_state.advance_phase()
