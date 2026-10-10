"""HiringCafe (hiring.cafe / hiringcafe.com) discovery source.

Verified 2026-09-26: there is no public, documented API. The frontend's
internal endpoints (``POST /api/search-jobs``, ``/jobs-sitemap.xml``, and
even the plain root page) all sit behind a Cloudflare *interactive* JS
challenge ("Just a moment..." / managed challenge) that returns HTTP 403 to
a plain HTTP client and requires solving a browser challenge to pass --
this is exactly the JS-rendering/anti-bot wall this pipeline is not allowed
to bypass.

On top of that, ``https://hiringcafe.com/robots.txt`` explicitly disallows
crawling the search surface:
    Disallow: /*?searchState=*
    Disallow: /*?page=*
    Disallow: /*&page=*
    Disallow: /viewjob/
so even a challenge-solving browser would not be permitted to crawl
paginated/query-based search results here.

Do not bypass the Cloudflare challenge or route around robots.txt.
"""
from __future__ import annotations


def fetch(config: dict) -> list[dict]:
    raise NotImplementedError(
        "hiring.cafe requires solving a Cloudflare interactive JS challenge "
        "(HTTP 403 'Just a moment...' on the search API, sitemap, and "
        "general pages for a plain HTTP client), and robots.txt explicitly "
        "disallows the query/pagination-based search surface "
        "(Disallow: /*?searchState=*, /*?page=*, /viewjob/) -- no legitimate "
        "non-JS, non-bypassing access path exists."
    )
