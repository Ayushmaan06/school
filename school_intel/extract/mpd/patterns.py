"""Tier 2 - pattern extractors, for fields tier 1 missed on free-layout pages.

Runs after the label/table parser and only fills what it left empty. Same schema
(ADR-013), so the two tiers are interchangeable and comparable via `make eval`.

Every function here returns None rather than a best guess.
"""

import re

from school_intel.extract.normalize import normalize_person, normalize_phone

EXTRACTOR = "regex:mpd_patterns_v1"

# RFC-ish. Deliberately not exhaustive: a false email is worse than none, since
# BD would email a stranger.
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Addresses belonging to the site's builder rather than the school.
EMAIL_REJECT = re.compile(
    r"@(?:example|test|localhost|sentry|wordpress|wixpress|godaddy|"
    r"cloudflare|gmail\.example)|webmaster@|postmaster@|noreply@|no-reply@",
    re.IGNORECASE,
)

PRINCIPAL_LABEL = re.compile(
    r"(?:principal|head\s*of\s*(?:the\s*)?(?:school|institution)|head\s*master|"
    r"headmistress)\s*[:\-–]?\s*",
    re.IGNORECASE,
)
# A principal name: 2-5 capitalised-ish words. Stops at punctuation so it does
# not swallow the rest of a sentence.
NAME_AFTER_LABEL = re.compile(
    r"([A-Z][A-Za-z.'\-]{1,20}(?:\s+[A-Z][A-Za-z.'\-]{1,20}){1,4})"
)

STREAM_KEYWORDS = {
    "PCM": re.compile(r"\bPCM\b|\bphysics\W{0,3}chemistry\W{0,3}math", re.IGNORECASE),
    "PCB": re.compile(r"\bPCB\b|\bphysics\W{0,3}chemistry\W{0,3}bio", re.IGNORECASE),
    "PCMB": re.compile(r"\bPCMB\b", re.IGNORECASE),
    "Commerce": re.compile(r"\bcommerce\b", re.IGNORECASE),
    "Arts": re.compile(r"\barts\b|\bhumanities\b", re.IGNORECASE),
}


def find_email(text: str) -> str | None:
    for candidate in EMAIL.findall(text or ""):
        if EMAIL_REJECT.search(candidate):
            continue
        return candidate.lower()
    return None


def find_phone(text: str) -> str | None:
    numbers = normalize_phone(text)
    return numbers[0] if numbers else None


# Words that mean the text after a "principal" label is NOT a name.
# [VERIFIED M5] "Principal's Desk Message" in a website nav produced a principal
# called "desk message", which shipped into the CSV export. Website chrome uses
# the word principal constantly - desk, message, welcome, speaks, corner - and
# none of those are followed by a person.
PRINCIPAL_REJECT = re.compile(
    r"^\W*(qualification|experience|signature|photo|contact|email|mobile|phone|"
    r"desk|message|welcome|speak|corner|note|address|profile|blog|column|"
    r"name\s*of\s*the\s*school|s\s*desk)",
    re.IGNORECASE,
)


def find_principal(text: str) -> str | None:
    """The name FOLLOWING a principal label, honorifics stripped.

    Never sourced from OCR output: [VERIFIED M0-0] OCR renders "Principal" as
    "Pingipa!" while getting tabular digits exactly right, so names from a
    scanned document are not trustworthy even when the numbers are.
    """
    match = PRINCIPAL_LABEL.search(text or "")
    if not match:
        return None
    tail = text[match.end() : match.end() + 120]
    # [VERIFIED M2-4] "PRINCIPAL QUALIFICATION Msc, B.Ed" yielded a
    # principal_name of "qualification msc". A qualification is not a name, and
    # COMPLIANCE.md says we do not store one at all.
    if PRINCIPAL_REJECT.match(tail):
        return None
    name = NAME_AFTER_LABEL.search(tail)
    if not name:
        return None
    return normalize_person(name.group(1))


def find_streams(text: str) -> list[str] | None:
    found = [name for name, rx in STREAM_KEYWORDS.items() if rx.search(text or "")]
    return found or None


_FINDERS = {
    "email": find_email,
    "phone": find_phone,
    "principal_name": find_principal,
    "streams": find_streams,
}


def value_for(field: str, text: str):
    finder = _FINDERS.get(field)
    return finder(text) if finder else None


def fill_gaps(extraction, text: str):
    """Populate fields tier 1 left empty. Returns a NEW extraction.

    Tier 2 never overwrites tier 1: a labelled value beats a pattern match,
    because the label is evidence of intent and the pattern is a guess about
    layout.
    """
    values = extraction.model_dump()
    evidence = dict(extraction.evidence)

    for field, finder in _FINDERS.items():
        if values.get(field) is not None:
            continue
        found = finder(text)
        if found is None:
            continue
        values[field] = found
        evidence[field] = _span(text, found)

    values["evidence"] = evidence
    return type(extraction)(**values)


def span_for(text: str, value, width: int = 60) -> str:
    """Public: the verbatim window around a matched value."""
    return _span(text, value, width)


def _span(text: str, value, width: int = 60) -> str:
    needle = value[0] if isinstance(value, list) else str(value)
    index = (text or "").lower().find(str(needle).lower())
    if index < 0:
        return str(value)
    start = max(0, index - width)
    return re.sub(r"\s+", " ", text[start : index + len(str(needle)) + width]).strip()
