import pytest

from bos.extensions.tools import knowledge


@pytest.mark.asyncio
async def test_web_search_defaults_to_duckduckgo(monkeypatch):
    calls = []

    async def fake_duckduckgo(query: str, *, timeout_seconds: int, max_results: int):
        calls.append((query, timeout_seconds, max_results))
        return [
            knowledge.SearchResult(
                title="Duck result",
                url="https://example.test/duck",
                snippet="From DuckDuckGo",
            )
        ]

    async def fake_tavily(*args, **kwargs):
        raise AssertionError("Tavily should not be called by default")

    monkeypatch.setattr(knowledge, "_search_duckduckgo", fake_duckduckgo)
    monkeypatch.setattr(knowledge, "_search_tavily", fake_tavily)

    result = await knowledge.tool_web_search("bos ai")

    assert calls == [("bos ai", 15, 5)]
    assert "[https://example.test/duck] Duck result" in result
    assert "From DuckDuckGo" in result


@pytest.mark.asyncio
async def test_web_search_uses_tavily_when_prioritized(monkeypatch):
    calls = []

    async def fake_tavily(query: str, *, config: dict, timeout_seconds: int, max_results: int):
        calls.append((query, config, timeout_seconds, max_results))
        return (
            "Direct answer",
            [
                knowledge.SearchResult(
                    title="Tavily result",
                    url="https://example.test/tavily",
                    snippet="From Tavily",
                )
            ],
        )

    async def fake_duckduckgo(*args, **kwargs):
        raise AssertionError("DuckDuckGo should not be called after Tavily succeeds")

    monkeypatch.setattr(knowledge, "_search_tavily", fake_tavily)
    monkeypatch.setattr(knowledge, "_search_duckduckgo", fake_duckduckgo)

    result = await knowledge.tool_web_search(
        "bos ai",
        {
            "priority": ["tavily", "duckduckgo"],
            "timeout_seconds": 7,
            "max_results": 3,
            "providers": {"tavily": {"search_depth": "basic"}},
        },
    )

    assert calls == [("bos ai", {"search_depth": "basic"}, 7, 3)]
    assert result.startswith("Direct answer")
    assert "[https://example.test/tavily] Tavily result" in result


@pytest.mark.asyncio
async def test_web_search_falls_back_after_tavily_fallback_error(monkeypatch):
    calls = []

    async def fake_tavily(*args, **kwargs):
        calls.append("tavily")
        raise knowledge.WebSearchProviderError("tavily", "HTTP 429: credit exhausted", fallback_allowed=True)

    async def fake_duckduckgo(query: str, *, timeout_seconds: int, max_results: int):
        calls.append("duckduckgo")
        return [
            knowledge.SearchResult(
                title="Duck fallback",
                url="https://example.test/fallback",
                snippet="Fallback result",
            )
        ]

    monkeypatch.setattr(knowledge, "_search_tavily", fake_tavily)
    monkeypatch.setattr(knowledge, "_search_duckduckgo", fake_duckduckgo)

    result = await knowledge.tool_web_search("bos ai", {"priority": ["tavily", "duckduckgo"]})

    assert calls == ["tavily", "duckduckgo"]
    assert "[https://example.test/fallback] Duck fallback" in result


@pytest.mark.asyncio
async def test_web_search_stops_after_non_fallback_tavily_error(monkeypatch):
    calls = []

    async def fake_tavily(*args, **kwargs):
        calls.append("tavily")
        raise knowledge.WebSearchProviderError("tavily", "HTTP 401: unauthorized", fallback_allowed=False)

    async def fake_duckduckgo(*args, **kwargs):
        calls.append("duckduckgo")
        return []

    monkeypatch.setattr(knowledge, "_search_tavily", fake_tavily)
    monkeypatch.setattr(knowledge, "_search_duckduckgo", fake_duckduckgo)

    result = await knowledge.tool_web_search("bos ai", {"priority": ["tavily", "duckduckgo"]})

    assert calls == ["tavily"]
    assert result == "Error executing WebSearch: tavily: HTTP 401: unauthorized"


@pytest.mark.asyncio
async def test_web_search_skips_unsupported_priority_entries(monkeypatch):
    calls = []

    async def fake_duckduckgo(query: str, *, timeout_seconds: int, max_results: int):
        calls.append("duckduckgo")
        return [
            knowledge.SearchResult(
                title="Duck result",
                url="https://example.test/duck",
                snippet="Supported fallback",
            )
        ]

    monkeypatch.setattr(knowledge, "_search_duckduckgo", fake_duckduckgo)

    result = await knowledge.tool_web_search("bos ai", {"priority": ["unknown", "duckduckgo"]})

    assert calls == ["duckduckgo"]
    assert "[https://example.test/duck] Duck result" in result
