"""The single HTTP path used by every source.

Order of operations matters and is not negotiable:

    denylist  ->  robots  ->  rate limit  ->  request

The denylist runs FIRST and before the request, so a blocked URL's bytes never
exist on our disk (ADR-006, hard rule 1). A blocked or disallowed URL still
writes a `fetches` row: we record that we *declined*, which is an auditable
decision rather than a gap.
"""

import asyncio
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping
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
    # [VERIFIED 2026-09-15] The CURRENT Cloudflare challenge, served as HTTP 403
    # with a "Just a moment..." title. The two markers above it are the legacy
    # 2019-era wording and match none of it, so locate.cisce.org was being
    # counted as a plain 403 and the circuit breaker never fired.
    b"Just a moment...",
    b"_cf_chl_opt",
)

# Cloudflare states the mitigation in a header, which is cheaper and far more
# stable than matching body copy that changes with every redesign.
CHALLENGE_HEADER = "cf-mitigated"

# Consecutive interstitials from one domain before we stop fetching it this run.
CIRCUIT_BREAKER_THRESHOLD = 3

# Domains fetched with `curl` instead of `httpx`.
#
# [VERIFIED 2026-09-15] locate.cisce.org answers httpx with a Cloudflare
# challenge (403, `cf-mitigated: challenge`) on every request, and answers curl
# with 200 on every request - same User-Agent, same machine, same minute, with
# or without browser Accept headers, over HTTP/1.1 and HTTP/2 alike. The
# discriminator is the TLS handshake fingerprint, which scores httpx as
# automated and curl as not.
#
# This is a transport swap, NOT an impersonation. We keep announcing ourselves
# as TensorSchoolIntel with a contact address, we obey the same robots.txt,
# denylist and rate limit as every other fetch, and curl is an ordinary HTTP
# client rather than a spoofed browser. CISCE's own robots.txt allows us
# (`User-agent: *` -> `Allow: /`) and can disallow us by name at any time; the
# challenge is an automated bot score, not an access decision.
#
# Deliberately NOT here: curl_cffi / curl-impersonate TLS impersonation, which
# is real evasion, and a headless browser, which is ADR-012's rejected
# Playwright dependency by another name. If a domain needs either, drop it as a
# source instead and record why.
CURL_TRANSPORT_DOMAINS = frozenset({"cisce.org"})


def is_interstitial(body: bytes, headers: Mapping[str, str] | None = None) -> bool:
    """A bot-check page masquerading as content.

    The header is checked first: a challenge says so in `cf-mitigated` whatever
    the body happens to look like this quarter.
    """
    if headers is not None and headers.get(CHALLENGE_HEADER):
        return True
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

    async def _curl_get(
        self, url: str, headers: dict[str, str] | None
    ) -> httpx.Response:
        """One GET through curl, returned as an httpx.Response.

        Reusing httpx's own Response type means the whole pipeline downstream -
        interstitial detection, media type, storage, the `fetches` row - is
        untouched and cannot drift between the two transports.

        Called only for CURL_TRANSPORT_DOMAINS, and only AFTER the denylist,
        robots and rate-limit gates above. There is no path to this that skips
        them. Arguments are passed as a list, never a shell string.
        """
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "body"
            header_path = Path(tmp) / "headers"
            args = [
                "curl",
                "-sS",
                "--compressed",
                "--max-time",
                str(int(TIMEOUT)),
                "--location",
                "--max-redirs",
                str(MAX_REDIRECTS),
                "--user-agent",
                self.user_agent,
                "--output",
                str(body_path),
                "--dump-header",
                str(header_path),
                "--write-out",
                "%{http_code}",
            ]
            for key, value in (headers or {}).items():
                args += ["--header", f"{key}: {value}"]
            args.append(url)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:  # curl absent from the image
                raise httpx.TransportError(f"curl unavailable: {exc}") from exc
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise httpx.TransportError(
                    f"curl exit {proc.returncode}: {stderr.decode('utf-8', 'replace')[:200]}"
                )

            # With --location the header file holds one block per hop; only the
            # final response describes the bytes we actually kept.
            blocks = header_path.read_text("utf-8", errors="replace").split("\r\n\r\n")
            final = next((b for b in reversed(blocks) if b.strip()), "")
            # `--compressed` means curl hands back decoded bytes, so the origin's
            # own content-encoding and content-length no longer describe them.
            # Passing them on makes httpx.Response try to gunzip plain HTML.
            parsed = [
                (name.strip(), value.strip())
                for name, _, value in (
                    line.partition(":") for line in final.splitlines()[1:]
                )
                if name.strip()
                and name.strip().lower() not in {"content-encoding", "content-length"}
            ]
            return httpx.Response(
                status_code=int(stdout.decode("ascii", "ignore").strip() or 0),
                headers=parsed,
                content=body_path.read_bytes(),
            )

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
                # The transport swap is the LAST thing to happen, after every
                # gate above. POST still goes through httpx: no source needing
                # curl needs a POST, and one transport per verb is enough.
                if domain in CURL_TRANSPORT_DOMAINS and method == "GET":
                    response = await self._curl_get(url, headers)
                else:
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
        if is_interstitial(response.content, response.headers):
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
