import pytest
from ddgs.exceptions import DDGSException

from bos.extensions.tools import knowledge


@pytest.mark.asyncio
async def test_duckduckgo_maps_sdk_results(monkeypatch):
    captured = {}

    class FakeDDGS:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

        def text(self, query, max_results=None):
            captured["query"] = query
            captured["max_results"] = max_results
            return [
                {"title": "T1", "href": "https://a.test", "body": "B1"},
                {"title": "", "href": "", "body": "no url: skipped"},
                {"title": "T3", "href": "https://c.test", "body": ""},
            ]

    monkeypatch.setattr(knowledge, "DDGS", FakeDDGS)

    results = await knowledge._search_duckduckgo("bos ai", timeout_seconds=9, max_results=3)

    assert captured == {"timeout": 9, "query": "bos ai", "max_results": 3}
    assert results == [
        knowledge.SearchResult(title="T1", url="https://a.test", snippet="B1"),
        knowledge.SearchResult(title="T3", url="https://c.test", snippet=""),
    ]


@pytest.mark.asyncio
async def test_duckduckgo_wraps_sdk_errors_as_provider_error(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, query, max_results=None):
            raise DDGSException("ratelimited")

    monkeypatch.setattr(knowledge, "DDGS", FakeDDGS)

    with pytest.raises(knowledge.WebSearchProviderError) as exc_info:
        await knowledge._search_duckduckgo("bos ai", timeout_seconds=5, max_results=3)

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.fallback_allowed is True


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

    async def fake_tavily(query: str, *, config, timeout_seconds: int, max_results: int):
        calls.append((query, dict(config), timeout_seconds, max_results))
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
        priority=["tavily", "duckduckgo"],
        timeout_seconds=7,
        max_results=3,
        tavily={"search_depth": "basic"},
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

    result = await knowledge.tool_web_search("bos ai", priority=["tavily", "duckduckgo"])

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

    result = await knowledge.tool_web_search("bos ai", priority=["tavily", "duckduckgo"])

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

    result = await knowledge.tool_web_search("bos ai", priority=["unknown", "duckduckgo"])

    assert calls == ["duckduckgo"]
    assert "[https://example.test/duck] Duck result" in result
