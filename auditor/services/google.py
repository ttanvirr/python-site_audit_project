from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from auditor.services.cache import JsonCache
from auditor.network import api_request

PSI_ENDPOINT = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"


class GoogleAuditServices:
    def __init__(self, timeout: float = 90.0, cache: JsonCache | None = None):
        self.timeout = timeout
        self.cache = cache or JsonCache()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "SiteAudit/1.0"})

    def pagespeed(self, url: str, strategy: str, api_key: str | None = None) -> dict[str, Any]:
        key = f"psi:{url}:{strategy}"
        cached = self.cache.get(key)
        if cached is not None:
            cached["_cached"] = True
            return cached
        params: dict[str, Any] = {"url": url, "strategy": strategy, "category": ["PERFORMANCE", "SEO"]}
        if api_key:
            params["key"] = api_key
        response = api_request(self.session, "GET", PSI_ENDPOINT, params=params, timeout=self.timeout)
        payload = response.json()
        if not isinstance(payload, dict) or "lighthouseResult" not in payload:
            raise ValueError("PageSpeed Insights returned an unexpected response")
        self.cache.set(key, payload)
        payload["_cached"] = False
        return payload

    def crux(self, origin: str, form_factor: str, api_key: str) -> dict[str, Any]:
        key = f"crux:{origin}:{form_factor}"
        cached = self.cache.get(key)
        if cached is not None:
            cached["_cached"] = True
            return cached
        metrics = ["largest_contentful_paint", "interaction_to_next_paint", "cumulative_layout_shift", "first_contentful_paint", "experimental_time_to_first_byte"]
        response = api_request(
            self.session, "POST", f"{CRUX_ENDPOINT}?key={quote(api_key)}",
            json={"origin": origin, "formFactor": form_factor, "metrics": metrics},
            timeout=self.timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict) or "record" not in payload:
            raise ValueError("CrUX API returned an unexpected response")
        self.cache.set(key, payload)
        payload["_cached"] = False
        return payload


def extract_psi_summary(payload: dict[str, Any]) -> dict[str, Any]:
    lighthouse = payload.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})
    metric_ids = {
        "first_contentful_paint": "first-contentful-paint",
        "largest_contentful_paint": "largest-contentful-paint",
        "cumulative_layout_shift": "cumulative-layout-shift",
        "speed_index": "speed-index",
        "total_blocking_time": "total-blocking-time",
        "time_to_first_byte": "server-response-time",
        "interaction_to_next_paint": "interaction-to-next-paint",
    }
    metrics = {}
    for metric, audit_id in metric_ids.items():
        audit = audits.get(audit_id, {})
        if audit:
            metrics[metric] = {"value": audit.get("numericValue"), "display": audit.get("displayValue"), "score": audit.get("score"), "unit": audit.get("numericUnit")}
    performance_score = categories.get("performance", {}).get("score")
    seo_score = categories.get("seo", {}).get("score")
    return {
        "strategy": lighthouse.get("configSettings", {}).get("formFactor"),
        "performance_score": round(performance_score * 100, 1) if isinstance(performance_score, (int, float)) else None,
        "seo_score": round(seo_score * 100, 1) if isinstance(seo_score, (int, float)) else None,
        "metrics": metrics,
        "audits": audits,
        "lighthouse_version": lighthouse.get("lighthouseVersion"),
        "final_url": lighthouse.get("finalDisplayedUrl"),
        "fetch_time": lighthouse.get("fetchTime"),
        "cached": payload.get("_cached", False),
    }


def extract_crux_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    record = payload.get("record", {})
    metrics = record.get("metrics", {})
    output = {}
    for metric_name, metric_payload in metrics.items():
        output[metric_name] = {
            "p75": metric_payload.get("percentiles", {}).get("p75"),
            "histogram": metric_payload.get("histogram"),
            "category": metric_payload.get("category"),
        }
    return {"collection_period": record.get("collectionPeriod"), "metrics": output, "cached": payload.get("_cached", False)}
