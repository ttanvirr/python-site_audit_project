import pytest

from auditor.network import UnsafeTargetError, assert_public_host, normalize_url
from auditor.services.google import extract_crux_metrics, extract_psi_summary


def test_normalize_url_assumes_https_and_drops_fragment():
    assert normalize_url("example.com/path#top") == "https://example.com/path"
    assert normalize_url("http://example.com") == "http://example.com/"


def test_normalize_url_rejects_unsafe_schemes_and_credentials():
    with pytest.raises(ValueError):
        normalize_url("ftp://example.com")
    with pytest.raises(ValueError):
        normalize_url("https://user:pass@example.com")


def test_private_ip_is_rejected():
    with pytest.raises(UnsafeTargetError):
        assert_public_host("127.0.0.1")


def test_extract_psi_summary():
    payload = {
        "lighthouseResult": {
            "configSettings": {"formFactor": "mobile"},
            "categories": {"performance": {"score": 0.82}, "seo": {"score": 0.95}},
            "audits": {"largest-contentful-paint": {"numericValue": 2200, "displayValue": "2.2 s", "score": 0.9}},
            "lighthouseVersion": "12.0.0",
        }
    }
    summary = extract_psi_summary(payload)
    assert summary["performance_score"] == 82
    assert summary["seo_score"] == 95
    assert summary["metrics"]["largest_contentful_paint"]["display"] == "2.2 s"


def test_extract_crux_metrics():
    payload = {"record": {"collectionPeriod": {"firstDate": {"year": 2026}}, "metrics": {"largest_contentful_paint": {"percentiles": {"p75": 2400}, "category": "FAST"}}}}
    summary = extract_crux_metrics(payload)
    assert summary["metrics"]["largest_contentful_paint"]["p75"] == 2400


def test_dataforseo_citations_match_exact_domain_or_subdomain_only():
    from auditor.services.external import extract_dataforseo_domain_citations

    payload = {
        "tasks": [{
            "result": [{
                "items": [{
                    "question": "Which provider should I use?",
                    "answer": "A sample answer.",
                    "platform": "chat_gpt",
                    "sources": [
                        {"url": "https://www.example.com/services", "title": "Example"},
                        {"url": "https://docs.example.com/guide", "title": "Docs"},
                        {"url": "https://notexample.com/page", "title": "Unrelated"},
                    ],
                }]
            }]
        }]
    }
    citations = extract_dataforseo_domain_citations(payload, "example.com")
    assert [item["citation_url"] for item in citations] == [
        "https://www.example.com/services",
        "https://docs.example.com/guide",
    ]


def test_api_request_retries_server_errors(monkeypatch):
    from auditor.network import api_request

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}
            self.closed = False

        def close(self):
            self.closed = True

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(f"HTTP {self.status_code}")

    class FakeSession:
        def __init__(self):
            self.responses = [FakeResponse(503), FakeResponse(200)]
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return self.responses.pop(0)

    session = FakeSession()
    monkeypatch.setattr("auditor.network.time.sleep", lambda *_: None)
    response = api_request(session, "GET", "https://example.com", attempts=2)
    assert response.status_code == 200
    assert session.calls == 2


def test_api_request_can_preserve_target_site_http_error_status(monkeypatch):
    from auditor.network import api_request

    class FakeResponse:
        status_code = 404
        headers = {}

        def raise_for_status(self):
            raise AssertionError("raise_for_status must not be called")

    class FakeSession:
        def request(self, *args, **kwargs):
            return FakeResponse()

    response = api_request(
        FakeSession(), "GET", "https://example.com/missing", attempts=1,
        raise_for_status=False,
    )
    assert response.status_code == 404


def test_safe_http_client_requests_http_status_without_raising(monkeypatch):
    from auditor import network

    class FakeResponse:
        status_code = 503
        headers = {}
        url = "https://example.com/"

    captured = {}

    def fake_api_request(session, method, url, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(network, "assert_public_host", lambda *_args, **_kwargs: ["93.184.216.34"])
    monkeypatch.setattr(network, "api_request", fake_api_request)
    result = network.SafeHttpClient().get("https://example.com/")

    assert result.response.status_code == 503
    assert captured["raise_for_status"] is False
