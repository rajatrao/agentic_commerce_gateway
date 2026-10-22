"""Provider selection, the prompt, and the AI providers (Claude, Ollama, OpenAI-compatible)."""

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
import respx

from app.core.config import Settings
from app.services.ai_enricher import (
    MAX_SOURCE_CHARS,
    SYSTEM_PROMPT,
    AIEnrichment,
    AIEnrichmentError,
    ClaudeEnricher,
    OllamaEnricher,
    OpenAICompatibleEnricher,
    build_user_message,
    enricher_from_settings,
    resolve_provider,
)
from app.services.attribute_extractor import TextSource
from app.services.catalog_schema import builtin_schemas

BACKPACKS = builtin_schemas()["backpacks"]
SOURCES = [TextSource("title", "Trail Pack 28"), TextSource("description", "Holds 28 L.")]
RESULT = {
    "attributes": [
        {
            "key": "capacity",
            "status": "stated",
            "value": "28 L",
            "evidence_quote": "Holds 28 L.",
            "evidence_source": "description",
            "confidence": 0.95,
            "explanation": "Stated in the description.",
        }
    ],
    "issues": [],
}


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ai_provider": "",
        "ai_model": "",
        "ai_base_url": "",
        "ai_api_key": "",
        "anthropic_api_key": "",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("overrides", "expected", "model"),
    [
        ({}, None, None),
        ({"ai_provider": "none", "anthropic_api_key": "sk-test"}, None, None),
        ({"anthropic_api_key": "sk-test"}, ClaudeEnricher, "claude-opus-5-5"),
        ({"ai_provider": "anthropic"}, None, None),
        ({"ai_provider": "ollama"}, OllamaEnricher, "qwen3:8b"),
        ({"ai_provider": "ollama", "ai_model": "qwen3:4b"}, OllamaEnricher, "qwen3:4b"),
        ({"ai_provider": "openai_compatible", "ai_model": "m"}, None, None),
        (
            {"ai_provider": "openai_compatible", "ai_base_url": "http://x/v1", "ai_model": "m"},
            OpenAICompatibleEnricher,
            "m",
        ),
    ],
)
async def test_enricher_from_settings(
    overrides: dict[str, Any], expected: type | None, model: str | None
) -> None:
    enricher = enricher_from_settings(settings(**overrides))
    if expected is None:
        assert enricher is None
        return
    assert isinstance(enricher, expected)
    assert enricher.model == model
    await enricher.aclose()


@respx.mock
async def test_ollama_sends_schema_and_parses_result() -> None:
    route = respx.post("http://ollama:11434/api/chat").respond(
        json={
            "message": {"role": "assistant", "content": json.dumps(RESULT)},
            "done_reason": "stop",
        }
    )
    enricher = OllamaEnricher(settings(ai_provider="ollama"), "http://ollama:11434/")
    result = await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()

    assert result.attributes[0].value == "28 L"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    assert body["format"]["required"] == ["attributes", "issues"]
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "<product_data>" in body["messages"][1]["content"]


@respx.mock
async def test_openai_compatible_sends_key_and_parses_result() -> None:
    route = respx.post("http://llm/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": json.dumps(RESULT)}, "finish_reason": "stop"}]}
    )
    enricher = OpenAICompatibleEnricher(
        settings(ai_provider="openai_compatible", ai_model="m", ai_api_key="secret"),
        "http://llm/v1",
    )
    result = await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()

    assert result.attributes[0].evidence_quote == "Holds 28 L."
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret"
    assert json.loads(request.content)["response_format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, json={"message": {"content": "not json"}}), "expected format"),
        (httpx.Response(200, json={"message": {"content": ""}}), "empty"),
        (
            httpx.Response(200, json={"message": {"content": "{"}, "done_reason": "length"}),
            "incomplete",
        ),
        (httpx.Response(404, json={"error": "model not found"}), "error 404"),
        (httpx.Response(429), "rate limit"),
    ],
)
@respx.mock
async def test_ollama_failures_raise_enrichment_error(
    response: httpx.Response, message: str
) -> None:
    respx.post("http://ollama:11434/api/chat").mock(return_value=response)
    enricher = OllamaEnricher(settings(ai_provider="ollama"), "http://ollama:11434")
    with pytest.raises(AIEnrichmentError, match=message):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()


@respx.mock
async def test_unreachable_provider_raises_enrichment_error() -> None:
    respx.post("http://ollama:11434/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    enricher = OllamaEnricher(settings(ai_provider="ollama"), "http://ollama:11434")
    with pytest.raises(AIEnrichmentError, match="unreachable"):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()


@respx.mock
async def test_openai_compatible_refusal_and_truncation() -> None:
    route = respx.post("http://llm/v1/chat/completions")
    enricher = OpenAICompatibleEnricher(
        settings(ai_provider="openai_compatible", ai_model="m"), "http://llm/v1"
    )
    route.respond(json={"choices": [{"message": {"refusal": "no"}, "finish_reason": "stop"}]})
    with pytest.raises(AIEnrichmentError, match="declined"):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    route.respond(json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]})
    with pytest.raises(AIEnrichmentError, match="incomplete"):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()


# --------------------------------------------------------------------------- prompt


def test_user_message_truncates_sources_and_metafields() -> None:
    sources = [
        TextSource("title", "Trail Pack"),
        TextSource("description", "x" * (MAX_SOURCE_CHARS + 500)),
        TextSource("tags", ""),  # empty sources are left out
    ]
    metafields = [{"namespace": "custom", "key": "notes", "value": "y" * 900}]
    message = build_user_message(sources, metafields, BACKPACKS)
    data = json.loads(message.split("<product_data>\n", 1)[1].split("\n</product_data>", 1)[0])
    assert set(data) == {"title", "description", "metafields"}
    assert len(data["description"]) == MAX_SOURCE_CHARS
    assert data["metafields"] == [{"field": "custom.notes", "value": "y" * 500}]

    schema = json.loads(message.split(f"Schema ({BACKPACKS.name}):\n", 1)[1])
    by_key = {a["key"]: a for a in schema}
    assert by_key["capacity"]["unit"] == "l"
    assert "allowed_values" not in by_key["capacity"]
    assert "nylon" in by_key["material"]["allowed_values"]
    assert "unit" not in by_key["material"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "none"),
        ({"anthropic_api_key": "sk-test"}, "anthropic"),
        ({"ai_provider": "ollama", "anthropic_api_key": "sk-test"}, "ollama"),
    ],
)
def test_resolve_provider(overrides: dict[str, Any], expected: str) -> None:
    assert resolve_provider(settings(**overrides)) == expected


# --------------------------------------------------------------------------- Claude


ANTHROPIC_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def api_response(status: int) -> httpx.Response:
    return httpx.Response(status, request=ANTHROPIC_REQUEST)


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeAnthropic:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def claude(result: Any, **overrides: Any) -> tuple[ClaudeEnricher, FakeAnthropic]:
    client = FakeAnthropic(result)
    enricher = ClaudeEnricher(settings(anthropic_api_key="sk-test", **overrides), client=client)
    return enricher, client


def parsed(output: AIEnrichment | None, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(parsed_output=output, stop_reason=stop_reason)


async def test_claude_sends_prompt_and_returns_parsed_output() -> None:
    expected = AIEnrichment.model_validate(RESULT)
    enricher, client = claude(parsed(expected), ai_model="claude-test", ai_effort="high")
    assert await enricher.enrich(SOURCES, [], BACKPACKS) == expected
    kwargs = client.messages.kwargs
    assert kwargs["model"] == "claude-test"
    assert kwargs["system"] == SYSTEM_PROMPT
    assert kwargs["output_format"] is AIEnrichment
    assert kwargs["output_config"] == {"effort": "high"}
    assert "<product_data>" in kwargs["messages"][0]["content"]
    await enricher.aclose()
    assert client.closed


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (parsed(None, "refusal"), "declined"),
        (parsed(None, "max_tokens"), "incomplete"),
        (parsed(None), "incomplete"),
        (anthropic.RateLimitError("slow down", response=api_response(429), body=None), "rate"),
        (
            anthropic.InternalServerError("overloaded", response=api_response(529), body=None),
            "error 529",
        ),
        (anthropic.APIConnectionError(request=ANTHROPIC_REQUEST), "unreachable"),
    ],
)
async def test_claude_failures_raise_enrichment_error(result: Any, message: str) -> None:
    enricher, _ = claude(result)
    with pytest.raises(AIEnrichmentError, match=message):
        await enricher.enrich(SOURCES, [], BACKPACKS)


# --------------------------------------------------------------------------- HTTP providers


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(429), "rate limit"),
        (httpx.Response(500, text="boom"), "error 500"),
        (httpx.Response(200, text="<html>"), "invalid JSON"),
        (httpx.Response(200, json={"choices": []}), "empty"),
        (httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]}), "format"),
    ],
)
@respx.mock
async def test_openai_compatible_failures_raise_enrichment_error(
    response: httpx.Response, message: str
) -> None:
    respx.post("http://llm/v1/chat/completions").mock(return_value=response)
    enricher = OpenAICompatibleEnricher(
        settings(ai_provider="openai_compatible", ai_model="m"), "http://llm/v1"
    )
    with pytest.raises(AIEnrichmentError, match=message):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()


@respx.mock
async def test_provider_timeout_raises_enrichment_error() -> None:
    respx.post("http://ollama:11434/api/chat").mock(side_effect=httpx.ReadTimeout("slow"))
    enricher = OllamaEnricher(settings(ai_provider="ollama"), "http://ollama:11434")
    with pytest.raises(AIEnrichmentError, match="timed out"):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    await enricher.aclose()


@respx.mock
async def test_ollama_without_a_message_is_empty_and_sends_no_auth_by_default() -> None:
    route = respx.post("http://ollama:11434/api/chat").respond(json={"done_reason": "stop"})
    enricher = OllamaEnricher(settings(ai_provider="ollama"), "http://ollama:11434")
    with pytest.raises(AIEnrichmentError, match="empty"):
        await enricher.enrich(SOURCES, [], BACKPACKS)
    assert "authorization" not in route.calls.last.request.headers
    await enricher.aclose()
