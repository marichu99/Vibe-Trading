"""Anthropic-style prompt caching (cache_control ephemeral breakpoints).

Built 2026-09-19 after tracing a real committee run's token usage: the
system prompt + full tool-schema definitions (~75-80k tokens) were being
resent byte-for-byte on every single call in a pass, across the
orchestrator's own loop AND each investment-committee swarm sub-agent's
independent loop, with zero reuse discount. See llm.py's
_apply_prompt_caching docstring for the full design rationale.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.providers.llm import ChatOpenAIWithReasoning, _apply_prompt_caching


def _instance(model: str, vibe_provider: str | None) -> Any:
    if ChatOpenAIWithReasoning is None:
        pytest.skip("langchain-openai is not installed")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test")
    return ChatOpenAIWithReasoning(model=model, api_key="sk-test", vibe_provider=vibe_provider)


class TestApplyPromptCaching:
    """Unit tests for the payload-mutation helper itself."""

    def test_wraps_string_system_content_with_cache_control(self) -> None:
        payload = {"messages": [{"role": "system", "content": "You are a trading committee."}]}
        _apply_prompt_caching(payload)
        system_msg = payload["messages"][0]
        assert system_msg["content"] == [
            {"type": "text", "text": "You are a trading committee.", "cache_control": {"type": "ephemeral"}}
        ]

    def test_marks_last_tool_only(self) -> None:
        payload = {
            "messages": [{"role": "system", "content": "sys"}],
            "tools": [
                {"type": "function", "function": {"name": "a"}},
                {"type": "function", "function": {"name": "b"}},
            ],
        }
        _apply_prompt_caching(payload)
        assert "cache_control" not in payload["tools"][0]
        assert payload["tools"][1]["cache_control"] == {"type": "ephemeral"}
        # original tool dict must not be mutated in place (only the copy in payload)
        assert payload["tools"][1]["function"] == {"name": "b"}

    def test_no_tools_key_is_a_safe_no_op(self) -> None:
        payload = {"messages": [{"role": "system", "content": "sys"}]}
        _apply_prompt_caching(payload)  # must not raise
        assert "tools" not in payload

    def test_empty_tools_list_is_a_safe_no_op(self) -> None:
        payload = {"messages": [{"role": "system", "content": "sys"}], "tools": []}
        _apply_prompt_caching(payload)
        assert payload["tools"] == []

    def test_no_system_message_is_a_safe_no_op(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        _apply_prompt_caching(payload)
        assert payload["messages"][0]["content"] == "hi"

    def test_already_list_system_content_is_left_alone(self) -> None:
        """A system message already in content-block form (e.g. re-sent from
        a prior turn) must not be double-wrapped."""
        original = [{"type": "text", "text": "sys"}]
        payload = {"messages": [{"role": "system", "content": original}]}
        _apply_prompt_caching(payload)
        assert payload["messages"][0]["content"] is original

    def test_empty_string_system_content_is_not_wrapped(self) -> None:
        payload = {"messages": [{"role": "system", "content": ""}]}
        _apply_prompt_caching(payload)
        assert payload["messages"][0]["content"] == ""

    def test_only_the_first_system_message_is_marked(self) -> None:
        payload = {
            "messages": [
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second"},
            ]
        }
        _apply_prompt_caching(payload)
        assert isinstance(payload["messages"][0]["content"], list)
        assert payload["messages"][1]["content"] == "second"


class TestGetRequestPayloadIntegration:
    """End-to-end through ChatOpenAIWithReasoning._get_request_payload --
    confirms the capability gate actually controls whether caching fires."""

    def test_claude_via_openrouter_gets_cache_control(self) -> None:
        from langchain_core.messages import HumanMessage, SystemMessage

        instance = _instance("anthropic/claude-sonnet-5", vibe_provider="openrouter")
        history = [SystemMessage(content="You are a trading committee."), HumanMessage(content="Decide.")]

        payload = instance._get_request_payload(history)

        system_msg = next(m for m in payload["messages"] if m["role"] == "system")
        assert isinstance(system_msg["content"], list)
        assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_deepseek_via_openrouter_does_not_get_cache_control(self) -> None:
        """Same relay (openrouter), non-Claude model -- must stay untouched."""
        from langchain_core.messages import HumanMessage, SystemMessage

        instance = _instance("deepseek/deepseek-v4-pro", vibe_provider="openrouter")
        history = [SystemMessage(content="You are a trading committee."), HumanMessage(content="Decide.")]

        payload = instance._get_request_payload(history)

        system_msg = next(m for m in payload["messages"] if m["role"] == "system")
        assert system_msg["content"] == "You are a trading committee."

    def test_native_deepseek_does_not_get_cache_control(self) -> None:
        from langchain_core.messages import HumanMessage, SystemMessage

        instance = _instance("deepseek-v4-flash", vibe_provider="deepseek")
        history = [SystemMessage(content="You are a trading committee."), HumanMessage(content="Decide.")]

        payload = instance._get_request_payload(history)

        system_msg = next(m for m in payload["messages"] if m["role"] == "system")
        assert system_msg["content"] == "You are a trading committee."
