#!/usr/bin/env python3
"""Reusable CLI website auditor. See README.md for scoring and integration details."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from auditor.audits.geo import run_geo
from auditor.audits.health import run_health
from auditor.audits.optimization import run_optimization
from auditor.audits.responsiveness import run_responsiveness
from auditor.audits.security import run_security
from auditor.audits.seo import run_seo
from auditor.audits.speed import run_speed
from auditor.configuration import apply_scoring_config, load_scoring_config
from auditor.context import build_context
from auditor.models import CategoryResult
from auditor.network import normalize_url
from auditor.reporting.generate import generate_reports
from auditor.scoring import category_result, overall_score
from auditor.services.cache import JsonCache
from auditor.services.external import ExternalAudits
from auditor.services.google import GoogleAuditServices

APP_VERSION = "1.0.0"
LOG = logging.getLogger("site_audit")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a public website and produce HTML, JSON, and CSV reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("url", help="Public website URL (HTTP or HTTPS). Scheme may be omitted; HTTPS is assumed.")
    parser.add_argument("--pages", type=int, default=20, help="Maximum number of same-host HTML pages to crawl.")
    parser.add_argument("--output-dir", default="reports", help="Directory in which timestamped reports are saved.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout in seconds for individual website requests.")
    parser.add_argument("--api-timeout", type=float, default=90.0, help="Timeout in seconds for third-party API requests.")
    parser.add_argument("--cache-dir", default=".site_audit_cache", help="Directory for cached API response JSON.")
    parser.add_argument("--cache-ttl", type=int, default=86400, help="API cache lifetime in seconds.")
    parser.add_argument("--scoring-config", help="Optional JSON file with category/check scoring weights and overrides.")
    parser.add_argument("--enable-paid-apis", action="store_true", help="Explicitly allow potentially billable API calls (DataForSEO and legacy SecurityHeaders).")
    parser.add_argument("--enable-ssl-labs", action="store_true", help="Request a Qualys SSL Labs public TLS assessment (free API; may take time and is subject to service limits).")
    parser.add_argument("--geo-query", action="append", default=[], help="Seed question/topic for the DataForSEO AI visibility query; may be repeated. Requires paid-API opt-in.")
    parser.add_argument("--skip-pagespeed", action="store_true", help="Skip Google PageSpeed Insights requests.")
    parser.add_argument("--skip-playwright", action="store_true", help="Skip Chromium viewport testing.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    parser.add_argument("--version", action="version", version=f"Site Audit {APP_VERSION}")
    return parser.parse_args(argv)


def _limitations(ctx, config: dict, categories: dict[str, CategoryResult], args: argparse.Namespace) -> list[str]:
    limitations: list[str] = []
    if not os.getenv("GOOGLE_PSI_API_KEY"):
        limitations.append("GOOGLE_PSI_API_KEY is not set. PageSpeed Insights was attempted without a key; automated workloads may encounter tighter quota limits.")
    if not os.getenv("GOOGLE_CRUX_API_KEY"):
        limitations.append("CrUX real-user field data was not queried because GOOGLE_CRUX_API_KEY is not configured. Lighthouse lab data is not a substitute for field data.")
    if not args.enable_ssl_labs:
        limitations.append("Qualys SSL Labs API assessment was not initiated. Pass --enable-ssl-labs to request the public TLS scan; the local TLS certificate check still runs.")
    if not os.getenv("SECURITY_HEADERS_API_KEY"):
        limitations.append("SecurityHeaders.com API was not queried. Its API requires an existing key, and its key page currently states that new subscriptions are discontinued; local header checks and MDN Observatory are used instead.")
    if not args.enable_paid_apis:
        limitations.append("Potentially billable integrations were not called. Pass --enable-paid-apis to permit supported paid API requests.")
    elif not (os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD") and args.geo_query):
        limitations.append("DataForSEO AI visibility was not queried: set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD and provide one or more --geo-query values in addition to --enable-paid-apis.")
    if os.getenv("OTTERLY_API_KEY"):
        limitations.append("OTTERLY_API_KEY is present, but this version does not call OtterlyAI: the available public help page describes API capabilities/authentication, while a validated endpoint/request schema was not implemented. No OtterlyAI request was sent.")
    else:
        limitations.append("OtterlyAI actual visibility was not queried because OTTERLY_API_KEY is not configured and this version does not assume unverified endpoints.")
    if not (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and os.getenv("GSC_PROPERTY_URL")):
        limitations.append("Google Search Console was not queried because GOOGLE_APPLICATION_CREDENTIALS and GSC_PROPERTY_URL are not both configured. No keyword rankings or search traffic are inferred.")
    if ctx.fetch_error:
        limitations.append(f"Root page fetch issue: {ctx.fetch_error}")
    if ctx.crawl_errors:
        limitations.append(f"Some crawl targets were skipped or unavailable; see the Site Health JSON details ({len(ctx.crawl_errors)} crawl note(s)).")
    for name, category in categories.items():
        if category.score is None:
            limitations.append(f"{name} score is N/A because no valid scoring measurements were collected for that category.")
    psi_errors = ctx.external_service_results.get("pagespeed", {}).get("errors", {})
    for service, error in psi_errors.items():
        limitations.append(f"{service} unavailable: {error}")
    return limitations


def run_audit(args: argparse.Namespace) -> Path:
    load_dotenv()
    normalized = normalize_url(args.url)
    if args.pages < 1 or args.pages > 500:
        raise ValueError("--pages must be between 1 and 500")
    if args.timeout <= 0 or args.api_timeout <= 0:
        raise ValueError("Timeout values must be greater than zero")

    scoring_config = load_scoring_config(args.scoring_config)
    config = {
        "psi_api_key": os.getenv("GOOGLE_PSI_API_KEY"),
        "crux_api_key": os.getenv("GOOGLE_CRUX_API_KEY"),
        "security_headers_api_key": os.getenv("SECURITY_HEADERS_API_KEY"),
        "dataforseo_login": os.getenv("DATAFORSEO_LOGIN"),
        "dataforseo_password": os.getenv("DATAFORSEO_PASSWORD"),
        "otterly_api_key": os.getenv("OTTERLY_API_KEY"),
        "gsc_credentials_file": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        "gsc_property_url": os.getenv("GSC_PROPERTY_URL"),
        "enable_paid_apis": bool(args.enable_paid_apis),
        "enable_ssl_labs": bool(args.enable_ssl_labs),
        "geo_queries": args.geo_query,
    }

    LOG.info("Starting audit for %s", normalized)
    LOG.info("Preparing site context and crawling up to %s internal pages", args.pages)
    ctx = build_context(normalized, args.pages, args.timeout)
    LOG.info("Site responded: status=%s final_url=%s pages_crawled=%s", ctx.root_status, ctx.final_url, len(ctx.pages))

    categories: dict[str, CategoryResult] = {}
    api_cache = JsonCache(args.cache_dir, args.cache_ttl)
    google = GoogleAuditServices(timeout=args.api_timeout, cache=api_cache)
    external = ExternalAudits(timeout=args.api_timeout, cache=api_cache)

    if args.skip_pagespeed:
        categories["Speed"] = category_result("Speed", [], summary="PageSpeed Insights and CrUX requests were skipped by command-line option.")
        ctx.external_service_results["pagespeed"] = {"summaries": {}, "errors": {"pagespeed": "Skipped by --skip-pagespeed."}}
    else:
        LOG.info("Running Google PageSpeed Insights (mobile and desktop) and optional CrUX field data")
        categories["Speed"] = run_speed(ctx, google, config)

    LOG.info("Running site health checks, internal crawl, TLS certificate and W3C validation")
    categories["Site Health"] = run_health(ctx, external, args.timeout)
    ctx.external_service_results["site_health"] = categories["Site Health"].raw

    if args.skip_playwright:
        categories["Responsiveness"] = category_result("Responsiveness", [], summary="Playwright viewport checks were skipped by command-line option.")
    else:
        LOG.info("Running Playwright Chromium viewport checks")
        categories["Responsiveness"] = run_responsiveness(ctx, args.timeout)

    LOG.info("Scoring resource and asset optimization")
    categories["Optimization"] = run_optimization(ctx)
    LOG.info("Running response-header and external security checks")
    categories["Security"] = run_security(ctx, external, config)
    LOG.info("Running technical SEO checks")
    categories["SEO"] = run_seo(ctx, external, config)
    LOG.info("Running GEO technical-readiness checks")
    categories["GEO"] = run_geo(ctx, external, config)

    apply_scoring_config(categories, scoring_config)
    overall = overall_score(categories, scoring_config.get("category_weights"))
    website = {
        "requested_url": normalized,
        "final_url": ctx.final_url,
        "domain": ctx.hostname,
        "audit_date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "http_status": ctx.root_status,
        "response_time_ms": ctx.root_elapsed_ms,
        "pages_crawled": len(ctx.pages),
        "redirects": ctx.root_redirects,
    }
    data = {
        "application": {"name": "Python Website Audit & Scoring Tool", "version": APP_VERSION},
        "website": website,
        "overall": overall,
        "categories": {name: category.to_dict() for name, category in categories.items()},
        "crawl": {
            "robots_url": ctx.robots_url,
            "robots_error": ctx.robots_error,
            "sitemap_url": ctx.sitemap_url,
            "sitemap_status": ctx.sitemap_status,
            "sitemap_urls": ctx.sitemap_urls,
            "pages": [{"url": p.url, "status_code": p.status_code, "title": p.title, "description": p.description, "word_count": p.word_count, "internal_links": p.internal_links, "external_links": p.external_links} for p in ctx.pages],
            "broken_internal_links": ctx.broken_internal_links,
            "resource_urls_checked": ctx.resource_urls,
            "broken_resources": ctx.broken_resources,
        },
        "limitations": _limitations(ctx, config, categories, args),
        "scoring_configuration": scoring_config,
    }

    target = generate_reports(data, args.output_dir, Path(__file__).parent / "templates")
    LOG.info("Audit finished. Overall: %s (%s), coverage: %s%%", overall["score"], overall["grade"], overall["coverage_percent"])
    return target


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        report_dir = run_audit(args)
    except KeyboardInterrupt:
        LOG.error("Audit interrupted by user.")
        return 130
    except Exception as exc:
        LOG.exception("Audit failed: %s", exc)
        return 2
    print(f"\nAudit report generated: {report_dir}")
    print(f"HTML: {report_dir / 'report.html'}")
    print(f"JSON: {report_dir / 'report.json'}")
    print(f"CSV:  {report_dir / 'scores.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
