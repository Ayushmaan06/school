"""Derive a likely gmail address from a school's own domain.

The pattern, from operator field research in Nagpur (~40% hit) and Patna
(~80%): a school on `www.<name>.<tld>` very often uses `<name>@gmail.com` as
its working inbox, because the domain is a brochure site and the mail lives on
free gmail.

This is a GUESS. It is written to `institutions.email_guessed`, never to
`email`, so `resolve`, `score` and every contactable count stay based on
published facts only (hard rule 6). The UI labels it, and the CSV export names
the column so nobody mistakes it for a verified address.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# Registrable-domain suffixes that take two labels. Without these,
# `bikanerboysschool.ac.in` would yield "ac" instead of the school name.
MULTI_SUFFIXES = {
    "ac.in", "co.in", "edu.in", "org.in", "net.in", "gov.in",
    "sch.in", "nic.in", "res.in", "gen.in", "ind.in", "co.uk",
}

# Tokens that identify a platform or a generic word rather than a school.
GENERIC = {
    "school", "schools", "vidyalaya", "academy", "college", "education",
    "wixsite", "blogspot", "wordpress", "weebly", "google", "sites",
    "webs", "yolasite", "godaddysites", "squarespace", "gmail", "com",
    "org", "net", "edu", "home", "index", "www", "public",
}

# A school domain shared by this many institutions is an ERP vendor or a
# hosting platform, not one school's site. The token would be the VENDOR's
# name - `vnps.claraerp.com` yields "claraerp" - so the guess is skipped.
# ponytail: a count, not a blocklist. Blocklists need maintaining; a shared
# domain identifies itself.
MAX_SCHOOLS_PER_DOMAIN = 3

_CLEAN = re.compile(r"[^a-z0-9]")


def registrable_token(website: str | None) -> str | None:
    """The school's own name as it appears in its domain, or None.

    >>> registrable_token("www.dpsnadergul.com")
    'dpsnadergul'
    >>> registrable_token("https://bikanerboysschool.ac.in/")
    'bikanerboysschool'
    >>> registrable_token("www.dipsgilzian/in")
    'dipsgilzian'
    """
    if not website:
        return None
    host = website.strip().lower()
    host = re.sub(r"^https?:(//)?", "", host)
    # Cut path, query and fragment. A typo'd separator ("name/in") loses its
    # broken suffix here too, which is the right answer.
    host = re.split(r"[/?#]", host)[0]
    host = host.removeprefix("www.").strip(".")
    if not host:
        return None

    # Some schools typed an email into the website field with a mangled domain
    # ("bioreschool@.in"). The local part is the school's own name, and it is
    # the one case here where the token is stated rather than inferred.
    if "@" in host:
        host = host.split("@", 1)[0]

    labels = [x for x in host.split(".") if x]
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_SUFFIXES:
        token = labels[-3]
    elif len(labels) >= 2:
        token = labels[-2]
    else:
        token = labels[0]

    token = _CLEAN.sub("", token)
    if len(token) < 4 or token in GENERIC or token.isdigit():
        return None
    return token


def guess_for(website: str | None) -> str | None:
    token = registrable_token(website)
    return f"{token}@gmail.com" if token else None


def backfill(session: Session, *, only_weak: bool = True) -> dict[str, int]:
    """Fill `email_guessed` for enriched schools with a site but no contact."""
    where = ["i.status = 'active'", "i.website IS NOT NULL"]
    if only_weak:
        # The operator's scope: enriched non-contactables only. A school we
        # never visited might still publish a real address, and a guess would
        # stop us from ever looking.
        where += [
            "i.phone IS NULL",
            "i.email IS NULL",
            "i.last_enriched_at IS NOT NULL",
        ]
    rows = session.execute(
        text(f"SELECT i.id, i.website FROM institutions i WHERE {' AND '.join(where)}")
    ).all()

    tokens: dict[int, str] = {}
    for inst_id, website in rows:
        token = registrable_token(website)
        if token:
            tokens[inst_id] = token

    # Drop vendor/platform domains, identified by how many schools share them.
    shared: dict[str, int] = {}
    for token in tokens.values():
        shared[token] = shared.get(token, 0) + 1
    skipped_shared = sum(1 for t in tokens.values() if shared[t] > MAX_SCHOOLS_PER_DOMAIN)

    now = datetime.now(UTC)
    written = 0
    for inst_id, token in tokens.items():
        if shared[token] > MAX_SCHOOLS_PER_DOMAIN:
            continue
        session.execute(
            text(
                "UPDATE institutions SET email_guessed = :e, email_guessed_at = :t"
                " WHERE id = :id"
            ),
            {"e": f"{token}@gmail.com", "t": now, "id": inst_id},
        )
        written += 1

    return {
        "considered": len(rows),
        "guessed": written,
        "no_usable_token": len(rows) - len(tokens),
        "skipped_shared_domain": skipped_shared,
    }
