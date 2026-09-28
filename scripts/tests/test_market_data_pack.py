"""Tests for scripts/market_data_pack.py -- pure math plus the swarm plumbing
it depends on (preset grounding opt-out, variable mapping). No MT5 needed."""
from __future__ import annotations

from pathlib import Path

import pytest

import market_data_pack as mdp


def _bar(high, low, close):
    return {"high": high, "low": low, "close": close}


class TestIndicators:
    def test_ema_matches_hand_computation(self) -> None:
        # k = 2/(3+1) = 0.5: 1 -> 1.5 -> 2.25
        assert mdp.ema([1.0, 2.0, 3.0], 3) == [1.0, 1.5, 2.25]

    def test_rsi_all_gains_is_100(self) -> None:
        assert mdp.rsi([float(i) for i in range(20)]) == 100.0

    def test_rsi_needs_more_than_period_closes(self) -> None:
        assert mdp.rsi([1.0] * 14) is None

    def test_atr_uses_true_range_including_gaps(self) -> None:
        bars = [_bar(1.0, 0.9, 0.95)] + [_bar(1.2, 1.1, 1.15)] * 14  # first bar gaps up 0.25 from 0.95
        # TR[0] = max(0.1, |1.2-0.95|, |1.1-0.95|) = 0.25; the rest = max(0.1, 0.05, 0.05) = 0.1
        assert mdp.atr(bars) == pytest.approx((0.25 + 0.1 * 13) / 14)

    def test_trend_labels(self) -> None:
        assert mdp.trend_label([1.0 + i * 0.001 for i in range(80)]) == "up"
        assert mdp.trend_label([2.0 - i * 0.001 for i in range(80)]) == "down"
        assert mdp.trend_label([1.0] * 10) == "n/a"

    def test_swing_levels_find_fractal_high_and_low(self) -> None:
        bars = [_bar(1.0, 0.9, 0.95), _bar(1.1, 1.0, 1.05), _bar(1.5, 1.2, 1.3),
                _bar(1.1, 1.0, 1.05), _bar(1.0, 0.5, 0.6), _bar(1.1, 0.9, 1.0), _bar(1.2, 0.95, 1.1)]
        highs, lows = mdp.swing_levels(bars)
        assert 1.5 in highs and 0.5 in lows

    def test_synthetic_dxy_at_parity_is_the_constant(self) -> None:
        closes = {pair: [1.0, 1.0] for pair, _ in mdp._DXY_WEIGHTS}
        assert mdp.synthetic_dxy(closes) == [pytest.approx(mdp._DXY_CONSTANT)] * 2

    def test_synthetic_dxy_rises_when_eurusd_falls(self) -> None:
        closes = {pair: [1.0, 1.0] for pair, _ in mdp._DXY_WEIGHTS}
        closes["EURUSD"] = [1.10, 1.00]
        series = mdp.synthetic_dxy(closes)
        assert series[1] > series[0]


class TestSwarmInstruction:
    def test_pack_line_is_verbatim_and_first(self) -> None:
        text = mdp.swarm_instruction("fx_commodity_day_desk", "EURUSD", "forex", Path("C:/a/b.md"))
        assert 'preset_name="fx_commodity_day_desk"' in text
        lines = text.splitlines()
        i = lines.index("DATA PACK FILE: C:/a/b.md")
        assert lines[i + 1] == "EURUSD (forex)"

    def test_no_pack_omits_the_line(self) -> None:
        text = mdp.swarm_instruction("fx_commodity_day_desk", "EURUSD", "forex", None)
        assert "DATA PACK FILE" not in text and '"EURUSD (forex)"' in text


class TestWriteDataPackFailsSoft:
    def test_returns_none_instead_of_raising(self, monkeypatch, tmp_path) -> None:
        def _boom(*a, **k):
            raise RuntimeError("terminal down")
        monkeypatch.setattr(mdp, "build_data_pack", _boom)
        monkeypatch.setattr(mdp, "DATA_PACK_DIR", tmp_path)

        assert mdp.write_data_pack("EURUSDm", "mt5-live-trade") is None

    def test_writes_utf8_file_under_pack_dir(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(mdp, "build_data_pack", lambda *a, **k: "# DATA PACK — test")
        monkeypatch.setattr(mdp, "DATA_PACK_DIR", tmp_path)

        path = mdp.write_data_pack("EURUSDm", "mt5-live-trade")

        assert path.parent == tmp_path and path.read_text(encoding="utf-8") == "# DATA PACK — test"


class TestSwarmPlumbing:
    """The agent-side pieces the data pack relies on."""

    def test_fx_desk_gets_target_and_market_variables(self) -> None:
        from src.tools.swarm_tool import _build_variables

        v = _build_variables("fx_commodity_day_desk", "DATA PACK FILE: C:/a/b.md\nEURUSD (forex)")
        assert v["market"] == "forex" and v["target"].startswith("DATA PACK FILE: C:/a/b.md")

    def test_fx_desk_disables_grounding_others_keep_it(self) -> None:
        from src.swarm.presets import build_run_from_preset

        fx = build_run_from_preset("fx_commodity_day_desk", {"target": "EURUSD", "market": "forex"})
        ic = build_run_from_preset("investment_committee", {"target": "EURUSD", "market": "forex"})
        assert fx.grounding_enabled is False and ic.grounding_enabled is True

    def test_runtime_skips_prefetch_when_grounding_disabled(self, monkeypatch) -> None:
        from src.swarm import grounding
        from src.swarm.presets import build_run_from_preset
        import src.swarm.runtime as runtime

        called = []
        monkeypatch.setattr(grounding, "extract_symbols_from_user_vars", lambda uv: called.append(uv) or [])
        run = build_run_from_preset("fx_commodity_day_desk", {"target": "LIVE MARKET ATR NOW", "market": "forex"})
        runtime_cls = next(v for v in vars(runtime).values()
                           if isinstance(v, type) and hasattr(v, "_prefetch_grounding_data"))
        runtime_cls._prefetch_grounding_data(object.__new__(runtime_cls), run)

        assert called == [] and run.grounding_data is None

    def test_every_fx_desk_agent_reads_the_pack(self) -> None:
        from src.swarm.presets import load_preset

        agents = load_preset("fx_commodity_day_desk")["agents"]
        assert all("DATA PACK FILE" in a["system_prompt"] for a in agents)
        head = next(a for a in agents if a["id"] == "head_trader")
        assert "Reward:risk floor" in head["system_prompt"]


class TestExplicitPresetResolution:
    def test_fx_desk_is_accepted_by_name(self) -> None:
        # Regression 2026-09-28: rejected as "Unknown preset_name", after which the
        # wrapper retried unnamed and keyword routing picked unrelated desks.
        from src.tools.swarm_tool import _resolve_preset

        assert _resolve_preset("DATA PACK FILE: x\nEURUSD (forex)", "fx_commodity_day_desk") == ("fx_commodity_day_desk", None)

    def test_every_bundled_yaml_is_a_valid_explicit_name(self) -> None:
        from pathlib import Path
        from src.tools.swarm_tool import _PRESET_NAMES

        presets_dir = Path(mdp.AGENT_DIR) / "src" / "swarm" / "presets"
        assert {p.stem for p in presets_dir.glob("*.yaml")} <= _PRESET_NAMES

    def test_unknown_name_still_rejected(self) -> None:
        from src.tools.swarm_tool import _resolve_preset

        preset, error = _resolve_preset("x", "not_a_real_desk")
        assert preset is None and "Unknown preset_name" in error

    def test_instruction_forbids_fallback_to_another_preset(self) -> None:
        text = mdp.swarm_instruction("fx_commodity_day_desk", "EURUSD", "forex", Path("C:/a/b.md"))
        assert "do NOT retry without preset_name" in text
