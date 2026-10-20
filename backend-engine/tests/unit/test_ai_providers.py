"""Provider selection and the HTTP-based AI providers (Ollama, OpenAI-compatible)."""

import json
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.services.ai_enricher import (
    SYSTEM_PROMPT,
    AIEnrichmentError,
    ClaudeEnricher,
    OllamaEnricher,
    OpenAICompatibleEnricher,
    enricher_from_settings,
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
