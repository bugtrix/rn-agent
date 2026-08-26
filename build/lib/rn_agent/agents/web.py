"""Live web lookup for the interactive chat.

Search and page fetch are how a free-text prompt stays current: published
versions, docs, and error write-ups are not in the scanned tree. The only
network this module opens is public HTTPS. Localhost, private IPs and
non-https URLs are refused, so a model cannot turn a chat turn into SSRF.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..constants import APP_VERSION
from ..errors import TransportError
from ..net.http import HttpResponse, JsonTransport, default_transport

SEARCH_TIMEOUT = 20.0
MAX_QUERY = 200
MAX_RESULTS = 5
MAX_PAGE_CHARS = 10_000
USER_AGENT = f"rn-agent/{APP_VERSION} (knowledge lookup)"
DDG_HTML = "https://html.duckduckgo.com/html/"
WIKI_OPENSEARCH = "https://en.wikipedia.org/w/api.php"

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "iframe"})
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.",
        "metadata.google.internal",
        "metadata.google.internal.",
    }
)
_HREF_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|span|div)>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t]+\n|\n{3,}|[ \t]{2,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li", "br", "tr", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return _TAG_RE.sub(" ", markup)
    text = _SPACE_RE.sub("\n", "".join(parser.parts))
    return text.strip()


def public_https_url(raw: str) -> str | None:
    """A public https URL, or ``None`` when it must not be fetched."""
    text = raw.strip()
    if not text or len(text) > 2000:
        return None
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return text
    if not ip.is_global:
        return None
    return text


def unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo wraps destinations in ``//duckduckgo.com/l/?uddg=``."""
    raw = html.unescape(href).strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])
    return raw


def parse_ddg_html(markup: str) -> list[tuple[str, str, str]]:
    """``(title, url, snippet)`` from a DuckDuckGo HTML results page."""
    titles = [
        (_TAG_RE.sub("", html.unescape(match.group("title"))).strip(), unwrap_ddg_url(match.group("href")))
        for match in _HREF_RE.finditer(markup)
    ]
    snippets = [
        _TAG_RE.sub(" ", html.unescape(match.group("snippet"))).strip()
        for match in _SNIPPET_RE.finditer(markup)
    ]
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index, (title, url) in enumerate(titles):
        if not title or not url or url in seen:
            continue
        if not public_https_url(url):
            continue
        seen.add(url)
        snippet = snippets[index] if index < len(snippets) else ""
        rows.append((title, url, snippet))
        if len(rows) >= MAX_RESULTS:
            break
    return rows


def search_web(query: str, *, transport: JsonTransport | None = None) -> tuple[str, str]:
    """Search the public web. Returns ``(body, summary)``."""
    needle = " ".join(query.split())
    if not needle:
        return "No search query given.", "missing query"
    if len(needle) > MAX_QUERY:
        needle = needle[:MAX_QUERY]
    client = transport or default_transport()
    rows = _ddg_search(client, needle)
    if not rows:
        rows = _wiki_search(client, needle)
    if not rows:
        return (
            f"No web results for {needle!r}. The search host may be unreachable.",
            "0 results",
        )
    lines: list[str] = []
    for index, (title, url, snippet) in enumerate(rows, start=1):
        lines.append(f"{index}. {title}\n   {url}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines), f"{len(rows)} results"


def fetch_page(url: str, *, transport: JsonTransport | None = None) -> tuple[str, str]:
    """GET a public https page and return readable text."""
    safe = public_https_url(url)
    if safe is None:
        return (
            "Refused: only public https URLs can be fetched (no localhost or private IPs).",
            "refused",
        )
    client = transport or default_transport()
    try:
        response = _get(client, safe)
    except TransportError as error:
        return f"Could not fetch {safe}: {error.message}", "unreachable"
    if not response.ok:
        return f"HTTP {response.status} from {safe}", f"HTTP {response.status}"
    markup = response.text or ""
    stripped = markup.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            text = json.dumps(payload, indent=2, ensure_ascii=False)[:MAX_PAGE_CHARS]
            return text, f"{len(text):,} chars"
    text = html_to_text(markup)
    truncated = ""
    if len(text) > MAX_PAGE_CHARS:
        text = text[:MAX_PAGE_CHARS]
        truncated = " truncated"
    if not text:
        return f"No readable text at {safe}.", "empty"
    return text, f"{len(text):,} chars{truncated}"


def _ddg_search(client: JsonTransport, query: str) -> list[tuple[str, str, str]]:
    url = f"{DDG_HTML}?q={_quote(query)}"
    try:
        response = _get(client, url)
    except TransportError:
        return []
    if not response.ok:
        return []
    return parse_ddg_html(response.text)


def _wiki_search(client: JsonTransport, query: str) -> list[tuple[str, str, str]]:
    url = (
        f"{WIKI_OPENSEARCH}?action=opensearch&search={_quote(query)}"
        f"&limit={MAX_RESULTS}&namespace=0&format=json"
    )
    try:
        response = _get(client, url)
    except TransportError:
        return []
    if not response.ok:
        return []
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list) or len(payload) < 4:
        return []
    titles, snippets, urls = payload[1], payload[2], payload[3]
    if not isinstance(titles, list) or not isinstance(urls, list):
        return []
    extra = snippets if isinstance(snippets, list) else []
    rows: list[tuple[str, str, str]] = []
    for index, title in enumerate(titles):
        if index >= len(urls):
            break
        href = urls[index]
        if not isinstance(title, str) or not isinstance(href, str):
            continue
        if not public_https_url(href):
            continue
        note = extra[index] if index < len(extra) and isinstance(extra[index], str) else ""
        rows.append((title, href, note))
        if len(rows) >= MAX_RESULTS:
            break
    return rows


def _get(client: JsonTransport, url: str) -> HttpResponse:
    return client.request(
        "GET",
        url,
        headers={
            "user-agent": USER_AGENT,
            "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
        timeout=SEARCH_TIMEOUT,
    )


def _quote(value: str) -> str:
    return quote(value, safe="")
