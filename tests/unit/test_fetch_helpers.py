"""Pure helpers from the fetch layer. No network, no database."""

from pathlib import Path

import pytest

from school_intel.fetch.client import normalize_url, registrable_domain
from school_intel.fetch.store import content_hash, storage_path


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.vvi.edu.in/a", "vvi.edu.in"),
        ("https://nal.kvs.ac.in/x", "kvs.ac.in"),
        ("https://mgrly.kvs.ac.in/x", "kvs.ac.in"),
        ("https://saras.cbse.gov.in/SARAS", "cbse.gov.in"),
        ("https://school.co.in/a", "school.co.in"),
        ("https://rcis.in/a", "rcis.in"),
        ("https://sub.example.com/a", "example.com"),
    ],
)
def test_registrable_domain(url, expected):
    """Two KV subdomains must share one rate-limit bucket, and 'kvs.ac.in' must
    not be truncated to 'ac.in'."""
    assert registrable_domain(url) == expected


def test_normalize_url_encodes_spaces():
    """[VERIFIED M0-0] Real fee links contain spaces: '/pdf/md/C1.FEE STRUCTURE.pdf'.
    httpx raises on those rather than encoding them."""
    got = normalize_url("https://www.tosss.edu.in/pdf/md/C1.FEE STRUCTURE.pdf")
    assert " " not in got
    assert got.endswith("C1.FEE%20STRUCTURE.pdf")


def test_normalize_url_leaves_clean_urls_alone():
    url = "https://school.edu.in/mandatory-disclosure?year=2025"
    assert normalize_url(url) == url


def test_content_hash_is_sha256_and_stable():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert len(content_hash(b"abc")) == 64
    assert content_hash(b"abc") != content_hash(b"abd")


def test_storage_path_fans_out_and_keeps_pdf_suffix():
    digest = "9f2a" + "c" * 60
    html = storage_path(Path("/raw"), digest, "text/html")
    pdf = storage_path(Path("/raw"), digest, "application/pdf")
    assert html.parts[-3:-1] == ("9f", "2a")
    assert pdf.suffix == ".pdf"
    assert html.suffix == ".bin"
