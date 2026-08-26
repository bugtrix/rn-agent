"""Public web search and fetch for chat - no live network."""

from __future__ import annotations

import json

from rn_agent.agents.web import (
    fetch_page,
    html_to_text,
    parse_ddg_html,
    public_https_url,
    search_web,
)
from rn_agent.net.http import HttpResponse

DDG_MARKUP = """
<html><body>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freactnative.dev%2Fblog">React Native 0.81</a>
<a class="result__snippet">The latest React Native is 0.81.1.</a>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=http%3A%2F%2F127.0.0.1%2Fsecret">Local trap</a>
<a class="result__snippet">should be skipped</a>
</body></html>
"""


class FakeTransport:
    def __init__(self, pages: dict[str, HttpResponse]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def request(self, method, url, *, headers, payload=None, timeout=30.0):
        self.calls.append(url)
        for needle, response in self.pages.items():
            if needle in url:
                return response
        return HttpResponse(status=404, body={}, text="")


def test_public_https_url_refuses_localhost_and_private_ips():
    assert public_https_url("https://reactnative.dev/blog")
    assert public_https_url("http://example.com") is None
    assert public_https_url("https://localhost/x") is None
    assert public_https_url("https://127.0.0.1/x") is None
    assert public_https_url("https://192.168.0.1/x") is None
    assert public_https_url("https://10.0.0.8/x") is None
    assert public_https_url("https://[::1]/x") is None
    assert public_https_url("https://0.0.0.0/") is None


def test_parse_ddg_html_unwraps_and_skips_private_urls():
    rows = parse_ddg_html(DDG_MARKUP)

    assert len(rows) == 1
    title, url, snippet = rows[0]
    assert title == "React Native 0.81"
    assert url == "https://reactnative.dev/blog"
    assert "0.81.1" in snippet


def test_search_web_uses_duckduckgo_results():
    transport = FakeTransport(
        {"html.duckduckgo.com": HttpResponse(status=200, body={}, text=DDG_MARKUP)}
    )

    body, summary = search_web("latest React Native version", transport=transport)

    assert "reactnative.dev/blog" in body
    assert "1 results" in summary
    assert not any("wikipedia" in url for url in transport.calls)


def test_search_web_falls_back_to_wikipedia_when_ddg_is_empty():
    wiki = json.dumps(
        [
            "React Native",
            ["React Native"],
            ["A framework for native apps."],
            ["https://en.wikipedia.org/wiki/React_Native"],
        ]
    )
    transport = FakeTransport(
        {
            "html.duckduckgo.com": HttpResponse(status=200, body={}, text="<html></html>"),
            "wikipedia.org": HttpResponse(status=200, body={}, text=wiki),
        }
    )

    body, summary = search_web("React Native", transport=transport)

    assert "en.wikipedia.org/wiki/React_Native" in body
    assert "1 results" in summary


def test_search_web_keeps_wikipedia_hits_when_snippets_are_missing():
    wiki = json.dumps(
        [
            "React Native",
            ["React Native"],
            [],
            ["https://en.wikipedia.org/wiki/React_Native"],
        ]
    )
    transport = FakeTransport(
        {
            "html.duckduckgo.com": HttpResponse(status=503, body={}, text=""),
            "wikipedia.org": HttpResponse(status=200, body={}, text=wiki),
        }
    )

    body, _ = search_web("React Native", transport=transport)

    assert "React Native" in body
    assert "en.wikipedia.org" in body


def test_fetch_page_refuses_ssrf():
    body, summary = fetch_page("https://127.0.0.1/secret", transport=FakeTransport({}))

    assert "Refused" in body
    assert summary == "refused"


def test_fetch_page_returns_readable_html():
    markup = "<html><body><h1>0.81</h1><script>alert(1)</script><p>Released.</p></body></html>"
    transport = FakeTransport(
        {"reactnative.dev": HttpResponse(status=200, body={}, text=markup)}
    )

    body, summary = fetch_page("https://reactnative.dev/blog", transport=transport)

    assert "0.81" in body
    assert "Released." in body
    assert "alert" not in body
    assert "chars" in summary


def test_fetch_page_pretty_prints_json():
    payload = {"dist-tags": {"latest": "0.81.1"}}
    transport = FakeTransport(
        {
            "registry.npmjs.org": HttpResponse(
                status=200,
                body=payload,
                text=json.dumps(payload),
            )
        }
    )

    body, _ = fetch_page("https://registry.npmjs.org/react-native", transport=transport)

    assert '"latest": "0.81.1"' in body


def test_html_to_text_drops_scripts():
    assert "secret" not in html_to_text("<p>ok</p><script>secret</script>")
    assert "ok" in html_to_text("<p>ok</p><script>secret</script>")
