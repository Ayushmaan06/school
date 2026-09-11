"""robots.txt fetch, parse and cache.

Checked before the first request to a new host. Politeness is not optional here:
S2 touches thousands of small school sites on shared hosting, and Project-Doc
6.12 was right that per-domain limits matter more than a global one.
"""

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class _Entry:
    parser: RobotFileParser | None  # None = fetch failed; treat as allow-all
    fetched_at: float
    crawl_delay: float | None = None


@dataclass
class RobotsCache:
    """Per-origin robots.txt, cached for 24h.

    A missing or unreachable robots.txt means allow - that is what the standard
    says, and [VERIFIED M0-0] it is the actual situation on saras.cbse.gov.in,
    which serves a 404 page for /robots.txt.
    """

    user_agent: str
    _cache: dict[str, _Entry] = field(default_factory=dict)

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        entry = await self._entry(client, url)
        if entry.parser is None:
            return True
        return entry.parser.can_fetch(self.user_agent, url)

    async def crawl_delay(self, client: httpx.AsyncClient, url: str) -> float | None:
        return (await self._entry(client, url)).crawl_delay

    async def _entry(self, client: httpx.AsyncClient, url: str) -> _Entry:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._cache.get(origin)
        if cached and (time.monotonic() - cached.fetched_at) < CACHE_TTL_SECONDS:
            return cached

        parser: RobotFileParser | None = None
        delay: float | None = None
        try:
            response = await client.get(f"{origin}/robots.txt", timeout=15.0)
            # A 404 HTML error page is not a robots.txt. Only parse a 2xx that
            # actually looks like one; anything else means "no rules".
            if response.status_code == 200 and "html" not in response.headers.get(
                "content-type", ""
            ):
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                raw_delay = parser.crawl_delay(self.user_agent)
                delay = float(raw_delay) if raw_delay is not None else None
        except (httpx.HTTPError, UnicodeDecodeError, ValueError):
            parser = None

        entry = _Entry(parser=parser, fetched_at=time.monotonic(), crawl_delay=delay)
        self._cache[origin] = entry
        return entry
