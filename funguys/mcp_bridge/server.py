"""Internet Access, Documentation Scraper, and Graphify Tool Bridge for AI Agents."""

from __future__ import annotations

import re
import urllib.parse
from typing import Dict, List

import httpx
from bs4 import BeautifulSoup


async def web_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Search the live internet via DuckDuckGo HTML endpoint (no paid API key required)."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    results: List[Dict[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select(".result")[:max_results]:
                title_el = item.select_one(".result__title a")
                snippet_el = item.select_one(".result__snippet")
                if title_el:
                    results.append(
                        {
                            "title": title_el.get_text(strip=True),
                            "url": str(title_el.get("href", "")),
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                        }
                    )
    except Exception as exc:
        results.append({"title": "Search Error", "url": "", "snippet": str(exc)})
    return results


async def fetch_docs_markdown(url: str, max_chars: int = 8000) -> str:
    """Fetch a public documentation or web page and extract clean readable text/markdown."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n")
            cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
            return cleaned[:max_chars]
    except Exception as exc:
        return f"Error fetching {url}: {exc}"
