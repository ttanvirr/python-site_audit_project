from __future__ import annotations

import time
from typing import Any

import requests

from auditor.network import api_request
from auditor.services.cache import JsonCache


def extract_dataforseo_domain_citations(payload: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    """Extract citations to a domain from the documented DataForSEO LLM Mentions response."""
    from urllib.parse import urlsplit

    target_host = domain.lower().removeprefix("www.")
    citations: list[dict[str, Any]] = []
    for task in payload.get("tasks", []):
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                for source in item.get("sources") or []:
                    citation_url = str(source.get("url") or "")
                    citation_host = (urlsplit(citation_url).hostname or "").lower().removeprefix("www.")
                    if not target_host or not (citation_host == target_host or citation_host.endswith("." + target_host)):
                        continue
                    citations.append({
                        "question": item.get("question"),
                        "answer_excerpt": str(item.get("answer") or "")[:1200],
                        "citation_url": citation_url,
                        "citation_domain": source.get("domain") or citation_host,
                        "source_title": source.get("title"),
                        "source_rank": source.get("rank"),
                        "platform": item.get("platform"),
                        "model_name": item.get("model_name"),
                        "ai_search_volume": item.get("ai_search_volume"),
                    })
    return citations


def hostname_cache_key(domain: str, queries: list[str]) -> str:
    return domain.lower() + ":" + "|".join(queries)


class ExternalAudits:
    def __init__(self, timeout: float = 30.0, cache: JsonCache | None = None):
        self.timeout = timeout
        self.cache = cache or JsonCache()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "SiteAudit/1.0 (technical website audit)"})

    def observatory(self, hostname: str) -> dict[str, Any]:
        cache_key = f"mdn-observatory:{hostname}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["_cached"] = True
            return cached
        response = api_request(
            self.session, "POST", "https://observatory-api.mdn.mozilla.net/api/v2/scan",
            params={"host": hostname}, timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict) or ("score" not in payload and "error" not in payload):
            raise ValueError("MDN HTTP Observatory returned an unexpected response")
        self.cache.set(cache_key, payload)
        payload["_cached"] = False
        return payload

    def nu_validator(self, html: str, document_url: str) -> dict[str, Any]:
        import hashlib
        cache_key = "w3c-nu:" + hashlib.sha256((document_url + html).encode("utf-8")).hexdigest()
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        response = api_request(
            self.session, "POST", "https://validator.w3.org/nu/", params={"out": "json"},
            data=html.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Location": document_url},
            timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload.get("messages"), list):
            raise ValueError("W3C Nu Checker returned an unexpected response")
        self.cache.set(cache_key, payload)
        return payload

    def securityheaders(self, url: str, api_key: str) -> dict[str, Any]:
        cache_key = f"securityheaders:{url}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        response = api_request(
            self.session, "GET", "https://api.securityheaders.com/",
            params={"q": url, "hide": "on", "followRedirects": "on"},
            headers={"x-api-key": api_key}, timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("SecurityHeaders API returned an unexpected response")
        self.cache.set(cache_key, payload)
        return payload

    def ssl_labs(self, hostname: str, *, max_polls: int = 5, poll_seconds: int = 15) -> dict[str, Any]:
        """Start/check a public SSL Labs assessment; a new scan can take much longer."""
        cache_key = f"ssllabs:{hostname}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        endpoint = "https://api.ssllabs.com/api/v4/analyze"
        params = {"host": hostname, "publish": "off", "all": "done", "ignoreMismatch": "on"}
        response = api_request(self.session, "GET", endpoint, params=params, timeout=self.timeout)
        payload = response.json()
        for _ in range(max_polls):
            if payload.get("status") in {"READY", "ERROR"}:
                self.cache.set(cache_key, payload)
                return payload
            if payload.get("status") not in {"DNS", "IN_PROGRESS"}:
                return payload
            time.sleep(poll_seconds)
            response = api_request(self.session, "GET", endpoint, params=params, timeout=self.timeout)
            payload = response.json()
        return payload

    def search_console(self, credentials_file: str, property_url: str) -> dict[str, Any]:
        """Read-only Search Console aggregate for the previous 28 complete days."""
        from datetime import date, timedelta
        from urllib.parse import quote

        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=27)
        cache_key = f"gsc:{property_url}:{start_date.isoformat()}:{end_date.isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        import google.auth.transport.requests
        from google.oauth2 import service_account

        scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]
        credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
        credentials.refresh(google.auth.transport.requests.Request())
        endpoint = "https://www.googleapis.com/webmasters/v3/sites/" + quote(property_url, safe="") + "/searchAnalytics/query"
        response = api_request(
            self.session, "POST", endpoint,
            headers={"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"},
            json={"startDate": start_date.isoformat(), "endDate": end_date.isoformat(), "dataState": "final", "rowLimit": 1},
            timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Google Search Console returned an unexpected response")
        rows = payload.get("rows") or []
        result = {
            "property": property_url,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "summary": rows[0] if rows else None,
            "note": "Search Console data is property-authorized and aggregated over the previous 28 complete days.",
        }
        self.cache.set(cache_key, result)
        return result

    def dataforseo_mentions(self, login: str, password: str, queries: list[str], domain: str) -> dict[str, Any]:
        """Paid live LLM Mentions request; caller must gate this behind explicit CLI opt-in."""
        cache_key = "dataforseo:" + hostname_cache_key(domain, queries)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if not queries:
            raise ValueError("At least one --geo-query is required for DataForSEO")
        # The documented endpoint allows up to ten target entities in one task.
        targets = [{
            "domain": domain.lower().removeprefix("www."),
            "search_filter": "include",
            "search_scope": ["sources"],
            "include_subdomains": True,
        }]
        targets.extend({
            "keyword": query,
            "search_filter": "include",
            "search_scope": ["question"],
            "match_type": "word_match",
        } for query in queries[:9])
        body = [{
            "target": targets,
            "platform": "chat_gpt",
            "location_code": 2840,
            "language_code": "en",
            "limit": 50,
        }]
        response = api_request(
            self.session, "POST", "https://api.dataforseo.com/v3/ai_optimization/llm_mentions/search_mentions/live",
            json=body, auth=(login, password), timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise ValueError("DataForSEO returned an unexpected response")
        global_status = payload.get("status_code")
        if isinstance(global_status, int) and global_status >= 40000:
            raise ValueError(f"DataForSEO API error {global_status}: {payload.get('status_message', 'Unknown error')}")
        if not payload["tasks"]:
            raise ValueError("DataForSEO returned no task results")
        task_errors = [
            f"{task.get('status_code')}: {task.get('status_message')}"
            for task in payload["tasks"]
            if isinstance(task.get("status_code"), int) and task.get("status_code") >= 40000
        ]
        if task_errors:
            raise ValueError("DataForSEO task error: " + "; ".join(task_errors))
        citations = extract_dataforseo_domain_citations(payload, domain)
        result = {
            "raw": payload,
            "domain_citations": citations,
            "query_count": len(queries[:9]),
            "location": "United States",
            "language": "English",
            "platform": "ChatGPT",
            "note": "The ChatGPT dataset is currently restricted to United States/English in this provider API.",
        }
        self.cache.set(cache_key, result)
        return result
