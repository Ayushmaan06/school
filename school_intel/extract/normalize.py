"""Stage 1 normalisation. Pure functions - no I/O, no database, no network.

Normalisation is where most entity-resolution accuracy actually comes from
(docs/ENTITY-RESOLUTION.md). Everything here is table-driven from
`normalize_data/*.yaml`, because every one of these lists grows the first time a
new naming style turns up, and a config edit is cheaper than a code change.

The most important function in this module is `parse_inr`, and the most
important thing it does is return None.
"""

import re
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).with_name("normalize_data")

HONORIFICS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "dr",
        "prof",
        "smt",
        "shri",
        "sri",
        "fr",
        "sr",
        "rev",
        "capt",
    }
)


@lru_cache(maxsize=1)
def _abbreviations() -> list[tuple[str, str]]:
    """Longest-first, so 'sr sec' expands before 'sr'."""
    raw: dict[str, str] = yaml.safe_load(
        (DATA / "abbreviations.yaml").read_text("utf-8")
    )
    return sorted(raw.items(), key=lambda kv: -len(kv[0]))


@lru_cache(maxsize=1)
def _generic_words() -> frozenset[str]:
    return frozenset(yaml.safe_load((DATA / "generic_words.yaml").read_text("utf-8")))


@lru_cache(maxsize=1)
def _city_renames() -> dict[str, str]:
    return yaml.safe_load((DATA / "city_renames.yaml").read_text("utf-8"))


@lru_cache(maxsize=1)
def _titles() -> list[tuple[str, str]]:
    raw: dict[str, list[str]] = yaml.safe_load(
        (DATA / "titles.yaml").read_text("utf-8")
    )
    pairs = [
        (alias, canonical) for canonical, aliases in raw.items() for alias in aliases
    ]
    return sorted(pairs, key=lambda kv: -len(kv[0]))


def _basic(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().replace("&", " and ")
    # Apostrophes are DELETED, not replaced with a space: "St. Mary's" must key
    # the same as "Saint Marys", and " " would split it into "mary" + "s".
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_key(name: str) -> str:
    """A comparable key for one institution name. Never displayed.

    canonical_name is what a human reads; this is derived and only ever used for
    matching. Tokens are sorted, so word order cannot split a match.
    """
    if not name:
        return ""
    text = _basic(name)

    tokens = text.split()
    expanded: list[str] = []
    for token in tokens:
        replacement = next((v for k, v in _abbreviations() if k == token), None)
        expanded.extend(replacement.split() if replacement else [token])

    # Multi-word abbreviations ("k v") need a second pass over the joined text.
    joined = " ".join(expanded)
    for abbreviation, full in _abbreviations():
        if " " in abbreviation:
            joined = re.sub(rf"\b{re.escape(abbreviation)}\b", full, joined)

    # Place names inside an institution name need the same rename map as the
    # city column: "DPS Bangalore" and "Delhi Public School, Bengaluru" are one
    # school, and without this they key differently.
    renames = _city_renames()
    for old, new in renames.items():
        if " " in old:
            joined = re.sub(rf"\b{re.escape(old)}\b", new, joined)
    joined = " ".join(renames.get(t, t) for t in joined.split())

    generic = _generic_words()
    kept = [t for t in joined.split() if t not in generic]
    # Everything was generic ("The Public School") - fall back to the full token
    # set rather than returning an empty key that matches every other empty key.
    if not kept:
        kept = joined.split()
    return " ".join(sorted(kept))


def normalize_city(city: str | None, state: str | None = None) -> str | None:
    """Apply the rename map. `state` is accepted because city alone is NEVER a
    join key (Project-Doc 6.3) - callers should key on the pair."""
    if not city:
        return None
    text = _basic(city)
    return _city_renames().get(text, text) or None


def normalize_state(state: str | None) -> str | None:
    if not state:
        return None
    text = _basic(state)
    return _city_renames().get(text, text) or None


def normalize_person(name: str | None) -> str | None:
    """Strip honorifics, normalise initials, collapse whitespace.

    'Dr. A.B. Sharma' and 'A B Sharma' must produce the same string, or M4-3
    emits a principal_change signal every time a source adds or drops a title.
    """
    if not name:
        return None
    text = _basic(name)
    tokens = [t for t in text.split() if t not in HONORIFICS]
    return " ".join(tokens) or None


def normalize_title(raw: str | None) -> str:
    """Map to the roles.title_normalized vocabulary. Unknown -> 'other'.

    title_raw is always kept by the caller (Project-Doc 6.8); this is only for
    filtering and scoring.
    """
    if not raw:
        return "other"
    text = _basic(raw)
    for alias, canonical in _titles():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return canonical
    return "other"


def normalize_domain(url: str | None) -> str | None:
    """Registrable-ish domain: scheme-insensitive, `www` stripped, no path."""
    if not url:
        return None
    text = url.strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.split("@")[-1].split(":")[0]
    text = text.removeprefix("www.")
    # [VERIFIED M0-0] SARAS publishes at least one URL as
    # "https://.agragamividyakendra.com/" - a leading dot straight from the
    # registry. Stray edge dots are unambiguous noise, not a guess about intent.
    text = text.strip(".")
    if "." not in text or text in {"na", "n/a", "nil"}:
        return None
    # A label cannot be empty, so "a..b.com" is malformed beyond repair.
    if any(not label for label in text.split(".")):
        return None
    return text or None


def normalize_phone(text: str | None) -> list[str]:
    """Every distinct Indian phone number in a blob, as 10-digit strings.

    Landlines with STD codes, mobiles with or without +91, and separator noise
    all collapse to the same form so a phone match is a real match.
    """
    if not text:
        return []
    out: list[str] = []
    for raw in re.findall(r"(?:\+?91[\s-]?)?\d[\d\s()-]{7,14}\d", text):
        digits = re.sub(r"\D", "", raw)
        digits = digits.removeprefix("910") if digits.startswith("910") else digits
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        digits = digits.lstrip("0")
        if 10 <= len(digits) <= 11:
            digits = digits[-10:]
            if digits not in out:
                out.append(digits)
    return out


def normalize_legal_entity(name: str | None) -> str | None:
    """Trust/society name, for the ADR-005 group axis.

    Legal suffixes vary freely between sources for the same registered body, so
    they are dropped: 'Sutara Trust' and 'Sutara Trust (Regd.)' are one entity.
    """
    if not name:
        return None
    text = _basic(name)
    text = re.sub(
        r"\b(regd|registered|reg|trust|society|societies|samiti|sangha|sangh|"
        r"foundation|charitable|educational|education|welfare|association|"
        r"committee|managing|management|pvt|private|ltd|limited|inc)\b",
        " ",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(sorted(text.split())) or None


# ---------------------------------------------------------------------------
# Money. The function that must return None.
# ---------------------------------------------------------------------------

_LAKH = re.compile(r"([\d.,]+)\s*(?:lakh|lakhs|lac|lacs|l)\b", re.IGNORECASE)
_CRORE = re.compile(r"([\d.,]+)\s*(?:crore|crores|cr)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# NOTE: no leading or trailing \b on these. An alternation that begins with "/"
# can never match after a \b, because the character before "/" is whitespace and
# "/" is not a word character - so "Rs 15,000 / month" silently read as
# "period ambiguous" and the fee was dropped. Word boundaries go inside the
# alternatives that actually need them.
_MONTHLY = re.compile(
    r"(?:per\s*month|/\s*month|\bp\.?\s*m\b\.?|\bmonthly\b|\ba\s*month\b|per\s*mensem)",
    re.IGNORECASE,
)
_ANNUAL = re.compile(
    r"(?:per\s*annum|/\s*annum|\bp\.?\s*a\b\.?|\bannual(?:ly)?\b|per\s*year|/\s*year|"
    r"\ba\s*year\b|\byearly\b|/\s*yr\b|per\s*session|academic\s*year)",
    re.IGNORECASE,
)
_QUARTERLY = re.compile(r"(?:per\s*quarter|\bquarterly\b|/\s*quarter)", re.IGNORECASE)
_TERM = re.compile(r"(?:per\s*term|\btermly\b|/\s*term)", re.IGNORECASE)

# Anything outside this is not a plausible annual school fee, and emitting it
# would be a guess dressed as data.
MIN_PLAUSIBLE_ANNUAL_INR = 1_000
MAX_PLAUSIBLE_ANNUAL_INR = 5_000_000


def parse_inr(text: str | None, *, assume_annual: bool = False) -> int | None:
    """Parse an Indian fee expression to ANNUAL rupees, or return None.

    Returns None whenever the period is ambiguous. That is the entire point:
    docs/ENTITY-RESOLUTION.md is explicit that a 12x error moves an institution
    across four affordability bands, so silence is mandatory. Missing beats
    wrong (hard rule 6).

    `assume_annual=True` is for contexts where the surrounding document has
    already established the period - an Appendix-IX "ANNUAL FEE" column header,
    say. It is never a default, and callers must be able to point at the
    evidence that justifies it.
    """
    if not text:
        return None

    if _CRORE.search(text):
        value = _to_float(_CRORE.search(text).group(1))
        amount = None if value is None else value * 10_000_000
    elif _LAKH.search(text):
        value = _to_float(_LAKH.search(text).group(1))
        amount = None if value is None else value * 100_000
    else:
        match = _NUMBER.search(text)
        amount = _to_float(match.group(0)) if match else None

    if amount is None:
        return None

    monthly, annual = bool(_MONTHLY.search(text)), bool(_ANNUAL.search(text))
    if monthly and annual:
        return None  # the source contradicts itself; say nothing
    if monthly:
        amount *= 12
    elif _QUARTERLY.search(text):
        amount *= 4
    elif _TERM.search(text):
        # A "term" is three terms in some schools and two in others. Unknowable.
        return None
    elif not annual and not assume_annual:
        return None

    result = round(amount)
    if not MIN_PLAUSIBLE_ANNUAL_INR <= result <= MAX_PLAUSIBLE_ANNUAL_INR:
        return None
    return result


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None
