"""Provider-boundary regression tests for canonical system-message shape."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.llm.client import (
    AnthropicClient,
    GeminiClient,
    LLMMessage,
    LLMRequestShapeError,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
    create_llm_client,
)


def _messages_with_legacy_system_history() -> list[LLMMessage]:
    return [
        LLMMessage(role="user", content="Earlier user turn"),
        LLMMessage(
            role="system",
            content="Static Base Prompt",
            dynamic_content="Dynamic Runtime Context",
        ),
        LLMMessage(role="system", content="Legacy onboarding instruction"),
        LLMMessage(role="user", content="Current user turn"),
    ]


def _system_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("supports_cache_control", [False, True])
def test_openai_compatible_final_payload_has_one_leading_system_message(
    stream: bool,
    supports_cache_control: bool,
) -> None:
    client = OpenAICompatibleClient(
        api_key="test",
        model="local-model",
        supports_cache_control=supports_cache_control,
    )

    payload = client._build_payload(
        _messages_with_legacy_system_history(),
        tools=None,
        temperature=0.2,
        max_tokens=1024,
        stream=stream,
    )

    system_messages = [message for message in payload["messages"] if message.get("role") == "system"]
    assert len(system_messages) == 1
    assert payload["messages"][0] is system_messages[0]
    assert payload["stream"] is stream

    system_text = _system_text(system_messages[0])
    assert system_text.index("Static Base Prompt") < system_text.index("Dynamic Runtime Context")
    assert system_text.index("Dynamic Runtime Context") < system_text.index("Legacy onboarding instruction")
    assert [_system_text(message) for message in payload["messages"] if message.get("role") == "user"] == [
        "Earlier user turn",
        "Current user turn",
    ]


@pytest.mark.parametrize("stream", [False, True])
def test_openai_responses_final_input_has_one_leading_system_message(stream: bool) -> None:
    client = OpenAIResponsesClient(api_key="test", model="responses-model")

    payload = client._build_payload(
        _messages_with_legacy_system_history(),
        tools=None,
        temperature=0.2,
        max_tokens=1024,
        stream=stream,
    )

    system_items = [item for item in payload["input"] if item.get("role") == "system"]
    assert len(system_items) == 1
    assert payload["input"][0] is system_items[0]
    assert payload["stream"] is stream
    system_text = _system_text(system_items[0])
    assert system_text.index("Static Base Prompt") < system_text.index("Dynamic Runtime Context")
    assert system_text.index("Dynamic Runtime Context") < system_text.index("Legacy onboarding instruction")


def test_native_provider_payloads_fold_later_system_records_in_order() -> None:
    messages = _messages_with_legacy_system_history()
    gemini_payload = GeminiClient(api_key="test", model="gemini-model")._build_payload(
        messages,
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )
    anthropic_payload = AnthropicClient(api_key="test", model="anthropic-model")._build_payload(
        messages,
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )

    gemini_system = gemini_payload["systemInstruction"]["parts"][0]["text"]
    anthropic_system = "\n".join(block["text"] for block in anthropic_payload["system"])
    for system_text in (gemini_system, anthropic_system):
        assert system_text.index("Static Base Prompt") < system_text.index("Dynamic Runtime Context")
        assert system_text.index("Dynamic Runtime Context") < system_text.index("Legacy onboarding instruction")

    assert all(item.get("role") != "system" for item in gemini_payload["contents"])
    assert all(item.get("role") != "system" for item in anthropic_payload["messages"])


@pytest.mark.asyncio
async def test_legacy_gemini_openai_fallback_uses_the_same_system_normalization() -> None:
    gemini = GeminiClient(
        api_key="test",
        base_url="https://example.invalid/v1beta/openai",
        model="gemini-openai-model",
    )
    fallback = await gemini._get_openai_fallback_client()

    payload = fallback._build_payload(
        _messages_with_legacy_system_history(),
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )

    assert [message.get("role") for message in payload["messages"]].count("system") == 1
    assert payload["messages"][0]["role"] == "system"
    await gemini.close()


@pytest.mark.parametrize("provider", ["ollama", "vllm", "sglang", "custom"])
def test_local_openai_compatible_providers_share_the_system_normalization(provider: str) -> None:
    client = create_llm_client(
        provider=provider,
        api_key="test",
        model="local-model",
        base_url="http://localhost.invalid/v1",
    )

    assert isinstance(client, OpenAICompatibleClient)
    payload = client._build_payload(
        _messages_with_legacy_system_history(),
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )
    assert [message.get("role") for message in payload["messages"]].count("system") == 1
    assert payload["messages"][0]["role"] == "system"


@pytest.mark.parametrize(
    "invalid_messages, expected_error",
    [
        (
            [
                {"role": "system", "content": "one"},
                {"role": "system", "content": "two"},
            ],
            "multiple system messages",
        ),
        (
            [
                {"role": "user", "content": "hello"},
                {"role": "system", "content": "late"},
            ],
            "system message must be the first item",
        ),
    ],
)
def test_openai_compatible_rejects_an_invalid_final_provider_shape(
    monkeypatch: pytest.MonkeyPatch,
    invalid_messages: list[dict[str, Any]],
    expected_error: str,
) -> None:
    client = OpenAICompatibleClient(api_key="test", model="local-model")
    monkeypatch.setattr(client, "_messages_to_openai_payload", lambda _messages: invalid_messages)

    with pytest.raises(LLMRequestShapeError, match=expected_error):
        client._build_payload(
            [LLMMessage(role="system", content="valid before conversion")],
            tools=None,
            temperature=0.2,
            max_tokens=1024,
        )


def _messages_with_cache_break() -> list[LLMMessage]:
    """Durable-runtime layout: static system, history, dynamic block, tail control."""
    return [
        LLMMessage(role="system", content="Static Base Prompt"),
        LLMMessage(role="user", content="Earlier user turn"),
        LLMMessage(role="assistant", content="Earlier assistant turn"),
        LLMMessage(role="user", content="Dynamic Runtime Context", prefix_cache_break=True),
        LLMMessage(role="user", content="Current user turn"),
    ]


def _cache_marked_texts(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and part.get("cache_control") == {"type": "ephemeral"}
    ]


def _cache_control_client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(api_key="test", model="qwen-local", supports_cache_control=True)


def test_cache_control_boundary_lands_on_history_tail_when_break_is_marked() -> None:
    payload = _cache_control_client()._build_payload(
        _messages_with_cache_break(),
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )
    messages = payload["messages"]
    # Static system block and the history tail (message before the dynamic
    # block) carry the cache boundary.
    assert _cache_marked_texts(messages[0]) == ["Static Base Prompt"]
    assert _cache_marked_texts(messages[1]) == []
    assert _cache_marked_texts(messages[2]) == ["Earlier assistant turn"]
    # The per-turn dynamic block and the final control message stay unmarked.
    assert _cache_marked_texts(messages[3]) == []
    assert _cache_marked_texts(messages[4]) == []


def test_cache_control_without_break_still_marks_last_user_message() -> None:
    messages = [
        LLMMessage(role="system", content="Static Base Prompt"),
        LLMMessage(role="user", content="Earlier user turn"),
        LLMMessage(role="user", content="Current user turn"),
    ]
    payload = _cache_control_client()._build_payload(
        messages,
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )
    assert _cache_marked_texts(payload["messages"][0]) == ["Static Base Prompt"]
    assert _cache_marked_texts(payload["messages"][1]) == []
    assert _cache_marked_texts(payload["messages"][2]) == ["Current user turn"]


def test_cache_control_break_with_empty_history_marks_only_the_system_block() -> None:
    messages = [
        LLMMessage(role="system", content="Static Base Prompt"),
        LLMMessage(role="user", content="Dynamic Runtime Context", prefix_cache_break=True),
        LLMMessage(role="user", content="Current user turn"),
    ]
    payload = _cache_control_client()._build_payload(
        messages,
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )
    assert _cache_marked_texts(payload["messages"][0]) == ["Static Base Prompt"]
    assert _cache_marked_texts(payload["messages"][1]) == []
    assert _cache_marked_texts(payload["messages"][2]) == []


def test_cache_break_flag_is_ignored_without_cache_control_support() -> None:
    client = OpenAICompatibleClient(api_key="test", model="deepseek", supports_cache_control=False)
    payload = client._build_payload(
        _messages_with_cache_break(),
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )
    messages = payload["messages"]
    assert all(_cache_marked_texts(message) == [] for message in messages)
    assert messages[3]["content"] == "Dynamic Runtime Context"
    assert messages[4]["content"] == "Current user turn"
