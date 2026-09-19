"""France24 malformed RSS recovery without changes to other feeds."""

import datetime
import hashlib
from pathlib import Path
from unittest.mock import patch

import httpx

from news_digest.pipeline import FetchReport, _collect_candidates
from news_digest.sources.registry import SOURCES

NOW = datetime.datetime(2026, 7, 26, 12, tzinfo=datetime.UTC)
BAD = b'<?xml version="1.0"?><rss><channel><item'
GOOD = (Path(__file__).parent.parent / "fixtures/feeds/france24.xml").read_bytes()
FRANCE24 = next(source for source in SOURCES if source.key == "france24")
BBC = next(source for source in SOURCES if source.key == "bbc")


def _collect(source, bodies, monkeypatch):
    requests = []
    slept = []
    responses = iter(bodies)

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, content=next(responses), headers={"content-type": "text/html"})

    monkeypatch.setattr("news_digest.pipeline.time.sleep", slept.append)
    report = FetchReport()
    with patch("news_digest.sources.http.assert_public_host", lambda host: None):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            candidates = _collect_candidates(client, (source,), NOW, 24, report, False)
    return candidates, report, requests, slept


def test_france24_recovers_after_two_bad_feed_responses(monkeypatch):
    candidates, report, requests, slept = _collect(
        FRANCE24, [BAD, BAD, GOOD], monkeypatch
    )
    assert candidates
    assert report.per_source[FRANCE24.name].startswith("正常")
    assert len(requests) == 3
    assert slept == [120, 300]
    diag = report.diagnostics["france24"]
    assert diag["feed_attempts"] == 3
    assert diag["parse_warning"] is False
    assert diag["raw"] > 0
    assert len(diag["feed_bad_samples"]) == 2
    assert diag["feed_bad_samples"][0] == {
        "attempt": 1,
        "bytes": len(BAD),
        "sha256": hashlib.sha256(BAD).hexdigest(),
        "content_type": "text/html",
        "prefix": BAD.decode(),
    }


def test_france24_failure_after_bounded_retries(monkeypatch):
    candidates, report, requests, slept = _collect(FRANCE24, [BAD] * 3, monkeypatch)
    assert not candidates
    assert report.per_source[FRANCE24.name] == "失败：FEED_PARSE_EMPTY"
    assert len(requests) == 3
    assert slept == [120, 300]
    assert len(report.diagnostics["france24"]["feed_bad_samples"]) == 3


def test_other_source_does_not_retry(monkeypatch):
    candidates, report, requests, slept = _collect(BBC, [BAD], monkeypatch)
    assert not candidates
    assert len(requests) == 1
    assert slept == []
    assert report.per_source[BBC.name] == "失败：FEED_PARSE_EMPTY"
    assert "feed_bad_samples" not in report.diagnostics["bbc"]


def test_france24_good_first_attempt_never_sleeps(monkeypatch):
    candidates, report, requests, slept = _collect(FRANCE24, [GOOD], monkeypatch)
    assert candidates
    assert len(requests) == 1
    assert slept == []
    assert report.diagnostics["france24"]["feed_attempts"] == 1
    assert "feed_bad_samples" not in report.diagnostics["france24"]

