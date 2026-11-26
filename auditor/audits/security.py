from __future__ import annotations

from urllib.parse import urljoin

from auditor.checks import measured_check
from auditor.context import SiteContext
from auditor.models import Check, CategoryResult
from auditor.scoring import category_result
from auditor.services.external import ExternalAudits


OBSERVATORY_GRADE_SCORES = {
    "A+": 100, "A": 95, "A-": 90,
    "B+": 85, "B": 80, "B-": 75,
    "C+": 70, "C": 65, "C-": 60,
    "D+": 55, "D": 50, "D-": 45, "F": 20,
}
SSL_LABS_GRADE_SCORES = {**OBSERVATORY_GRADE_SCORES, "T": 10, "M": 10}

HEADER_POLICIES = [
    ("strict-transport-security", "Strict-Transport-Security", 1.5, "Enable HSTS on HTTPS with a suitable max-age; consider includeSubDomains only when all subdomains support HTTPS."),
    ("content-security-policy", "Content-Security-Policy", 2.0, "Deploy a restrictive Content-Security-Policy and progressively remove unsafe-inline/unsafe-eval where feasible."),
    ("x-content-type-options", "X-Content-Type-Options", 0.75, "Set X-Content-Type-Options: nosniff."),
    ("referrer-policy", "Referrer-Policy", 0.75, "Set a privacy-conscious Referrer-Policy such as strict-origin-when-cross-origin."),
    ("permissions-policy", "Permissions-Policy", 0.5, "Restrict browser features that the site does not need with a Permissions-Policy."),
]


def run_security(ctx: SiteContext, external: ExternalAudits, config: dict) -> CategoryResult:
    checks: list[Check] = []
    raw: dict = {"response_headers": ctx.root_headers}
    headers = {k.lower(): v for k, v in ctx.root_headers.items()}
    response_observed = ctx.root_status is not None
    https = response_observed and ctx.final_url.lower().startswith("https://")
    checks.append(measured_check("HTTPS enforcement", https if response_observed else None, value=ctx.final_url if response_observed else None, expected="HTTPS final URL", weight=1.5, source="Python HTTP check", priority="Critical", message="The site does not finish on HTTPS." if response_observed and not https else "The page is served over HTTPS." if https else "HTTPS could not be verified because no response was collected.", recommendation="Redirect all HTTP requests to HTTPS and update internal links." if response_observed and not https else None))

    certificate = ctx.external_service_results.get("site_health", {}).get("tls_certificate")
    if https and certificate:
        days = certificate.get("days_remaining")
        valid = days is not None and days >= 0
        checks.append(measured_check("TLS certificate validity", valid, value={"days_remaining": days, "not_after": certificate.get("not_after")}, expected="Valid certificate", weight=1.25, source="Python ssl (Site Health audit)", priority="Critical", message="TLS certificate is invalid or expired." if not valid else "TLS certificate validates successfully.", recommendation="Renew the certificate and verify its certificate chain." if not valid else None))
    elif https:
        checks.append(measured_check("TLS certificate validity", None, source="Python ssl (Site Health audit)"))

    for header, label, weight, recommendation in HEADER_POLICIES:
        value = headers.get(header)
        present = bool(value)
        if not response_observed:
            checks.append(measured_check(label, None, value=None, expected="Header present with a valid restrictive policy", source="HTTP response headers"))
            continue
        # HSTS is meaningful only on HTTPS; X-Content-Type-Options must use nosniff.
        if header == "strict-transport-security" and not https:
            checks.append(measured_check(label, None, value=value, expected="HSTS served on HTTPS", source="HTTP response headers"))
            continue
        valid = present and (header != "x-content-type-options" or value.strip().lower() == "nosniff")
        checks.append(measured_check(
            label, valid, value=value, expected="Header present with a valid restrictive policy", weight=weight,
            source="HTTP response headers", source_url="https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers",
            message=f"{label} is missing or has an invalid value." if not valid else f"{label} is present.",
            priority="High" if header in {"content-security-policy", "strict-transport-security"} else "Medium",
            recommendation=recommendation if not valid else None,
        ))

    csp = headers.get("content-security-policy", "").lower()
    x_frame_options = headers.get("x-frame-options", "").strip().lower()
    xfo_valid = x_frame_options in {"deny", "sameorigin"}
    csp_frame_ancestors = "frame-ancestors" in csp
    frame_control = xfo_valid or csp_frame_ancestors
    checks.append(measured_check(
        "Clickjacking protection", frame_control if response_observed else None,
        value={"x-frame-options": headers.get("x-frame-options"), "x_frame_options_valid": xfo_valid, "csp_frame_ancestors": csp_frame_ancestors},
        expected="X-Frame-Options or CSP frame-ancestors", weight=1.0, source="HTTP response headers",
        message="Neither X-Frame-Options nor CSP frame-ancestors was detected." if not frame_control else "A frame-embedding policy is present.",
        priority="High", recommendation="Set Content-Security-Policy: frame-ancestors 'self' (or 'none') or an appropriate X-Frame-Options header." if not frame_control else None,
    ))
    mixed = []
    if https:
        for page in ctx.pages:
            for tag in page.soup.find_all(src=True):
                src = str(tag.get("src", ""))
                if src.startswith("http://"):
                    mixed.append({"page": page.url, "resource": src})
            for tag in page.soup.find_all(href=True):
                href = str(tag.get("href", ""))
                rel = tag.get("rel") or []
                if href.startswith("http://") and any(item in rel for item in ("stylesheet", "preconnect", "icon")):
                    mixed.append({"page": page.url, "resource": href})
    checks.append(measured_check(
        "Mixed content references", len(mixed) == 0 if response_observed else None, value=mixed[:30] if response_observed else None, expected="No insecure HTTP subresources on HTTPS pages", weight=1.0,
        source="Crawled HTML source inspection", message=f"Found {len(mixed)} HTTP subresource reference(s) in the crawled HTTPS pages." if mixed else "No insecure mixed-content references were found in the crawled HTTPS pages.",
        priority="High", recommendation="Change HTTP asset references to HTTPS or remove the insecure dependency." if mixed else None,
    ))

    try:
        observatory = external.observatory(ctx.hostname)
        raw["mdn_http_observatory"] = observatory
        observatory_grade = str(observatory.get("grade") or "").upper()
        normalized = OBSERVATORY_GRADE_SCORES.get(observatory_grade)
        if normalized is not None:
            checks.append(Check(
                name="MDN HTTP Observatory score", status="pass" if normalized >= 90 else "warning" if normalized >= 60 else "fail",
                value={"provider_score": observatory.get("score"), "provider_grade": observatory_grade, "normalized_score": normalized, "tests_passed": observatory.get("tests_passed"), "tests_failed": observatory.get("tests_failed")},
                expected="Normalized 90/100 or higher", message=f"MDN HTTP Observatory grade: {observatory.get('grade')}.",
                source="MDN HTTP Observatory API v2", source_url=observatory.get("details_url") or "https://developer.mozilla.org/en-US/observatory",
                weight=2.0, score=float(normalized), priority="High" if normalized < 60 else "Medium",
                recommendation="Open the MDN Observatory report and address failed HTTP security tests." if normalized < 90 else None,
            ))
        else:
            checks.append(measured_check("MDN HTTP Observatory score", None, value=observatory, source="MDN HTTP Observatory API v2"))
    except Exception as exc:
        raw["mdn_http_observatory_error"] = f"{type(exc).__name__}: {exc}"
        checks.append(measured_check("MDN HTTP Observatory score", None, value=raw["mdn_http_observatory_error"], source="MDN HTTP Observatory API v2", source_url="https://developer.mozilla.org/en-US/observatory"))

    # A legacy API key is only used after explicit paid-API opt-in.
    if config.get("enable_paid_apis") and config.get("security_headers_api_key"):
        try:
            result = external.securityheaders(ctx.final_url, config["security_headers_api_key"])
            raw["securityheaders_api"] = result
            grade = (result.get("summary") or {}).get("grade")
            if grade:
                grade_scores = {"A+": 100, "A": 95, "A-": 90, "B+": 85, "B": 80, "B-": 75, "C+": 70, "C": 65, "C-": 60, "D+": 55, "D": 50, "D-": 45, "E": 35, "F": 15}
                score = grade_scores.get(str(grade).upper())
                if score is not None:
                    checks.append(Check(name="SecurityHeaders.com grade", status="pass" if score >= 90 else "warning" if score >= 60 else "fail", value=grade, expected="A or higher", message=f"SecurityHeaders.com returned grade {grade}.", source="SecurityHeaders.com API", source_url="https://securityheaders.com/api/docs/", weight=1.5, score=float(score), recommendation="Review the SecurityHeaders.com result and implement missing/weak response headers." if score < 90 else None))
                else:
                    checks.append(measured_check("SecurityHeaders.com grade", None, value=grade, source="SecurityHeaders.com API"))
        except Exception as exc:
            raw["securityheaders_api_error"] = f"{type(exc).__name__}: {exc}"
    elif config.get("security_headers_api_key"):
        raw["securityheaders_api_note"] = "Key found, but API calls were skipped because --enable-paid-apis was not provided."

    if config.get("enable_ssl_labs"):
        try:
            ssl_labs = external.ssl_labs(ctx.hostname, max_polls=2, poll_seconds=15)
            raw["ssl_labs"] = ssl_labs
            endpoints = ssl_labs.get("endpoints") or []
            grades = [endpoint.get("grade") for endpoint in endpoints if endpoint.get("grade")]
            if grades:
                score_values = [SSL_LABS_GRADE_SCORES[grade] for grade in grades if grade in SSL_LABS_GRADE_SCORES]
                if score_values:
                    score = min(score_values)
                    checks.append(Check(name="Qualys SSL Labs endpoint grade", status="pass" if score >= 90 else "warning" if score >= 60 else "fail", value=grades, expected="A or higher on all assessed endpoints", message=f"SSL Labs endpoint grades: {', '.join(grades)}.", source="Qualys SSL Labs API v4", source_url="https://www.ssllabs.com/ssltest/analyze.html?d=" + ctx.hostname, weight=2.0, score=float(score), priority="High", recommendation="Review TLS protocol, cipher, key exchange, and certificate-chain findings in SSL Labs." if score < 90 else None))
                else:
                    checks.append(measured_check("Qualys SSL Labs endpoint grade", None, value=grades, source="Qualys SSL Labs API v4"))
            else:
                checks.append(measured_check("Qualys SSL Labs endpoint grade", None, value=ssl_labs.get("statusMessage") or ssl_labs.get("status"), source="Qualys SSL Labs API v4"))
        except Exception as exc:
            raw["ssl_labs_error"] = f"{type(exc).__name__}: {exc}"
            checks.append(measured_check("Qualys SSL Labs endpoint grade", None, value=raw["ssl_labs_error"], source="Qualys SSL Labs API v4"))

    return category_result(
        "Security", checks,
        summary="Local security-header checks are supplemented by MDN HTTP Observatory; TLS scoring from SSL Labs is optional.",
        raw=raw,
        recommendations=["Add a carefully tested Content-Security-Policy and HSTS policy.", "Keep TLS certificates valid and use SSL Labs for a deeper TLS configuration assessment.", "Review frame protections, MIME sniffing, referrer, and permissions policies."],
    )
