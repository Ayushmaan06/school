"""Contact tiers and the two UI filters.

The point of the tier is that a WEAK school is one we visited and found no
published contact on - not one we simply never looked at. If that distinction
breaks, the BD list silently fills with schools nobody has checked.
"""

from sqlalchemy import text

from school_intel.api import queries


def test_tiers_partition_every_school(db_session):
    counts = {}
    for tier in ("verified", "weak", "none"):
        rows = queries.schools_in_state(
            db_session, "karnataka", limit=5000, contact=tier
        )
        counts[tier] = len(rows)
        for row in rows:
            assert row["contact_tier"] == tier

    every = queries.schools_in_state(db_session, "karnataka", limit=5000)
    assert sum(counts.values()) == len(every), counts

    reachable = queries.schools_in_state(
        db_session, "karnataka", limit=5000, contact="reachable"
    )
    assert len(reachable) == counts["verified"] + counts["weak"]


def test_weak_means_visited_and_has_a_site_but_no_contact(db_session):
    for row in queries.schools_in_state(
        db_session, "karnataka", limit=5000, contact="weak"
    ):
        assert not row["phone"] and not row["email"]
        assert row["website"], "weak tier must carry a website - it is the only way in"


def test_unconfirmed_class12_filter(db_session):
    rows = queries.schools_in_state(
        db_session, "karnataka", limit=5000, class12="unconfirmed"
    )
    assert all(not r["has_class_12"] for r in rows)


def test_unknown_filter_value_falls_back_to_all(db_session):
    every = queries.schools_in_state(db_session, "karnataka", limit=5000)
    junk = queries.schools_in_state(
        db_session, "karnataka", limit=5000, contact="'; DROP TABLE institutions--"
    )
    assert len(junk) == len(every)


# --- guessed emails -------------------------------------------------------
# The guess is useful ONLY while it stays distinguishable from a published
# address. These guard that boundary, not the hit rate.

from school_intel.guess_email import guess_for, registrable_token


def test_token_extraction():
    cases = {
        "www.dpsnadergul.com": "dpsnadergul",
        "https://bikanerboysschool.ac.in/": "bikanerboysschool",
        "nalandapublicschool.co.in": "nalandapublicschool",
        "www.dipsgilzian/in": "dipsgilzian",  # typo'd separator
        "bioreschool@.in": "bioreschool",  # email typed into website
        "sgtba.in": "sgtba",
        "nchs-gwalior.edu.in": "nchsgwalior",
    }
    for site, want in cases.items():
        assert registrable_token(site) == want, site
    # Nothing usable -> no guess, rather than a bad one. "gmail.com" is the
    # placeholder 117 schools carry; it names no school.
    for junk in ("", None, "www.school.com", "gmail.com", "sites.google.com/x"):
        assert registrable_token(junk) is None, junk
    assert guess_for("carmelujjain.com") == "carmelujjain@gmail.com"


def test_guess_never_lands_in_the_verified_email_column(db_session):
    """The whole safety property, in one query."""
    clash = db_session.execute(
        text(
            "SELECT count(*) FROM institutions"
            " WHERE email_guessed IS NOT NULL AND email IS NOT NULL"
        )
    ).scalar()
    assert clash == 0, "a guessed address sits on a school that has a real one"


def test_backfill_only_touches_enriched_non_contactables(db_session):
    stray = db_session.execute(
        text(
            "SELECT count(*) FROM institutions WHERE email_guessed IS NOT NULL"
            " AND (phone IS NOT NULL OR email IS NOT NULL"
            "      OR last_enriched_at IS NULL OR website IS NULL)"
        )
    ).scalar()
    assert stray == 0


def test_shared_vendor_domains_are_skipped(db_session):
    """`vnps.claraerp.com` must not become `claraerp@gmail.com` on 40 schools."""
    worst = db_session.execute(
        text(
            "SELECT count(*) FROM institutions WHERE email_guessed IS NOT NULL"
            " GROUP BY email_guessed ORDER BY count(*) DESC LIMIT 1"
        )
    ).scalar()
    assert worst is None or worst <= 3, f"{worst} schools share one guessed address"
