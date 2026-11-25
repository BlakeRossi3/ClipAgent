"""Public-domain clip sources from the Internet Archive (``public_domain_archive``).

The archive.org advancedsearch payload below is a recorded-shape fixture written from the documented
format (``/advancedsearch.php?...&output=json``), not captured live.
"""

from __future__ import annotations

from typing import Any

import pytest

import edit
import fetch
import pick
import scout
import strategist
import strategy as strategy_mod
import upload
from fetch import LICENCE_PUBLIC_DOMAIN, check_licence

PD_MARK = "http://creativecommons.org/publicdomain/mark/1.0/"
CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
OLD_PD = "http://creativecommons.org/licenses/publicdomain/"


@pytest.fixture
def pd_strategy(strategy):
    """The shipped strategy with the Internet Archive source type switched on."""
    topics = list(strategy["topics"]) + [{"query": "atomic energy film", "area": "science",
                                          "source_types": ["public_domain_archive"]}]
    return strategy.updated(source_types=[*strategy.source_types, "public_domain_archive"], topics=topics)


def ia_info(**over: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": "AtomicEnergy1950", "title": "A Is for Atom",
        "webpage_url": "https://archive.org/details/AtomicEnergy1950",
        "extractor_key": "ArchiveOrg", "extractor": "archive.org",
        "uploader": "prelinger@archive.org", "license": PD_MARK,
        "description": "Animated film about atomic energy, 1953.", "duration": 840.0,
    }
    info.update(over)
    return info


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------

def test_strategy_knows_the_type_and_allows_any_uploader(pd_strategy):
    assert "public_domain_archive" in strategy_mod.KNOWN_SOURCE_TYPES
    assert "public_domain_archive" in strategy_mod.ANY_CHANNEL_SOURCE_TYPES
    assert strategy_mod.validate_strategy(pd_strategy) == []
    ok, _ = strategy_mod.is_publisher_allowed(pd_strategy, "public_domain_archive", channel="anyone",
                                              url="https://archive.org/details/x")
    assert ok
    ok, _ = strategy_mod.is_publisher_allowed(pd_strategy, "public_domain_archive", channel="anyone",
                                              url="https://www.youtube.com/watch?v=abc")
    assert not ok


def test_shipped_strategy_leaves_it_off_until_approved(strategy):
    # Turning on a new source type is a major change the owner approves; the shipped file only
    # carries the allowlist entry ready for it.
    assert "public_domain_archive" not in strategy.source_types
    assert strategy["allowlist"]["public_domain_archive"]["domains"] == ["archive.org"]


def test_strategist_refuses_non_archive_domains(strategy):
    allow = dict(strategy["allowlist"])
    allow["public_domain_archive"] = {"any_channel": True, "channels": [], "domains": ["archive.org", "vimeo.com"]}
    problems = strategist.licence_problems(strategy, {**strategy, "allowlist": allow})
    assert any("public_domain_archive" in p for p in problems)


# ---------------------------------------------------------------------------
# licence gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lic", [PD_MARK, CC0, OLD_PD])
def test_public_domain_marks_pass(pd_strategy, lic):
    d = check_licence(ia_info(license=lic), pd_strategy, "public_domain_archive")
    assert d.ok and d.licence == LICENCE_PUBLIC_DOMAIN, d.reason
    assert d.evidence["license"] == lic


@pytest.mark.parametrize("lic,needle", [
    (None, "no public-domain"),
    ("http://creativecommons.org/licenses/by-nc/3.0/", "not a public-domain"),
    ("http://creativecommons.org/licenses/by/4.0/", "not a public-domain"),
])
def test_other_licences_rejected(pd_strategy, lic, needle):
    d = check_licence(ia_info(license=lic), pd_strategy, "public_domain_archive")
    assert not d.ok and needle in d.reason


def test_must_be_on_archive_org(pd_strategy):
    d = check_licence(ia_info(webpage_url="https://example.com/v.mp4"), pd_strategy, "public_domain_archive")
    assert not d.ok


def test_copyright_notice_in_description_rejects(pd_strategy):
    d = check_licence(ia_info(description="Copyright 2019 Some Studio. All rights reserved."),
                      pd_strategy, "public_domain_archive")
    assert not d.ok


def test_type_off_in_strategy_rejects(strategy):
    d = check_licence(ia_info(), strategy, "public_domain_archive")
    assert not d.ok and "not enabled" in d.reason


def test_stored_licence_url_fills_a_missing_yt_dlp_field(make_source):
    import db
    sid = make_source(status="candidate", source_type="public_domain_archive",
                      url="https://archive.org/details/AtomicEnergy1950",
                      metadata={"licenseurl": PD_MARK})
    info = fetch.licence_info(ia_info(license=None), db.require_source(sid))
    assert info["license"] == PD_MARK
    # yt-dlp's own value always wins
    info = fetch.licence_info(ia_info(license=CC0), db.require_source(sid))
    assert info["license"] == CC0


# ---------------------------------------------------------------------------
# scout
# ---------------------------------------------------------------------------

IA_SEARCH = {"response": {"numFound": 3, "docs": [
    {"identifier": "AtomicEnergy1950", "title": "A Is for Atom", "licenseurl": PD_MARK,
     "creator": "General Electric", "date": "1953-01-01T00:00:00Z", "description": "Animated film."},
    {"identifier": "SomeNCFilm", "title": "NC film", "licenseurl": "http://creativecommons.org/licenses/by-nc/3.0/"},
    {"identifier": "CopyFilm", "title": "Bootleg", "licenseurl": CC0,
     "description": ["Copyright 2020 Big Studio. All rights reserved."]},
]}}


def test_archive_searcher(monkeypatch, pd_strategy):
    calls = []

    def http(url, params=None):
        calls.append((url, params))
        assert url == scout.ARCHIVE_SEARCH_API
        return IA_SEARCH

    monkeypatch.setattr(scout, "http_get_json", http)
    cands = scout.SEARCHERS["public_domain_archive"]("atomic energy", 5, pd_strategy)
    assert [c.external_id for c in cands] == ["AtomicEnergy1950"]
    c = cands[0]
    assert c.url == "https://archive.org/details/AtomicEnergy1950"
    assert c.platform == "archive_org" and c.source_type == "public_domain_archive"
    assert c.metadata["licenseurl"] == PD_MARK and c.metadata["licence_verified"] is False
    assert c.published_at.startswith("1953-01-01")
    q = calls[0][1]["q"]
    assert "atomic energy" in q and "mediatype:(movies)" in q and "publicdomain" in q


# ---------------------------------------------------------------------------
# downstream readers: credit, description licence text, labels
# ---------------------------------------------------------------------------

def test_credit_and_upload_text(make_source):
    import db
    sid = make_source(status="picked", source_type="public_domain_archive", channel="Prelinger Archives",
                      licence=LICENCE_PUBLIC_DOMAIN)
    assert pick.credit_line(db.require_source(sid)) == "Video: Prelinger Archives (public domain)"
    assert LICENCE_PUBLIC_DOMAIN in upload.LICENCE_TEXT
    assert edit._LICENCE_LABEL[LICENCE_PUBLIC_DOMAIN] == "public domain"
