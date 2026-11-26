from __future__ import annotations

from collections.abc import Iterable

from auditor.models import CategoryResult, Check

GRADE_BANDS = [
    (90, "Excellent"),
    (80, "Very Good"),
    (70, "Good"),
    (60, "Needs Improvement"),
    (40, "Poor"),
    (0, "Critical"),
]


def grade_for(score: float | None) -> str:
    if score is None:
        return "N/A"
    for lower_bound, label in GRADE_BANDS:
        if score >= lower_bound:
            return label
    return "Critical"


def score_checks(checks: Iterable[Check]) -> float | None:
    """Weighted mean of explicit numeric check scores; N/A checks are excluded."""
    numerator = 0.0
    denominator = 0.0
    for check in checks:
        if check.score is None or check.status == "na" or check.weight <= 0:
            continue
        numerator += max(0.0, min(100.0, check.score)) * check.weight
        denominator += check.weight
    return round(numerator / denominator, 1) if denominator else None


def category_result(
    name: str,
    checks: list[Check],
    *,
    summary: str | None = None,
    raw: dict | None = None,
    recommendations: list[str] | None = None,
) -> CategoryResult:
    score = score_checks(checks)
    findings = []
    for check in checks:
        if check.status in {"fail", "warning"}:
            findings.append({
                "title": check.name,
                "priority": check.priority,
                "what": check.message,
                "why": check.expected,
                "how_to_fix": check.recommendation,
                "source": check.source,
                "source_url": check.source_url,
            })
    if summary is None:
        summary = "No measurable checks were completed." if score is None else f"{sum(c.status == 'pass' for c in checks)} passed; {sum(c.status == 'fail' for c in checks)} failed; {sum(c.status == 'warning' for c in checks)} warnings."
    status = "N/A" if score is None else "Measured"
    return CategoryResult(
        name=name,
        score=score,
        grade=grade_for(score),
        status=status,
        summary=summary,
        checks=checks,
        findings=findings,
        recommendations=recommendations or [],
        raw=raw or {},
    )


def overall_score(categories: dict[str, CategoryResult], category_weights: dict[str, float] | None = None) -> dict:
    category_weights = category_weights or {}
    weighted = [(item.score, float(category_weights.get(name, 1.0))) for name, item in categories.items() if item.score is not None and float(category_weights.get(name, 1.0)) > 0]
    measured = [score for score, _ in weighted]
    coverage = round(100 * len(measured) / 7, 1)
    denominator = sum(weight for _, weight in weighted)
    score = round(sum(value * weight for value, weight in weighted) / denominator, 1) if denominator else None
    return {
        "score": score,
        "grade": grade_for(score),
        "coverage_percent": coverage,
        "measured_categories": len(measured),
        "total_categories": 7,
        "status": "Provisional" if len(measured) < 7 else "Complete",
    }
