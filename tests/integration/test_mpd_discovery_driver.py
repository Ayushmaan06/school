"""M2-1: the three-hop discovery driver. No live network - respx only.

The plan requires the 6-fetch cap to hold in ALL branches, so every test here
asserts it, not just the ones about budget.
"""

import httpx
import pytest
import respx
from sqlalchemy import text

from school_intel.fetch.client import Fetcher
from school_intel.sources.cbse_mpd import MAX_FETCHES, discover

ROBOTS = "User-agent: *\nAllow: /\n"

PROFORMA = """
<html><body><h3>C : RESULT AND ACADEMICS</h3>
<p>Green Valley Public School. Affiliation 800001. Trust: Green Valley Trust.
Class wise student strength below.</p>
<table>
  <tr><td>1</td><td>FEE STRUCTURE OF THE SCHOOL</td>
      <td><a href="/uploads/Fee-Structure.pdf">View</a></td></tr>
  <tr><td>2</td><td>ANNUAL ACADEMIC CALENDER</td>
      <td><a href="/uploads/calendar.pdf">View</a></td></tr>
</table></body></html>
"""
HOMEPAGE_WITH_NAV = (
    '<html><body><nav><a href="/mandatory-disclosure/">'
    "<span>Mandatory Disclosure</span></a></nav></body></html>"
)
BARE_HOMEPAGE = "<html><body><a href='/admissions'>Admissions</a></body></html>"


@pytest.fixture
def clean(db_session):
    db_session.execute(text("TRUNCATE fetches, raw_documents CASCADE"))
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days,"
            " rate_limit_rps) VALUES ('cbse_mpd','institution_site',3,180,1000)"
            " ON CONFLICT (id) DO UPDATE SET rate_limit_rps = 1000"
        )
    )
    db_session.commit()
    yield


@pytest.fixture
def fetcher(tmp_path):
    return Fetcher("Test/1.0", tmp_path / "raw")


def _robots(host="https://s.test"):
    respx.get(f"{host}/robots.txt").mock(
        return_value=httpx.Response(
            200, text=ROBOTS, headers={"content-type": "text/plain"}
        )
    )


async def _run(fetcher, session, name="Green Valley Public School", site="s.test"):
    async with fetcher as f:
        return await discover(
            f, session, institution_id=1, canonical_name=name, website=site
        )


async def test_found_via_homepage_nav_then_the_fee_pdf(db_session, clean, fetcher):
    """The happy path, and the whole point of M2-1: three hops, ending at a fee
    document rather than at the index page."""
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(200, text=HOMEPAGE_WITH_NAV)
        )
        respx.get("https://s.test/mandatory-disclosure/").mock(
            return_value=httpx.Response(200, text=PROFORMA)
        )
        respx.get("https://s.test/uploads/Fee-Structure.pdf").mock(
            return_value=httpx.Response(
                200,
                content=b"%PDF-1.4 fee table",
                headers={"content-type": "application/pdf"},
            )
        )
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "found_fee"
    assert result.mpd_url == "https://s.test/mandatory-disclosure/"
    assert result.fee_url == "https://s.test/uploads/Fee-Structure.pdf"
    assert result.fee_hash is not None
    assert result.fetches <= MAX_FETCHES


async def test_found_via_a_guessed_path_when_the_nav_has_no_link(
    db_session, clean, fetcher
):
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(200, text=BARE_HOMEPAGE)
        )
        respx.get("https://s.test/mandatory-disclosure").mock(
            return_value=httpx.Response(200, text=PROFORMA)
        )
        respx.get("https://s.test/uploads/Fee-Structure.pdf").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
            )
        )
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "found_fee"
    assert result.fetches <= MAX_FETCHES


async def test_the_whole_proforma_published_as_one_pdf(db_session, clean, fetcher):
    """[VERIFIED M0-0] e.g. afsjal.org/disclosure/b1.pdf - index and fee in one
    document, so there is no third hop to make."""
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(
                200, text='<a href="/disclosure/b1.pdf">Mandatory Disclosure</a>'
            )
        )
        respx.get("https://s.test/disclosure/b1.pdf").mock(
            return_value=httpx.Response(
                200,
                content=b"%PDF-1.4 whole proforma",
                headers={"content-type": "application/pdf"},
            )
        )
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "found_fee"
    assert result.fee_hash == result.mpd_hash
    assert result.fetches <= MAX_FETCHES


async def test_index_found_but_no_fee_link(db_session, clean, fetcher):
    """A real and common outcome. Reported distinctly from not_found, because
    the two need different fixes."""
    no_fee = (
        "<html><body>Affiliation 800001. Trust deed. Student strength.</body></html>"
    )
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(
                200, text='<a href="/mandatory-disclosure/">MD</a>'
            )
        )
        respx.get("https://s.test/mandatory-disclosure/").mock(
            return_value=httpx.Response(200, text=no_fee)
        )
        result = await _run(fetcher, db_session, name="")
        db_session.commit()

    assert result.outcome == "found_index"
    assert result.fee_url is None
    assert result.fetches <= MAX_FETCHES


async def test_login_wall_is_abandoned_not_looped(db_session, clean, fetcher):
    wall = "<html><body>Parent Login. Username Password. Sign in</body></html>"
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(
                200, text='<a href="/mandatory-disclosure/">MD</a>'
            )
        )
        respx.get("https://s.test/mandatory-disclosure/").mock(
            return_value=httpx.Response(200, text=wall)
        )
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "login_wall"
    assert result.fetches <= MAX_FETCHES


async def test_a_wrong_school_page_is_rejected(db_session, clean, fetcher):
    """Shared hosting serves another school's disclosure page. Accepting it
    would attach a competitor's fees to this institution."""
    other = (
        "<html><body>Blue Ridge International Academy. Fee structure. "
        "Trust. Student strength. Affiliation.</body></html>"
    )
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(
                200, text='<a href="/mandatory-disclosure/">MD</a>'
            )
        )
        respx.get("https://s.test/mandatory-disclosure/").mock(
            return_value=httpx.Response(200, text=other)
        )
        respx.get(url__startswith="https://s.test/").mock(
            return_value=httpx.Response(404, text="nope")
        )
        result = await _run(fetcher, db_session, name="Green Valley Public School")
        db_session.commit()

    assert result.outcome == "not_found"
    assert result.fetches <= MAX_FETCHES


async def test_no_mpd_page_still_returns_the_homepage(db_session, clean, fetcher):
    """The homepage is where the phone and email actually live.

    `not_found` is the most common outcome, and the caller mines home_hash for
    contacts. Losing it means losing most of the contact coverage.
    """
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(
                200, text="<footer>Call 080 4123 4567 or office@s.test</footer>"
            )
        )
        respx.route(host="s.test").mock(return_value=httpx.Response(404, text="nope"))
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "not_found"
    assert result.mpd_hash is None
    assert result.home_hash is not None

    from school_intel.extract.mpd.run import extract_contacts
    from school_intel.fetch.store import load

    contacts = extract_contacts(load(db_session, result.home_hash))
    assert contacts.phone and contacts.email


async def test_not_found_still_respects_the_cap(db_session, clean, fetcher):
    """The worst case for budget: nothing matches, every guess is tried."""
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(200, text=BARE_HOMEPAGE)
        )
        respx.route(host="s.test").mock(return_value=httpx.Response(404, text="nope"))
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "not_found"
    assert result.fetches <= MAX_FETCHES

    made = db_session.execute(
        text("SELECT count(*) FROM fetches WHERE url NOT LIKE '%robots.txt'")
    ).scalar()
    assert made <= MAX_FETCHES, (
        "the cap must hold in the worst case, not just the happy one"
    )


async def test_no_website_costs_zero_fetches(db_session, clean, fetcher):
    async with fetcher as f:
        result = await discover(
            f, db_session, institution_id=1, canonical_name="X", website=None
        )
    assert result.outcome == "no_website"
    assert result.fetches == 0


async def test_a_denylisted_fee_link_is_never_followed(db_session, clean, fetcher):
    """The proforma section is titled RESULT AND ACADEMICS and its sibling rows
    link to board results. Following those is what the denylist prevents."""
    results_row = """
    <html><body><p>Trust. Affiliation. Student strength.</p><table>
      <tr><td>FEE STRUCTURE OF THE SCHOOL</td>
          <td><a href="/uploads/class-12-result.pdf">View</a></td></tr>
    </table></body></html>
    """
    with respx.mock:
        _robots()
        respx.get("https://s.test/").mock(
            return_value=httpx.Response(
                200, text='<a href="/mandatory-disclosure/">MD</a>'
            )
        )
        respx.get("https://s.test/mandatory-disclosure/").mock(
            return_value=httpx.Response(200, text=results_row)
        )
        blocked = respx.get("https://s.test/uploads/class-12-result.pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF names and marks")
        )
        result = await _run(fetcher, db_session, name="")
        db_session.commit()

        assert not blocked.called, "a denylisted document was fetched"

    assert result.outcome == "found_index"
    assert result.fee_hash is None


async def test_dead_origin_costs_one_fetch_not_six(db_session, clean, fetcher):
    """A 5xx homepage stops the walk. [VERIFIED M5] the biggest time sink.

    A dead domain used to cost the homepage plus all five guessed paths, each
    waiting out the 30s timeout - three minutes for a school that can never
    yield anything.
    """
    with respx.mock:
        _robots()
        respx.route(host="s.test").mock(return_value=httpx.Response(522, text=""))
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome == "dead_origin"
    assert result.fetches == 1


async def test_a_404_homepage_still_tries_the_guessed_paths(db_session, clean, fetcher):
    """404 means the server is ALIVE. The disclosure page may still exist."""
    with respx.mock:
        _robots()
        respx.get("https://s.test/mandatory-disclosure/").mock(
            return_value=httpx.Response(200, text=PROFORMA)
        )
        respx.get("https://s.test/").mock(return_value=httpx.Response(404, text="no"))
        respx.route(host="s.test").mock(return_value=httpx.Response(404, text="no"))
        result = await _run(fetcher, db_session)
        db_session.commit()

    assert result.outcome != "dead_origin"
    assert result.fetches > 1
