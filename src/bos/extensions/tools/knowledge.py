import asyncio
import json
import os
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError

from bs4 import BeautifulSoup

from bos.core import ep_tool


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class WebSearchProviderError(Exception):
    def __init__(self, provider: str, message: str, *, fallback_allowed: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.fallback_allowed = fallback_allowed


@ep_tool(
    name="WebSearch",
    description="Search the web for current information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
        },
        "required": ["query"],
    },
)
async def tool_web_search(query: str, tool_config: dict | None = None) -> str:
    config = tool_config or {}
    priority = _provider_priority(config)
    errors: list[str] = []

    for provider in priority:
        provider_config = _provider_config(config, provider)

        try:
            if provider == "duckduckgo":
                results = await _search_duckduckgo(
                    query,
                    timeout_seconds=_positive_int(config.get("timeout_seconds"), 15),
                    max_results=_positive_int(config.get("max_results"), 5),
                )
                return _format_search_results(results)

            if provider == "tavily":
                answer, results = await _search_tavily(
                    query,
                    config=provider_config,
                    timeout_seconds=_positive_int(config.get("timeout_seconds"), 15),
                    max_results=_positive_int(config.get("max_results"), 5),
                )
                return _format_search_results(results, answer=answer)

            continue
        except WebSearchProviderError as e:
            errors.append(f"{e.provider}: {e}")
            if not e.fallback_allowed:
                break
        except Exception as e:
            errors.append(f"{provider}: {e}")

    return "Error executing WebSearch: " + "; ".join(errors)


def _provider_priority(config: dict) -> list[str]:
    priority = config.get("priority")
    if not priority:
        return ["duckduckgo"]
    if isinstance(priority, str):
        return [priority]
    if isinstance(priority, list):
        return [str(provider) for provider in priority if str(provider).strip()]
    return ["duckduckgo"]


def _provider_config(config: dict, provider: str) -> dict:
    providers = config.get("providers") or {}
    provider_config = providers.get(provider) if isinstance(providers, dict) else {}
    return provider_config if isinstance(provider_config, dict) else {}


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


async def _search_duckduckgo(query: str, *, timeout_seconds: int, max_results: int) -> list[SearchResult]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"  # noqa: E501
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _fetch() -> bytes:
        return urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds).read()

    html = await asyncio.to_thread(_fetch)
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for result in soup.find_all("div", class_="result"):
        title = result.find("a", class_="result__a") or result.find("a", class_="result__url")
        snippet = result.find("a", class_="result__snippet")
        if title and snippet:
            href = title.get("href") or ""
            results.append(SearchResult(title=title.text.strip(), url=href, snippet=snippet.text.strip()))
            if len(results) >= max_results:
                break

    return results


async def _search_tavily(
    query: str,
    *,
    config: dict,
    timeout_seconds: int,
    max_results: int,
) -> tuple[str | None, list[SearchResult]]:
    api_key = config.get("api_key")
    if not api_key:
        api_key_env = str(config.get("api_key_env") or "TAVILY_API_KEY")
        api_key = os.getenv(api_key_env)
    if not api_key:
        raise WebSearchProviderError("tavily", "missing API key")

    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": config.get("search_depth", "basic"),
        "include_answer": bool(config.get("include_answer", False)),
        "include_raw_content": bool(config.get("include_raw_content", False)),
    }
    if "include_images" in config:
        payload["include_images"] = bool(config["include_images"])
    if "topic" in config:
        payload["topic"] = config["topic"]
    if "days" in config:
        payload["days"] = config["days"]
    if "include_domains" in config:
        payload["include_domains"] = config["include_domains"]
    if "exclude_domains" in config:
        payload["exclude_domains"] = config["exclude_domains"]

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "bos-ai/0.1",
        },
        method="POST",
    )

    def _fetch() -> bytes:
        try:
            return urllib.request.urlopen(req, timeout=timeout_seconds).read()
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            fallback_statuses = config.get("fallback_on_status", [429, 432, 433, 500, 502, 503, 504])
            fallback_allowed = e.code in set(fallback_statuses)
            detail = body.strip() or e.reason or "request failed"
            raise WebSearchProviderError("tavily", f"HTTP {e.code}: {detail}", fallback_allowed=fallback_allowed)

    raw = await asyncio.to_thread(_fetch)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise WebSearchProviderError("tavily", f"invalid JSON response: {e}") from e

    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise WebSearchProviderError("tavily", "malformed results")

    results = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or url).strip()
        snippet = str(item.get("content") or item.get("raw_content") or "").strip()
        if url and (title or snippet):
            results.append(SearchResult(title=title, url=url, snippet=snippet))

    answer = payload.get("answer")
    return (answer if isinstance(answer, str) and answer.strip() else None), results


def _format_search_results(results: list[SearchResult], *, answer: str | None = None) -> str:
    chunks = []
    if answer:
        chunks.append(answer.strip())
    chunks.extend(f"[{result.url}] {result.title}\n{result.snippet}" for result in results)
    return "\n\n".join(chunks) or "No results found."


@ep_tool(
    name="WebFetch",
    description="Fetch a URL and convert it into readable text.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch."},
        },
        "required": ["url"],
    },
)
async def tool_web_fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:

        def _fetch() -> bytes:
            return urllib.request.urlopen(req, context=ctx, timeout=15).read()

        html = await asyncio.to_thread(_fetch)
        soup = BeautifulSoup(html, "html.parser")

        # Remove script and style elements
        for script in soup(["script", "style", "noscript", "meta"]):
            script.extract()

        text = soup.get_text(separator="\n", strip=True)
        # Collapse multiple newlines
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)

        return text or "(No human-readable text extracted)"
    except Exception as e:
        return f"Error executing WebFetch: {e}"
