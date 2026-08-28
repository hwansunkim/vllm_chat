from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ... import config
from ..schemas import SearchResult

logger = logging.getLogger(__name__)

# DuckDuckGo 는 명백한 봇(python-httpx 기본 UA 등)을 차단한다. 평범한 브라우저 UA 필요.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"


def _unwrap_ddg_url(href: str) -> str:
    """`//duckduckgo.com/l/?uddg=<encoded>&rut=...` 리다이렉트 URL 을 실제 목적지로 언랩."""
    if not href:
        return href
    normalized = href
    if normalized.startswith("//"):
        normalized = "https:" + normalized
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return href
    if parsed.path.startswith("/l/") and "duckduckgo.com" in (parsed.netloc or ""):
        target = parse_qs(parsed.query).get("uddg", [])
        if target:
            return unquote(target[0])
    return normalized if href.startswith("//") else href


class _DDGResultParser(HTMLParser):
    """DuckDuckGo HTML 엔드포인트 결과 파서 (stdlib html.parser 기반).

    결과 구조: <a class="result__a" href="...">제목</a> 뒤에
               <a class="result__snippet">스니펫</a> (또는 class 를 가진 다른 태그).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._mode: str | None = None  # "title" | "snippet" | None
        self._title_buf: list[str] = []
        self._pending_href: str = ""

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        attr_map = {name: value for name, value in attrs}
        if tag == "a" and "result__a" in classes:
            self._mode = "title"
            self._title_buf = []
            self._pending_href = attr_map.get("href") or ""
        elif "result__snippet" in classes:
            self._mode = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if self._mode == "title" and tag == "a":
            title = "".join(self._title_buf).strip()
            if title and self._pending_href:
                self.results.append({
                    "title": title,
                    "url": _unwrap_ddg_url(self._pending_href),
                    "snippet": "",
                })
            self._mode = None
        elif self._mode == "snippet" and tag in ("a", "td", "div", "span"):
            self._mode = None

    def handle_data(self, data: str) -> None:
        if self._mode == "title":
            self._title_buf.append(data)
        elif self._mode == "snippet" and self.results:
            self.results[-1]["snippet"] += data


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class DuckDuckGoProvider:
    """DuckDuckGo HTML 엔드포인트를 파싱하는 검색 provider. API 키 불필요."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=config.WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko,en-US;q=0.7,en;q=0.3",
            },
        )

    async def search(self, query: str, k: int) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        resp = await self._client.post(
            _HTML_ENDPOINT, data={"q": query, "kl": "wt-wt"}
        )
        resp.raise_for_status()
        parser = _DDGResultParser()
        parser.feed(resp.text)

        results: list[SearchResult] = []
        seen: set[str] = set()
        max_chars = config.WEB_SEARCH_MAX_SNIPPET_CHARS
        for item in parser.results:
            url = item["url"]
            if not url or not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(
                title=_truncate(item["title"], 300),
                url=url,
                snippet=_truncate(item["snippet"], max_chars),
            ))
            if len(results) >= k:
                break

        if not results and parser.results:
            logger.warning("DuckDuckGo: parsed %d raw rows but none usable", len(parser.results))
        return results

    async def close(self) -> None:
        await self._client.aclose()
