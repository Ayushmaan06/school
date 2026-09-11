"""The single HTTP path used by every source.

Order of operations matters and is not negotiable:

    denylist  ->  robots  ->  rate limit  ->  request

The denylist runs FIRST and before the request, so a blocked URL's bytes never
exist on our disk (ADR-006, hard rule 1). A blocked or disallowed URL still
writes a `fetches` row: we record that we *declined*, which is an auditable
decision rather than a gap.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.fetch import denylist, store
from school_intel.fetch.robots import RobotsCache

TIMEOUT = 30.0
MAX_REDIRECTS = 5

# [VERIFIED M1-3] saras.cbse.gov.in serves a CAPTCHA interstitial after sustained
# request volume - HTTP 200 or 404, ~1KB, titled "Validation request". It is not
# an error page and not the content; continuing to request during one is both
# useless and impolite, and storing it would fill raw_documents with wallpaper.
INTERSTITIAL_MARKERS = (
    b"User validation required",
    b"captcha_resp",
    b"<title>Validation request</title>",
    b"Checking your browser before accessing",
    b"cf-browser-verification",
)
# Consecutive interstitials from one domain before we stop fetching it this run.
CIRCUIT_BREAKER_THRESHOLD = 3


def is_interstitial(body: bytes) -> bool:
    """A bot-check page masquerading as content."""
    head = body[:4096]
    return any(marker in head for marker in INTERSTITIAL_MARKERS)


class DomainBlockedError(RuntimeError):
    """A domain has served repeated interstitials. Stop fetching it this run."""


# Second-level suffixes common in India. Without these, "nal.kvs.ac.in" and
# "mgrly.kvs.ac.in" would be treated as one domain via a naive last-two-labels
# rule, or "kvs.ac.in" would be split as "ac.in".
_SECOND_LEVEL = frozenset({"co", "ac", "edu", "gov", "net", "org", "res", "nic", "com"})


def registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def normalize_url(url: str) -> str:
    """Percent-encode the path.

    [VERIFIED M0-0] Real Appendix-IX fee links contain spaces, e.g.
    '/pdf/md/C1.FEE STRUCTURE.pdf'. httpx raises on those rather than encoding
    them, so this is a correctness fix and not cosmetic.
    """
    p = urlsplit(url)
    return urlunsplit(
        (
            p.scheme,
            p.netloc,
            quote(p.path, safe="/%:@"),
            quote(p.query, safe="=&%?+"),
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class FetchResult:
    fetch_id: int
    content_hash: str | None
    status: int | None
    blocked_reason: str | None
    interstitial: bool = False


class _TokenBucket:
    """One request per 1/rps seconds, per registrable domain."""

    def __init__(self) -> None:
        self._next_allowed: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait(self, domain: str, rps: float) -> None:
        # The semaphore is per-domain too, so this lock only serialises the
        # arithmetic, never a whole domain's throughput.
        async with self._locks[domain]:
            now = time.monotonic()
            earliest = self._next_allowed[domain]
            if now < earliest:
                await asyncio.sleep(earliest - now)
                now = time.monotonic()
            self._next_allowed[domain] = now + (1.0 / rps if rps > 0 else 0.0)


class Fetcher:
    """Holds the httpx client, the per-domain limits, and the robots cache.

    One instance per process. Per-registrable-domain Semaphore(1) means we never
    open two concurrent connections to the same small school's shared hosting.
    """

    def __init__(self, user_agent: str, raw_root: Path) -> None:
        self.user_agent = user_agent
        self.raw_root = raw_root
        self.robots = RobotsCache(user_agent=user_agent)
        self._client: httpx.AsyncClient | None = None
        self._semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(1)
        )
        self._bucket = _TokenBucket()
        self._interstitials: dict[str, int] = defaultdict(int)

    def domain_is_blocked(self, url: str) -> bool:
        return self._interstitials[registrable_domain(url)] >= CIRCUIT_BREAKER_THRESHOLD

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=TIMEOUT,
            headers={"User-Agent": self.user_agent},
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Fetcher must be used as an async context manager")
        return self._client

    async def fetch(
        self,
        session: Session,
        url: str,
        source_id: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        url = normalize_url(url)

        # 1. Denylist, BEFORE any network activity. Hard rule 1.
        reason = denylist.blocked_reason(url, session)
        if reason is not None:
            return self._record(
                session, source_id, url, robots_allowed=True, denylist_hit=reason
            )

        # 2. Circuit breaker. Once a domain is serving bot checks, every further
        # request is wasted and rude, so stop rather than grinding through the
        # rest of the queue against a wall.
        if self.domain_is_blocked(url):
            raise DomainBlockedError(
                f"{registrable_domain(url)} has served "
                f"{CIRCUIT_BREAKER_THRESHOLD} consecutive bot checks; stopping. "
                "Lower rate_limit_rps for this source and resume later."
            )

        # 3. robots.txt for this origin.
        if not await self.robots.allowed(self.client, url):
            return self._record(session, source_id, url, robots_allowed=False)

        # 4. Rate limit, per registrable domain, honouring Crawl-delay when set.
        domain = registrable_domain(url)
        rps = _rate_limit(session, source_id)
        delay = await self.robots.crawl_delay(self.client, url)
        if delay:
            rps = min(rps, 1.0 / delay)

        async with self._semaphores[domain]:
            await self._bucket.wait(domain, rps)
            started = time.monotonic()
            try:
                response = await self.client.request(
                    method, url, data=data, headers=headers
                )
            except httpx.HTTPError as exc:
                return self._record(
                    session,
                    source_id,
                    url,
                    robots_allowed=True,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                await asyncio.sleep(min(float(retry_after), 120.0))

        media_type = (
            (response.headers.get("content-type") or "application/octet-stream")
            .split(";")[0]
            .strip()
        )
        # An interstitial is never stored: it is not this URL's content, and
        # keeping it would let a later extraction pass mistake it for one.
        if is_interstitial(response.content):
            self._interstitials[domain] += 1
            return self._record(
                session,
                source_id,
                url,
                robots_allowed=True,
                http_status=response.status_code,
                content_type=media_type,
                nbytes=len(response.content),
                elapsed_ms=elapsed_ms,
                error="interstitial: bot check served instead of content",
                interstitial=True,
            )
        self._interstitials[domain] = 0

        digest: str | None = None
        if response.status_code == 200 and response.content:
            digest, _ = store.store(
                session, self.raw_root, response.content, media_type
            )

        return self._record(
            session,
            source_id,
            url,
            robots_allowed=True,
            http_status=response.status_code,
            content_type=media_type,
            content_hash=digest,
            nbytes=len(response.content),
            elapsed_ms=elapsed_ms,
        )

    def _record(
        self,
        session: Session,
        source_id: str,
        url: str,
        *,
        robots_allowed: bool,
        denylist_hit: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        content_hash: str | None = None,
        nbytes: int | None = None,
        elapsed_ms: int | None = None,
        error: str | None = None,
        interstitial: bool = False,
    ) -> FetchResult:
        fetch_id = session.execute(
            text(
                "INSERT INTO fetches (source_id, url, http_status, content_type, error,"
                " content_hash, bytes, elapsed_ms, robots_allowed, denylist_hit)"
                " VALUES (:s, :u, :st, :ct, :err, :h, :b, :ms, :ra, :dh) RETURNING id"
            ),
            {
                "s": source_id,
                "u": url,
                "st": http_status,
                "ct": content_type,
                "err": error,
                "h": content_hash,
                "b": nbytes,
                "ms": elapsed_ms,
                "ra": robots_allowed,
                "dh": denylist_hit,
            },
        ).scalar_one()
        return FetchResult(
            fetch_id=fetch_id,
            content_hash=content_hash,
            status=http_status,
            blocked_reason=denylist_hit,
            interstitial=interstitial,
        )


def _rate_limit(session: Session, source_id: str) -> float:
    row = session.execute(
        text("SELECT rate_limit_rps FROM source_registry WHERE id = :id"),
        {"id": source_id},
    ).scalar()
    return float(row) if row is not None else 1.0
