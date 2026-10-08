"""D13/D14 (2026-10-08): regression guard that the regime-adaptation prompt
blocks actually landed in the head_trader system_prompt, and that the yaml
still parses. No other test in this suite touches the preset's prompt text
directly -- this is deliberately a thin content check, not a behavioral one
(an LLM's actual compliance with the prompt can't be unit-tested)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

PRESET_PATH = (
    Path(__file__).resolve().parents[2] / "agent" / "src" / "swarm" / "presets" / "fx_commodity_day_desk.yaml"
)


def _head_trader_prompt() -> str:
    data = yaml.safe_load(PRESET_PATH.read_text(encoding="utf-8"))
    agent = next(a for a in data["agents"] if a["id"] == "head_trader")
    return agent["system_prompt"]


def test_preset_still_parses_as_valid_yaml() -> None:
    data = yaml.safe_load(PRESET_PATH.read_text(encoding="utf-8"))
    assert data["name"] == "fx_commodity_day_desk"


def test_regime_adaptation_block_present() -> None:
    prompt = _head_trader_prompt()
    assert "REGIME ADAPTATION" in prompt
    assert "size_multiplier" in prompt
    assert "1.3x" in prompt
    assert "VOLATILE" in prompt and "NORMAL" in prompt


def test_regime_skeptic_block_present_before_output_format() -> None:
    prompt = _head_trader_prompt()
    assert "REGIME SKEPTIC" in prompt
    skeptic_pos = prompt.index("REGIME SKEPTIC")
    output_pos = prompt.index("Output, in this order")
    assert skeptic_pos < output_pos, "REGIME SKEPTIC must come before the OUTPUT FORMAT line"


def test_regime_skeptic_is_not_a_new_output_field() -> None:
    # D14 explicitly must not add a new output field -- the Output line's
    # own field list must be unchanged from v5.1 (still ends at REASON FOR
    # PASS, no REGIME_SKEPTIC-named field inserted).
    prompt = _head_trader_prompt()
    output_line = next(line for line in prompt.splitlines() if line.strip().startswith("Output, in this order"))
    assert "REGIME" not in output_line
