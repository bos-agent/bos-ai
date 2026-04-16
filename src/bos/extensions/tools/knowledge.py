import asyncio
import ssl
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

from bos.core import ep_tool


@ep_tool(
    name="WebSearch",
    description="Search the web for current information using DuckDuckGo HTML API.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
        },
        "required": ["query"],
    },
)
async def tool_web_search(query: str) -> str:
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
    try:

        def _fetch() -> bytes:
            return urllib.request.urlopen(req, context=ctx, timeout=15).read()

        html = await asyncio.to_thread(_fetch)
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for result in soup.find_all("div", class_="result"):
            title = result.find("a", class_="result__url")
            snippet = result.find("a", class_="result__snippet")
            if title and snippet:
                results.append(f"[{title.get('href')}] {title.text.strip()}\n{snippet.text.strip()}")

        return "\n\n".join(results) or "No results found."
    except Exception as e:
        return f"Error executing WebSearch: {e}"


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
