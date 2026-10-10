"""Shared helpers for discover/sources/* modules."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from html import unescape
from html.parser import HTMLParser

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_last_request_at: dict[str, float] = {}
MIN_INTERVAL = 1.0  # seconds, politeness: <=1 req/sec per host
# Hosts that punish a steady 1 req/s: Workable's Cloudflare answered "1015, Retry-After 15.8h" to a pipeline that
# probed it every round (2026-10-02), so it gets one request per 6 seconds.
HOST_INTERVAL = {"apply.workable.com": 6.0}
COOLDOWN_FILE = Path(__file__).resolve().parent.parent / "data" / "host_cooldowns.json"  # git-ignored (data/)
DEFAULT_COOLDOWN_S = 30 * 60   # a 429 with no usable Retry-After
MAX_COOLDOWN_S = 24 * 3600     # never trust a header into a multi-day blackout


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
        "h6", "tr", "table", "section", "article",
    }

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Convert an HTML fragment to plain text (collapsed whitespace)."""
    if not html:
        return ""
    if "<" not in html and "&lt;" in html:
        html = unescape(html)  # Greenhouse sends its HTML entity-escaped; parse the real markup
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return unescape(re.sub(r"<[^>]+>", " ", html)).strip()
    text = unescape("".join(parser.parts))
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


class HostCoolingDown(requests.RequestException):
    """The host answered HTTP 429 with a Retry-After; nothing is sent to it until the time it named."""


def _cooldowns() -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(COOLDOWN_FILE.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def cooldown_remaining(host: str) -> float:
    """Seconds until we may contact `host` again (0 = free)."""
    return max(0.0, _cooldowns().get(host, 0.0) - time.time())


def _retry_after_seconds(resp: requests.Response) -> float:
    raw = (resp.headers.get("Retry-After") or "").strip()
    try:
        secs = float(raw)
    except ValueError:
        try:  # HTTP-date form
            from email.utils import parsedate_to_datetime
            secs = parsedate_to_datetime(raw).timestamp() - time.time()
        except (TypeError, ValueError):
            secs = DEFAULT_COOLDOWN_S
    return min(max(secs, 60.0), MAX_COOLDOWN_S)


def note_rate_limit(host: str, resp: requests.Response) -> None:
    """Remember a 429 so every later request to the host -- this process, the next stage, tomorrow's
    batch -- waits it out instead of hammering a host that has already said stop (Workable's Cloudflare
    answered "1015 / Retry-After: 15.8h" to a pipeline that kept probing it every round, 2026-10-02)."""
    state = _cooldowns()
    state[host] = time.time() + _retry_after_seconds(resp)
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOLDOWN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, COOLDOWN_FILE)


def polite_get(url: str, host_key: str | None = None, **kwargs) -> requests.Response:
    """GET with a browser UA and a >=1s gap between requests to the same host. A host that has answered 429
    is not contacted again until its Retry-After has passed (raises HostCoolingDown without sending)."""
    key = host_key or requests.utils.urlparse(url).netloc
    remaining = cooldown_remaining(key)
    if remaining > 0:
        raise HostCoolingDown(f"{key} rate-limited us (HTTP 429); not contacting it for another "
                              f"{remaining / 3600:.1f}h (Retry-After honoured)")
    now = time.monotonic()
    wait = max(MIN_INTERVAL, HOST_INTERVAL.get(key, 0.0)) - (now - _last_request_at.get(key, 0.0))
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    headers.update(kwargs.pop("headers", {}) or {})
    resp = requests.get(url, headers=headers, timeout=kwargs.pop("timeout", 20), **kwargs)
    _last_request_at[key] = time.monotonic()
    if resp.status_code == 429:
        note_rate_limit(key, resp)
    return resp


def date_only(value: str | None) -> str | None:
    """Extract YYYY-MM-DD from an ISO-ish timestamp string."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else None
