"""Regression coverage for SwarmTool natural-language preset routing."""

from __future__ import annotations

import json

import pytest

import src.tools.swarm_tool as swarm_tool


def test_explicit_preset_name_wins_over_keyword_scoring() -> None:
    prompt = (
        "[Swarm Team Mode] Use the investment_committee preset to evaluate "
        "whether to go long or short on NVDA given current market conditions"
    )

    assert swarm_tool._match_preset(prompt) == "investment_committee"


def test_plain_given_does_not_trigger_iv_derivatives_match() -> None:
    prompt = "Evaluate whether to go long or short on NVDA given current market conditions"

    assert swarm_tool._match_preset(prompt) != "derivatives_strategy_desk"


def test_explicit_preset_name_accepts_spaces() -> None:
    prompt = "Use the investment committee preset for NVDA"

    assert swarm_tool._match_preset(prompt) == "investment_committee"


def test_explicit_preset_parameter_is_normalized() -> None:
    preset, error = swarm_tool._resolve_preset(
        "Continue and finish the report.",
        explicit_preset="Investment Committee",
    )

    assert error is None
    assert preset == "investment_committee"


def test_ambiguous_continuation_does_not_fallback_to_equity_team() -> None:
    preset, error = swarm_tool._resolve_preset(
        "Continue and finish report. Continue from 'Trim 25% of position if price r'."
    )

    assert preset is None
    assert error is not None
    assert "equity_research_team" in error


def test_swarm_tool_rejects_ambiguous_continuation_before_starting_run() -> None:
    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Continue and finish report. Continue from 'Trim 25% of position if price r'."
        )
    )

    assert payload["status"] == "error"
    assert "Ambiguous continuation" in payload["error"]


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("Analyze gold XAUUSD outlook for the next quarter", "commodity"),
        ("EURUSD trend analysis and positioning", "forex"),
        ("WTI crude oil supply/demand outlook", "commodity"),
        ("Bitcoin BTC price target for Q2", "crypto"),
        ("ABC company earnings report", "A-shares"),  # no market signal -> default, not a false forex match
    ],
)
def test_extract_market_covers_forex_and_commodity(prompt: str, expected: str) -> None:
    """Gold/FX/commodity prompts must not silently default to A-shares.

    A committee run on XAUUSD used to get {market}="A-shares" baked into
    every agent's system prompt because _MARKET_PATTERNS had no forex or
    commodity entries at all.
    """
    assert swarm_tool._extract_market(prompt) == expected


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("Analyze USTEC (Nasdaq 100) index CFD outlook", "US"),
        ("Run the committee on US500 (S&P 500) for the next session", "US"),
        ("Dow Jones industrial average technical setup", "US"),
        ("Wall Street index momentum going into Jackson Hole", "US"),
    ],
)
def test_extract_market_covers_us_index_names(prompt: str, expected: str) -> None:
    """Plain-English US index names (Nasdaq/S&P/Dow/Wall Street) must not
    fall through to A-shares — the 'US' bucket previously matched only
    Chinese terms plus a literal '.US' ticker suffix, the same class of gap
    already fixed once for forex/commodity."""
    assert swarm_tool._extract_market(prompt) == expected
