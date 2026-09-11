"""M1-2: the single HTTP path. No live network - respx intercepts everything."""

from unittest import mock

import httpx
import pytest
import respx
from sqlalchemy import text

from school_intel.fetch import denylist
from school_intel.fetch.client import Fetcher

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /private\n"


@pytest.fixture
def clean(db_session):
    db_session.execute(
        text("TRUNCATE fetches, raw_documents, denylist_learned CASCADE")
    )
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days, rate_limit_rps)"
            " VALUES ('test_src','registry',1,30,1000) ON CONFLICT (id) DO UPDATE"
            " SET rate_limit_rps = 1000"
        )
    )
    db_session.commit()
    denylist.match_static.cache_clear()
    yield


@pytest.fixture
def fetcher(tmp_path):
    return Fetcher(user_agent="TestAgent/1.0", raw_root=tmp_path / "raw")


async def test_same_url_twice_writes_two_fetches_and_one_document(
    db_session, clean, fetcher
):
    """M1-2 DoD. Content addressing gives dedup for free: the second fetch proves
    freshness without storing the bytes again."""
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        respx.get("https://school.test/page").mock(
            return_value=httpx.Response(200, text="<html>same bytes</html>")
        )
        async with fetcher as f:
            first = await f.fetch(db_session, "https://school.test/page", "test_src")
            second = await f.fetch(db_session, "https://school.test/page", "test_src")
        db_session.commit()

    assert first.content_hash == second.content_hash
    assert first.fetch_id != second.fetch_id
    assert db_session.execute(text("SELECT count(*) FROM fetches")).scalar() == 2
    assert db_session.execute(text("SELECT count(*) FROM raw_documents")).scalar() == 1


async def test_denylisted_url_makes_no_request(db_session, clean, fetcher):
    """The bytes must never exist on our disk. Hard rule 1."""
    with respx.mock:
        route = respx.get("https://school.test/results").mock(
            return_value=httpx.Response(200, text="TOP SCORERS")
        )
        robots = respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        async with fetcher as f:
            result = await f.fetch(
                db_session, "https://school.test/results", "test_src"
            )
        db_session.commit()

        assert not route.called, "a denylisted URL was actually requested"
        assert not robots.called, "the denylist must short-circuit before robots"

    assert result.blocked_reason is not None
    assert result.content_hash is None
    row = db_session.execute(
        text("SELECT http_status, denylist_hit FROM fetches WHERE id=:i"),
        {"i": result.fetch_id},
    ).first()
    assert row[0] is None, "no request was made, so there is no status"
    assert row[1] is not None, "we must record that we declined, not leave a gap"
    assert db_session.execute(text("SELECT count(*) FROM raw_documents")).scalar() == 0


async def test_robots_disallow_is_honoured_and_recorded(db_session, clean, fetcher):
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_DENY, headers={"content-type": "text/plain"}
            )
        )
        page = respx.get("https://school.test/private/x").mock(
            return_value=httpx.Response(200, text="secret")
        )
        async with fetcher as f:
            result = await f.fetch(
                db_session, "https://school.test/private/x", "test_src"
            )
        db_session.commit()
        assert not page.called

    allowed = db_session.execute(
        text("SELECT robots_allowed FROM fetches WHERE id=:i"), {"i": result.fetch_id}
    ).scalar()
    assert allowed is False


async def test_missing_robots_txt_means_allow(db_session, clean, fetcher):
    """[VERIFIED M0-0] saras.cbse.gov.in serves an HTML 404 for /robots.txt.
    An HTML error page is not a robots.txt and must not be parsed as one."""
    with respx.mock:
        respx.get("https://saras.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text="<html>404</html>", headers={"content-type": "text/html"}
            )
        )
        respx.get("https://saras.test/list").mock(
            return_value=httpx.Response(200, text="rows")
        )
        async with fetcher as f:
            result = await f.fetch(db_session, "https://saras.test/list", "test_src")
        db_session.commit()
    assert result.status == 200
    assert result.content_hash is not None


async def test_pdf_is_stored_as_is(db_session, clean, fetcher):
    pdf = b"%PDF-1.4\nfake scanned fee structure"
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        respx.get("https://school.test/fee.pdf").mock(
            return_value=httpx.Response(
                200, content=pdf, headers={"content-type": "application/pdf"}
            )
        )
        async with fetcher as f:
            result = await f.fetch(
                db_session, "https://school.test/fee.pdf", "test_src"
            )
        db_session.commit()

    row = db_session.execute(
        text(
            "SELECT media_type, bytes, storage_path FROM raw_documents WHERE content_hash=:h"
        ),
        {"h": result.content_hash},
    ).first()
    assert row[0] == "application/pdf"
    assert row[1] == len(pdf)
    from pathlib import Path

    assert Path(row[2]).read_bytes() == pdf, "PDFs must be stored byte-identical"


async def test_url_with_spaces_is_encoded_not_rejected(db_session, clean, fetcher):
    """[VERIFIED M0-0] '/pdf/md/C1.FEE STRUCTURE.pdf' is a real fee link."""
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        respx.get("https://school.test/md/C1.FEE%20STRUCTURE.pdf").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"}
            )
        )
        async with fetcher as f:
            result = await f.fetch(
                db_session, "https://school.test/md/C1.FEE STRUCTURE.pdf", "test_src"
            )
        db_session.commit()
    assert result.status == 200


async def test_http_error_is_recorded_without_a_document(db_session, clean, fetcher):
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        respx.get("https://school.test/gone").mock(
            side_effect=httpx.ConnectError("refused")
        )
        async with fetcher as f:
            result = await f.fetch(db_session, "https://school.test/gone", "test_src")
        db_session.commit()

    err = db_session.execute(
        text("SELECT error FROM fetches WHERE id=:i"), {"i": result.fetch_id}
    ).scalar()
    assert "ConnectError" in err
    assert result.content_hash is None


async def test_learned_denylist_blocks_a_previously_allowed_url(
    db_session, clean, fetcher
):
    url = "https://school.test/staff-directory"
    denylist.learn(db_session, url, "student_data_detected", "extractor")
    db_session.commit()

    with respx.mock:
        page = respx.get(url).mock(return_value=httpx.Response(200, text="x"))
        async with fetcher as f:
            result = await f.fetch(db_session, url, "test_src")
        db_session.commit()
        assert not page.called

    assert result.blocked_reason == "learned:student_data_detected"


# --- bot-check interstitials (M1-3 finding) ------------------------------

CAPTCHA = (
    b"<html><body><title>Validation request</title>"
    b"<h3>User validation required to continue..</h3>"
    b'<form action="/captcha_resp"><input name="captcha_resp_txt"/></form>'
    b"</body></html>"
)


async def test_an_interstitial_is_never_stored_as_content(db_session, clean, fetcher):
    """[VERIFIED M1-3] saras.cbse.gov.in serves this after sustained volume.
    Storing it would let a later extraction pass mistake it for the page."""
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        respx.get("https://school.test/detail/1").mock(
            return_value=httpx.Response(200, content=CAPTCHA)
        )
        async with fetcher as f:
            result = await f.fetch(
                db_session, "https://school.test/detail/1", "test_src"
            )
        db_session.commit()

    assert result.interstitial is True
    assert result.content_hash is None
    assert db_session.execute(text("SELECT count(*) FROM raw_documents")).scalar() == 0

    error = db_session.execute(
        text("SELECT error FROM fetches WHERE id = :i"), {"i": result.fetch_id}
    ).scalar()
    assert "interstitial" in error, (
        "we must record that we were refused, not leave a gap"
    )


async def test_the_circuit_breaker_stops_a_walled_domain(db_session, clean, fetcher):
    """Grinding through a queue against a bot wall is useless and impolite."""
    from school_intel.fetch.client import CIRCUIT_BREAKER_THRESHOLD, DomainBlockedError

    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        route = respx.route(host="school.test").mock(
            return_value=httpx.Response(200, content=CAPTCHA)
        )
        async with fetcher as f:
            for i in range(CIRCUIT_BREAKER_THRESHOLD):
                await f.fetch(db_session, f"https://school.test/d/{i}", "test_src")
            calls_before = route.call_count

            with pytest.raises(DomainBlockedError, match="bot checks"):
                await f.fetch(db_session, "https://school.test/d/99", "test_src")
        db_session.commit()

        assert route.call_count == calls_before, "no request may be made once tripped"


async def test_a_good_response_resets_the_breaker(db_session, clean, fetcher):
    """A single transient bot check must not disable a domain for the whole run."""
    with respx.mock:
        respx.get("https://school.test/robots.txt").mock(
            return_value=httpx.Response(
                200, text=ROBOTS_ALLOW, headers={"content-type": "text/plain"}
            )
        )
        respx.get("https://school.test/a").mock(
            return_value=httpx.Response(200, content=CAPTCHA)
        )
        respx.get("https://school.test/b").mock(
            return_value=httpx.Response(200, text="<html>real content</html>")
        )
        async with fetcher as f:
            await f.fetch(db_session, "https://school.test/a", "test_src")
            good = await f.fetch(db_session, "https://school.test/b", "test_src")
            assert not f.domain_is_blocked("https://school.test/c")
        db_session.commit()

    assert good.interstitial is False
    assert good.content_hash is not None


def test_load_returns_none_for_an_unreadable_document(db_session, clean, tmp_path):
    """A stored file that exists but will not open must not raise.

    Real cause: some of the 33,000 school sites we crawl are compromised, the
    antivirus quarantines the page we saved, and the file then fails to open.
    One poisoned document used to abort a whole multi-hour enrichment chunk.
    """
    from school_intel.fetch.store import load

    blocked = tmp_path / "quarantined.bin"
    blocked.write_bytes(b"<html>whatever</html>")
    db_session.execute(
        text(
            "INSERT INTO raw_documents (content_hash, storage_path, media_type,"
            " bytes) VALUES ('deadbeef', :p, 'text/html', 21)"
        ),
        {"p": str(blocked)},
    )
    db_session.commit()

    def boom(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    with mock.patch("pathlib.Path.read_bytes", boom):
        assert load(db_session, "deadbeef") is None
