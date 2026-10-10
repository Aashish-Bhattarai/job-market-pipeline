"""Welcome to the Jungle (UK) discovery source.

Verified 2026-09-26: Welcome to the Jungle's job search
(``welcometothejungle.com/en/jobs``) is a client-side-rendered SPA behind
CloudFront/bot protection -- a plain HTTP GET to the search page returns
HTTP 403 Forbidden with no job data, and search results only ever populate
via client-side JS (Algolia-backed) after the page loads in a real browser.

``https://www.welcometothejungle.com/robots.txt`` also explicitly disallows
the query-based search surface this source would need:
    disallow: */jobs?query=*
    Disallow: /*?
so even a JS-capable crawler would not be permitted to crawl parameterised
job searches here. There is no public RSS/JSON API alternative.

Do not render the SPA with a headless browser or route around robots.txt.
"""
from __future__ import annotations


def fetch(config: dict) -> list[dict]:
    raise NotImplementedError(
        "Welcome to the Jungle's job search is a JS-rendered SPA that "
        "returns HTTP 403 to plain HTTP requests, and robots.txt "
        "explicitly disallows the query-based search surface "
        "(disallow: */jobs?query=*, Disallow: /*?) -- no legitimate "
        "non-JS, non-bypassing access path exists."
    )
