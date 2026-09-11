"""S2 - Mandatory Public Disclosure discovery. The only source of fee data.

[VERIFIED M0-0] The chain is THREE hops, not two:

    homepage  ->  MPD index page  ->  the FEE STRUCTURE row's linked document

The fee does not appear on the MPD page. Section "C: RESULT AND ACADEMICS"
renders as label/link pairs, and row 1 is
"FEE STRUCTURE OF THE SCHOOL | View -> .../Fee-Structure-2024-25.pdf".
A discovery step that stops at the index measures 0% fee coverage and looks
like a parser bug when it is not.

Homepage FIRST, guessed paths second. Schools link the page from their nav under
a dozen slugs (/mandatory-discloser, /mandatory_public_disclosure.php,
/disclosure/b1.pdf), so harvesting anchors beats guessing. Match an anchor on
EITHER its href or its tag-stripped link text - matching only the text between
`>` and `<` misses every site that wraps nav labels in a <span>.

Measured on 140 Bengaluru schools: 81% reachable, 45% MPD found.
"""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from rapidfuzz import fuzz
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

SOURCE_ID = "cbse_mpd"

# Cap per institution, across ALL THREE hops. Budget it deliberately: a run that
# spends six fetches guessing paths never reaches the fee document.
MAX_FETCHES = 6

CANDIDATE_PATHS = (
    "/mandatory-disclosure",
    "/mandatory-public-disclosure",
    "/public-disclosure",
    "/cbse/mandatory-disclosure",
    "/disclosure",
    "/mandatory_disclosure",
    "/appendix-ix",
)

DISCLOSURE = re.compile(
    r"mandator|disclosur|appendix.?ix|public.?disclos", re.IGNORECASE
)

# Labels that identify the fee row. The anchor text is generic ("View",
# "Click here to view"), so the ROW LABEL is what identifies the link.
FEE_ROW_LABEL = re.compile(
    r"fee\s*structure|fees\s*structure|fee\s*details|school\s*fee|fee\s*particulars",
    re.IGNORECASE,
)

# A page is an MPD index if it mentions at least two of these.
MARKERS = (
    re.compile(r"fee", re.IGNORECASE),
    re.compile(
        r"student\s*strength|class\s*wise|no\.?\s*of\s*students|enrol", re.IGNORECASE
    ),
    re.compile(r"affiliation", re.IGNORECASE),
    re.compile(r"trust|society", re.IGNORECASE),
)
MIN_MARKERS = 2

# Parent-portal / login walls: detect and abandon rather than looping.
LOGIN_WALL = re.compile(
    r"(?:parent|student|staff)\s*(?:login|portal)|sign\s*in|username.{0,40}password",
    re.IGNORECASE,
)

# A wrong-school page on shared hosting must not be accepted.
NAME_MATCH_FLOOR = 60


@dataclass
class DiscoveryPlan:
    """What to fetch next, and what has been ruled out. Pure - no I/O here."""

    institution_id: int
    canonical_name: str
    website: str | None
    fetches_used: int = 0
    mpd_url: str | None = None
    fee_url: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def budget_left(self) -> int:
        return max(0, MAX_FETCHES - self.fetches_used)


def homepage_url(website: str | None) -> str | None:
    if not website:
        return None
    site = website.strip()
    # ponytail: 17 rows carry "http:host" (no slashes); httpx then reads the
    # host as a port and every chunk re-fails them. Drop the broken scheme.
    site = re.sub(r"^https?:(?!//)", "", site, flags=re.I)
    if not site.lower().startswith(("http://", "https://")):
        site = "https://" + site
    return site.rstrip("/")


def disclosure_links(html: str, base_url: str) -> list[str]:
    """Anchors that look like a disclosure page, best candidates first.

    Matches href OR tag-stripped link text. A directly linked PDF sorts first:
    [VERIFIED M0-0] some schools publish the whole proforma as one PDF
    (/disclosure/b1.pdf), which is the shortest path to the data.
    """
    tree = HTMLParser(html)
    found: list[str] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        label = node.text(separator=" ", strip=True)
        if not (DISCLOSURE.search(href) or DISCLOSURE.search(label)):
            continue
        url = urljoin(base_url + "/", href)
        if url.startswith(("http://", "https://")) and url not in found:
            found.append(url)
    found.sort(key=lambda u: (not u.lower().endswith(".pdf"), len(u)))
    return found


def looks_like_mpd(text: str) -> bool:
    return sum(1 for marker in MARKERS if marker.search(text)) >= MIN_MARKERS


def is_login_wall(text: str) -> bool:
    """Some sites redirect the whole disclosure section behind a parent portal."""
    return bool(LOGIN_WALL.search(text)) and not looks_like_mpd(text)


def name_matches(page_text: str, canonical_name: str) -> bool:
    """Guard against shared hosting serving another school's page.

    token_set_ratio, so "Green Valley School" matches a page titled
    "Green Valley Public School, Bengaluru" without being fooled by word order.
    """
    if not canonical_name:
        return True
    head = page_text[:4000]
    return (
        fuzz.token_set_ratio(canonical_name.lower(), head.lower()) >= NAME_MATCH_FLOOR
    )


def fee_document_url(html: str, base_url: str) -> str | None:
    """The href in the same proforma row as a FEE STRUCTURE label.

    Row-scoped, because the anchor text is generic. Falls back to matching the
    href or link text directly for schools that use a list rather than a table.
    """
    tree = HTMLParser(html)

    for row in tree.css("tr"):
        row_text = row.text(separator=" ", strip=True)
        if not FEE_ROW_LABEL.search(row_text):
            continue
        for anchor in row.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                return urljoin(base_url, href)

    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        label = anchor.text(separator=" ", strip=True)
        if (FEE_ROW_LABEL.search(href) or FEE_ROW_LABEL.search(label)) and (
            href and not href.startswith(("#", "javascript:", "mailto:"))
        ):
            return urljoin(base_url, href)
    return None


def candidate_paths(base_url: str, budget: int) -> list[str]:
    """Guessed paths, trimmed to the remaining budget.

    Reserve one fetch for the fee document - reaching an index page and then
    having no budget left to follow its fee link is the worst outcome, since it
    costs the fetches and yields no fee.
    """
    usable = max(0, budget - 1)
    return [base_url + path for path in CANDIDATE_PATHS[:usable]]


# ---------------------------------------------------------------------------
# The driver. Walks the three hops inside one fetch budget.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    institution_id: int
    mpd_url: str | None
    mpd_hash: str | None
    fee_url: str | None
    fee_hash: str | None
    fetches: int
    outcome: str  # found_fee | found_index | login_wall | no_website |
    #                not_found | dead_origin
    # The homepage, whatever the MPD outcome. Most schools have no MPD page at
    # all, but their homepage footer carries the phone and email the BD team
    # actually calls - and it is already fetched and stored by hop 1, so mining
    # it costs nothing. Without this the live run threw those contacts away and
    # only a later `extract --reextract` recovered them, and that path can only
    # attribute a document whose URL matches exactly one school.
    home_hash: str | None = None


async def discover(
    fetcher, session, *, institution_id: int, canonical_name: str, website: str | None
) -> DiscoveryResult:
    """homepage -> MPD index -> fee document, within MAX_FETCHES."""
    from school_intel.fetch.store import load

    base = homepage_url(website)
    if base is None:
        return DiscoveryResult(institution_id, None, None, None, None, 0, "no_website")

    used = 0

    # Hop 1 - homepage, for its nav.
    home = await fetcher.fetch(session, base, SOURCE_ID)
    used += 1
    home_html = ""
    if home.content_hash:
        body = load(session, home.content_hash) or b""
        home_html = body.decode("utf-8", "replace")

    # A dead ORIGIN has nothing behind it, so stop after hop 1. [VERIFIED M5]
    # gipsdabra.com cost six requests and roughly three minutes - the homepage
    # and all five guessed paths returned Cloudflare 522 at the 30s timeout -
    # and could never have yielded anything. Most schools end in `not_found`
    # and a good share of those are dead domains, so this was the single
    # biggest waste of a national pass.
    #
    # A transport error or a 5xx means the server is not answering. A 404 or a
    # 403 does NOT: the server is alive and /mandatory-disclosure may well
    # exist even where the root does not, so those still try the guessed paths.
    if home.status is None or home.status >= 500:
        return DiscoveryResult(
            institution_id, None, None, None, None, used, "dead_origin"
        )

    # Hop 2 - anchors from the homepage, then guessed paths.
    targets = disclosure_links(home_html, base) if home_html else []
    targets += [
        p for p in candidate_paths(base, MAX_FETCHES - used) if p not in targets
    ]

    index_url = index_hash = None
    for url in targets:
        if used >= MAX_FETCHES - 1:  # keep one fetch for the fee document
            break
        result = await fetcher.fetch(session, url, SOURCE_ID)
        used += 1
        if result.blocked_reason or result.status != 200 or not result.content_hash:
            continue

        body = load(session, result.content_hash) or b""
        if body[:5] == b"%PDF-":
            # The whole proforma as one PDF - M2-2 reads fee and strength from it.
            return DiscoveryResult(
                institution_id,
                url,
                result.content_hash,
                url,
                result.content_hash,
                used,
                "found_fee",
                home_hash=home.content_hash,
            )

        page_html = body.decode("utf-8", "replace")
        page_text = HTMLParser(page_html).text(separator=" ")
        if is_login_wall(page_text):
            return DiscoveryResult(
                institution_id, None, None, None, None, used, "login_wall",
                home_hash=home.content_hash,
            )
        if not looks_like_mpd(page_text):
            continue
        if not name_matches(page_text, canonical_name):
            log.info("%s: %s belongs to another school; skipping", canonical_name, url)
            continue

        index_url, index_hash, index_html = url, result.content_hash, page_html
        break

    if index_url is None:
        return DiscoveryResult(
            institution_id, None, None, None, None, used, "not_found",
            home_hash=home.content_hash,
        )

    # Hop 3 - the fee document named by the FEE STRUCTURE row.
    fee_url = fee_document_url(index_html, index_url)
    if fee_url is None or used >= MAX_FETCHES:
        return DiscoveryResult(
            institution_id, index_url, index_hash, None, None, used, "found_index",
            home_hash=home.content_hash,
        )

    fee = await fetcher.fetch(session, fee_url, SOURCE_ID)
    used += 1
    if fee.blocked_reason or fee.status != 200 or not fee.content_hash:
        return DiscoveryResult(
            institution_id, index_url, index_hash, fee_url, None, used,
            "found_index", home_hash=home.content_hash,
        )

    return DiscoveryResult(
        institution_id,
        index_url,
        index_hash,
        fee_url,
        fee.content_hash,
        used,
        "found_fee",
        home_hash=home.content_hash,
    )
