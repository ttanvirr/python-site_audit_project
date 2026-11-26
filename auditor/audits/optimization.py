from __future__ import annotations

from auditor.checks import measured_check
from auditor.context import SiteContext
from auditor.models import Check, CategoryResult
from auditor.scoring import category_result

LIGHTHOUSE_OPTIMIZATION_AUDITS = {
    "modern-image-formats": 1.5,
    "uses-optimized-images": 1.5,
    "uses-responsive-images": 1.0,
    "offscreen-images": 0.75,
    "uses-long-cache-ttl": 1.0,
    "uses-text-compression": 1.25,
    "render-blocking-resources": 1.25,
    "unminified-css": 0.75,
    "unminified-javascript": 0.75,
    "unused-css-rules": 1.0,
    "unused-javascript": 1.0,
    "total-byte-weight": 1.0,
    "third-party-summary": 0.75,
    "dom-size": 0.5,
}


def run_optimization(ctx: SiteContext) -> CategoryResult:
    checks: list[Check] = []
    raw: dict = {"lighthouse_audits": {}, "resource_inventory": {}}
    pagespeed = ctx.external_service_results.get("pagespeed", {})
    summaries = pagespeed.get("summaries", {})
    for strategy in ("mobile", "desktop"):
        summary = summaries.get(strategy)
        audits = summary.get("audits", {}) if summary else {}
        for audit_id, weight in LIGHTHOUSE_OPTIMIZATION_AUDITS.items():
            audit = audits.get(audit_id)
            if not audit or audit.get("score") is None:
                continue
            numeric_score = audit.get("score")
            normalized = round(float(numeric_score) * 100, 1)
            label = audit.get("title") or audit_id.replace("-", " ").title()
            passed = normalized >= 90
            warning = 50 <= normalized < 90
            description = audit.get("description", "")
            checks.append(Check(
                name=f"{strategy.title()}: {label}",
                status="pass" if passed else "warning" if warning else "fail",
                value=audit.get("displayValue") or f"{normalized}/100",
                expected="Lighthouse audit score ≥ 90/100",
                message=description[:500] if not passed else f"{label} passed the Lighthouse audit.",
                source="Google Lighthouse", source_url="https://developer.chrome.com/docs/lighthouse/overview/",
                weight=weight, score=normalized,
                priority="High" if normalized < 50 else "Medium",
                recommendation=f"Review Lighthouse: {label}." if not passed else None,
            ))
            raw["lighthouse_audits"].setdefault(strategy, {})[audit_id] = {
                "title": label, "score": normalized, "display_value": audit.get("displayValue"),
                "description": description, "details": audit.get("details"),
            }

    if ctx.root_soup is not None:
        soup = ctx.root_soup
        images = soup.find_all("img")
        images_lazy = sum(1 for image in images if str(image.get("loading", "")).lower() == "lazy")
        scripts = soup.find_all("script", src=True)
        styles = [link for link in soup.find_all("link", href=True) if "stylesheet" in (link.get("rel") or [])]
        resource_inventory = {
            "images": len(images), "images_lazy": images_lazy,
            "external_scripts": len(scripts), "stylesheets": len(styles),
            "html_bytes": len(ctx.root_html.encode("utf-8")),
        }
        raw["resource_inventory"] = resource_inventory
        if images:
            ratio = images_lazy / len(images)
            checks.append(measured_check(
                "Below-the-fold image lazy-loading coverage (page source heuristic)", ratio >= 0.5,
                value=f"{images_lazy}/{len(images)} images use loading=lazy", expected="At least 50% of image elements use native lazy-loading when appropriate",
                weight=0.75, source="HTML resource inspection", warning=ratio < 0.5, warning_score=65,
                message="Few images declare native lazy loading; this is a source-level heuristic and does not establish image position in the viewport.",
                recommendation="Add loading=lazy to below-the-fold images; keep the likely LCP/hero image eager.", priority="Low",
            ))
        checks.append(measured_check(
            "HTML document transfer size", resource_inventory["html_bytes"] <= 500_000,
            value=f"{resource_inventory['html_bytes']:,} bytes", expected="≤ 500 KB for HTML response", weight=0.75,
            source="Python HTTP response", warning=500_000 < resource_inventory["html_bytes"] <= 1_000_000, warning_score=60,
            message="The HTML document is large before external assets are counted." if resource_inventory["html_bytes"] > 500_000 else "HTML response size is within the heuristic budget.",
            recommendation="Reduce HTML payload size, repeated markup, and embedded data where possible." if resource_inventory["html_bytes"] > 500_000 else None,
        ))
    else:
        checks.append(measured_check("HTML resource inventory", None, source="Python HTML inspection"))

    return category_result(
        "Optimization", checks,
        summary="Optimization measures asset and resource implementation separately from Lighthouse loading performance.",
        raw=raw,
        recommendations=["Convert large raster images to WebP/AVIF and serve responsive dimensions.", "Enable compression and long-lived cache headers for versioned static assets.", "Reduce unused JavaScript/CSS and render-blocking resources."],
    )
