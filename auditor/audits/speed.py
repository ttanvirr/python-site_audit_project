from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from auditor.checks import measured_check, metric_check
from auditor.context import SiteContext
from auditor.models import Check, CategoryResult
from auditor.scoring import category_result
from auditor.services.google import GoogleAuditServices, extract_crux_metrics, extract_psi_summary

LOG = logging.getLogger(__name__)


def run_speed(ctx: SiteContext, services: GoogleAuditServices, config: dict) -> CategoryResult:
    psi_payloads = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(services.pagespeed, ctx.final_url, strategy, config.get("psi_api_key")): strategy
            for strategy in ("mobile", "desktop")
        }
        for future in as_completed(futures):
            strategy = futures[future]
            try:
                psi_payloads[strategy] = future.result()
            except Exception as exc:
                errors[strategy] = f"{type(exc).__name__}: {exc}"
                LOG.warning("PageSpeed Insights (%s) unavailable: %s", strategy, exc)

    psi_summaries = {strategy: extract_psi_summary(payload) for strategy, payload in psi_payloads.items()}
    ctx.external_service_results["pagespeed"] = {
        "mobile": psi_payloads.get("mobile"),
        "desktop": psi_payloads.get("desktop"),
        "summaries": psi_summaries,
        "errors": errors,
    }
    checks: list[Check] = []
    for strategy in ("mobile", "desktop"):
        summary = psi_summaries.get(strategy)
        if summary and summary.get("performance_score") is not None:
            score = summary["performance_score"]
            checks.append(Check(
                name=f"Lighthouse Performance ({strategy})",
                status="pass" if score >= 90 else "warning" if score >= 50 else "fail",
                value=f"{score}/100", expected="≥ 90/100", message=f"Lighthouse {strategy} lab performance score.",
                source="Google PageSpeed Insights / Lighthouse", source_url="https://pagespeed.web.dev/analysis",
                weight=4.0, score=score, priority="High" if score < 50 else "Medium",
                recommendation="Review Lighthouse opportunities and Core Web Vitals to improve loading and interaction performance." if score < 90 else None,
            ))
        else:
            checks.append(measured_check(f"Lighthouse Performance ({strategy})", None, source="Google PageSpeed Insights", source_url="https://developers.google.com/speed/docs/insights/rest"))
        if summary:
            for metric_name, metric in summary.get("metrics", {}).items():
                checks.append(Check(
                    name=f"{strategy.title()} {metric_name.replace('_', ' ').upper()}",
                    status="info", value=metric.get("display") or metric.get("value"),
                    expected="See Core Web Vitals guidance", message="Laboratory diagnostic value (not an additional score input).",
                    source="Google Lighthouse", source_url="https://web.dev/articles/vitals", weight=0, score=None,
                ))

    crux_results = {}
    if config.get("crux_api_key"):
        for form_factor, label in (("PHONE", "mobile"), ("DESKTOP", "desktop")):
            try:
                payload = services.crux(ctx.origin, form_factor, config["crux_api_key"])
                crux_results[label] = extract_crux_metrics(payload)
            except Exception as exc:
                errors[f"crux_{label}"] = f"{type(exc).__name__}: {exc}"
                LOG.warning("CrUX (%s) unavailable: %s", label, exc)
    else:
        errors["crux"] = "GOOGLE_CRUX_API_KEY is not configured; real-user field data was not queried."

    thresholds = {
        "largest_contentful_paint": (2500, 4000, "ms"),
        "interaction_to_next_paint": (200, 500, "ms"),
        "cumulative_layout_shift": (0.1, 0.25, "score"),
        "first_contentful_paint": (1800, 3000, "ms"),
        "experimental_time_to_first_byte": (800, 1800, "ms"),
    }
    for form_factor, data in crux_results.items():
        for metric_name, metric_data in data.get("metrics", {}).items():
            if metric_name not in thresholds:
                continue
            good, poor, unit = thresholds[metric_name]
            p75 = metric_data.get("p75")
            checks.append(metric_check(f"CrUX {form_factor.title()} {metric_name.replace('_', ' ').upper()} (p75)", p75, good, poor, unit, weight=1.0))

    raw = {"pagespeed": {k: v for k, v in psi_summaries.items()}, "crux": crux_results, "errors": errors}
    return category_result(
        "Speed", checks,
        summary="Lighthouse lab scores are reported separately from CrUX real-user field measurements.",
        raw=raw,
        recommendations=[
            "Prioritize Largest Contentful Paint, Interaction to Next Paint, and Cumulative Layout Shift improvements.",
            "Use separate mobile and desktop lab results; field data represents real Chrome users and is only available when CrUX has sufficient traffic data.",
        ],
    )
