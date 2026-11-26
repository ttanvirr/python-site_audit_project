from __future__ import annotations

import logging
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

from auditor.checks import measured_check
from auditor.context import SiteContext
from auditor.models import Check, CategoryResult
from auditor.network import assert_public_host
from auditor.scoring import category_result
from auditor.services.external import ExternalAudits

LOG = logging.getLogger(__name__)


def _certificate_details(hostname: str, port: int = 443, timeout: float = 7.0) -> dict:
    context = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=hostname) as secure_socket:
            cert = secure_socket.getpeercert()
            not_after = cert.get("notAfter")
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc) if not_after else None
            return {
                "subject": cert.get("subject"),
                "issuer": cert.get("issuer"),
                "not_after": expires.isoformat() if expires else None,
                "days_remaining": (expires - datetime.now(timezone.utc)).days if expires else None,
                "subject_alt_names": cert.get("subjectAltName", []),
            }


def run_health(ctx: SiteContext, external: ExternalAudits, timeout: float) -> CategoryResult:
    checks: list[Check] = []
    raw: dict = {"requested_url": ctx.requested_url, "final_url": ctx.final_url, "redirects": ctx.root_redirects, "pages_crawled": len(ctx.pages), "broken_internal_links": ctx.broken_internal_links, "resource_urls_checked": ctx.resource_urls, "broken_resources": ctx.broken_resources}

    response_observed = ctx.root_status is not None
    try:
        addresses = assert_public_host(ctx.hostname, urlsplit(ctx.final_url).port)
        raw["dns_addresses"] = addresses
        checks.append(measured_check("DNS resolution", bool(addresses), value=addresses, expected="At least one public DNS address", weight=1.5, source="Python socket/DNS", message="Hostname resolved successfully." if addresses else "No DNS addresses were returned." , recommendation="Check authoritative DNS records if this failure is reproducible from another network." if not addresses else None, priority="Critical"))
    except Exception as exc:
        raw["dns_error"] = str(exc)
        checks.append(measured_check("DNS resolution", None, value=f"Could not resolve from this audit environment: {exc}", expected="Resolvable hostname", weight=1.5, source="Python socket/DNS"))

    available = response_observed and 200 <= ctx.root_status < 400
    checks.append(measured_check("Website availability", available if response_observed else None, value=ctx.root_status if response_observed else ctx.fetch_error, expected="HTTP 2xx or 3xx", weight=2.0, source="Python requests", message="Root page could not be reached successfully." if response_observed and not available else "Root page responded successfully." if available else "No response could be collected from this audit environment.", priority="Critical", recommendation="Verify hosting, DNS, origin server health, firewall rules, and application logs." if response_observed and not available else None))
    status_ok = ctx.root_status is not None and 200 <= ctx.root_status < 300
    checks.append(measured_check("HTTP response status", status_ok if response_observed else None, value=ctx.root_status, expected="HTTP 200–299", weight=1.5, source="Python requests", message=f"Root page returned HTTP {ctx.root_status}." if response_observed and not status_ok else "Root page returned a successful 2xx response." if status_ok else "HTTP status unavailable because the root page could not be fetched.", warning=ctx.root_status is not None and 300 <= ctx.root_status < 400, warning_score=65, priority="High", recommendation="Resolve persistent 4xx/5xx responses and unnecessary redirect chains." if response_observed and not status_ok else None))

    if ctx.root_elapsed_ms is not None:
        latency_ok = ctx.root_elapsed_ms <= 1000
        latency_warning = 1000 < ctx.root_elapsed_ms <= 2500
        checks.append(measured_check("Root document response time", latency_ok, value=f"{ctx.root_elapsed_ms:.0f} ms", expected="≤ 1,000 ms", weight=1.0, source="Python requests timing", warning=latency_warning, warning_score=60, message=f"Root document response took {ctx.root_elapsed_ms:.0f} ms.", recommendation="Investigate server processing time, caching, hosting capacity, and backend queries." if not latency_ok else None, priority="Medium"))
    else:
        checks.append(measured_check("Root document response time", None, source="Python requests timing"))

    checks.append(measured_check("Redirect chain", len(ctx.root_redirects) <= 1 if response_observed else None, value=ctx.root_redirects, expected="0–1 redirect(s)", weight=0.75, source="Python requests", warning=len(ctx.root_redirects) > 1, warning_score=60, message=f"Observed {len(ctx.root_redirects)} redirect(s).", recommendation="Update internal links and canonical URLs to point directly to the final destination." if len(ctx.root_redirects) > 1 else None))
    is_https = response_observed and urlsplit(ctx.final_url).scheme == "https"
    checks.append(measured_check("HTTPS enabled", is_https if response_observed else None, value=ctx.final_url if response_observed else None, expected="Final page served over HTTPS", weight=2.0, source="Python URL/HTTP check", priority="Critical", message="Final page is not served over HTTPS." if response_observed and not is_https else "Final page uses HTTPS." if is_https else "HTTPS could not be verified without an HTTP response.", recommendation="Install a valid TLS certificate and redirect HTTP traffic to HTTPS." if response_observed and not is_https else None))

    if is_https:
        try:
            certificate = _certificate_details(ctx.hostname, urlsplit(ctx.final_url).port or 443, timeout=min(timeout, 7.0))
            raw["tls_certificate"] = certificate
            days = certificate.get("days_remaining")
            valid = days is not None and days >= 0
            checks.append(measured_check("TLS certificate validity", valid, value={"days_remaining": days, "not_after": certificate.get("not_after")}, expected="Valid certificate with no expiry", weight=1.5, source="Python ssl", priority="Critical", message="TLS certificate is expired or could not be validated." if not valid else "TLS certificate validates successfully.", recommendation="Renew or replace the TLS certificate and verify its complete certificate chain." if not valid else None))
            checks.append(measured_check("TLS certificate expiry window", days is not None and days > 30, value=f"{days} days remaining" if days is not None else None, expected="> 30 days remaining", weight=0.75, source="Python ssl", warning=days is not None and 0 <= days <= 30, warning_score=40, message=f"Certificate expires in {days} days." if days is not None and days <= 30 else "Certificate has more than 30 days remaining.", priority="High", recommendation="Schedule certificate renewal before it enters the 30-day expiry window." if days is not None and days <= 30 else None))
        except Exception as exc:
            raw["tls_error"] = f"{type(exc).__name__}: {exc}"
            checks.append(measured_check("TLS certificate validity", False, value=raw["tls_error"], expected="Valid TLS certificate", weight=1.5, source="Python ssl", priority="Critical", message=f"TLS validation failed: {exc}", recommendation="Inspect the certificate chain, hostname match, expiry, and trusted issuer."))
    else:
        checks.append(measured_check("TLS certificate validity", None, expected="Valid TLS certificate", source="Python ssl"))
        checks.append(measured_check("TLS certificate expiry window", None, source="Python ssl"))

    robots_ok = ctx.robots_parser is not None
    checks.append(measured_check("robots.txt availability", robots_ok if response_observed else None, value={"url": ctx.robots_url, "error": ctx.robots_error}, expected="HTTP 200 with parseable directives", weight=0.75, source="Python requests + RobotFileParser", warning=not robots_ok and response_observed, warning_score=65, message="robots.txt was not available as a successful response." if response_observed and not robots_ok else "robots.txt was fetched and parsed." if robots_ok else "robots.txt could not be checked because the site request failed.", recommendation="Publish a valid robots.txt file with intentional crawler rules and sitemap declaration." if response_observed and not robots_ok else None))
    sitemap_ok = ctx.sitemap_status == 200 and bool(ctx.sitemap_urls)
    checks.append(measured_check("XML sitemap availability", sitemap_ok if response_observed else None, value={"url": ctx.sitemap_url, "http_status": ctx.sitemap_status, "url_count": len(ctx.sitemap_urls)}, expected="Accessible XML sitemap with at least one <loc>", weight=0.75, source="Python requests + BeautifulSoup XML", warning=(ctx.sitemap_status == 404 or ctx.sitemap_status is None) and response_observed, warning_score=60, message="No accessible sitemap with URLs was found." if response_observed and not sitemap_ok else f"Sitemap parsed with {len(ctx.sitemap_urls)} URL(s)." if sitemap_ok else "Sitemap could not be checked because the site request failed.", recommendation="Publish an XML sitemap, submit it to search engines, and reference it from robots.txt." if response_observed and not sitemap_ok else None))

    broken_count = len(ctx.broken_internal_links)
    broken_score = 100 if broken_count == 0 else 70 if broken_count <= 2 else 40 if broken_count <= 5 else 0
    if ctx.pages:
        checks.append(Check(name="Broken/internal page errors", status="pass" if broken_count == 0 else "warning" if broken_count <= 2 else "fail", value=ctx.broken_internal_links, expected="No internal 4xx/5xx or inaccessible pages in the crawl", message=f"{broken_count} internal page(s) failed during the limited crawl.", source=f"Python crawl (up to {len(ctx.pages)} pages)", weight=2, score=broken_score, priority="High" if broken_count > 2 else "Medium", recommendation="Fix internal links, restore missing pages, or update links to valid destinations." if broken_count else None))
    else:
        checks.append(measured_check("Broken/internal page errors", None, value=ctx.broken_internal_links, expected="Internal crawl completed", source="Python crawl"))

    if ctx.pages:
        resource_count = len(ctx.broken_resources)
        resource_score = 100 if resource_count == 0 else 60 if resource_count <= 2 else 0
        checks.append(Check(name="Missing/inaccessible same-host resources", status="pass" if resource_count == 0 else "warning" if resource_count <= 2 else "fail", value=ctx.broken_resources, expected="No 4xx/5xx or inaccessible same-host root-page resources (sample up to 30)", message=f"{resource_count} same-host resource(s) failed in the checked sample.", source="Python resource check (same-host only)", weight=1.0, score=resource_score, priority="High" if resource_count > 2 else "Medium", recommendation="Restore missing assets or update page references to valid same-host resource URLs." if resource_count else None))
    else:
        checks.append(measured_check("Missing/inaccessible same-host resources", None, value=ctx.broken_resources, source="Python resource check"))

    if ctx.root_html:
        try:
            validation = external.nu_validator(ctx.root_html, ctx.final_url)
            ctx.w3c_result = validation
            messages = validation.get("messages", [])
            errors = sum(1 for item in messages if item.get("type") == "error")
            warnings = sum(1 for item in messages if item.get("type") == "warning")
            raw["w3c_validation"] = {"error_count": errors, "warning_count": warnings, "messages": messages[:50], "source": "W3C Nu HTML Checker"}
            score = 100 if errors == 0 else max(0, 100 - min(100, errors * 5))
            checks.append(Check(name="W3C HTML validation", status="pass" if errors == 0 else "warning" if errors <= 3 else "fail", value={"errors": errors, "warnings": warnings}, expected="0 validation errors", message=f"W3C Nu Checker found {errors} error(s) and {warnings} warning(s).", source="W3C Nu HTML Checker", source_url="https://validator.w3.org/nu/", weight=1.0, score=score, priority="Medium", recommendation="Review and fix W3C Nu Checker errors in the reported source locations." if errors else None))
        except Exception as exc:
            raw["w3c_error"] = f"{type(exc).__name__}: {exc}"
            checks.append(measured_check("W3C HTML validation", None, value=raw["w3c_error"], source="W3C Nu HTML Checker", source_url="https://validator.w3.org/nu/"))
    else:
        checks.append(measured_check("W3C HTML validation", None, expected="HTML document available", source="W3C Nu HTML Checker"))

    raw["crawl_errors"] = ctx.crawl_errors
    return category_result(
        "Site Health", checks,
        summary=f"Crawled {len(ctx.pages)} internal HTML page(s); found {broken_count} inaccessible internal target(s).",
        raw=raw,
        recommendations=["Keep robots.txt and XML sitemap accessible and synchronized with canonical URLs.", "Review crawl findings and the W3C HTML Checker output before deployment."],
    )
