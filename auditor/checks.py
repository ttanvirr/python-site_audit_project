from __future__ import annotations

from auditor.models import Check


def measured_check(
    name: str,
    passed: bool | None,
    value=None,
    expected=None,
    *,
    message: str = "",
    source: str = "Python technical check",
    source_url: str | None = None,
    weight: float = 1.0,
    warning_score: float = 50.0,
    pass_score: float = 100.0,
    fail_score: float = 0.0,
    warning: bool = False,
    priority: str = "Medium",
    recommendation: str | None = None,
) -> Check:
    if passed is None:
        status, score = "na", None
    elif passed:
        status, score = "pass", pass_score
    elif warning:
        status, score = "warning", warning_score
    else:
        status, score = "fail", fail_score
    return Check(
        name=name,
        status=status,
        value=value,
        expected=expected,
        message=message,
        source=source,
        source_url=source_url,
        weight=weight,
        score=score,
        priority=priority,
        recommendation=recommendation,
    )


def metric_check(name: str, value: float | int | None, good_max: float, poor_max: float, unit: str, *, weight=0.5, source="CrUX field data") -> Check:
    if value is None:
        return measured_check(name, None, expected=f"≤ {good_max} {unit}", weight=weight, source=source)
    if value <= good_max:
        return measured_check(name, True, value=f"{value} {unit}", expected=f"≤ {good_max} {unit}", weight=weight, source=source, message=f"{name} is within the recommended threshold.")
    if value <= poor_max:
        return measured_check(name, False, value=f"{value} {unit}", expected=f"≤ {good_max} {unit}", weight=weight, warning=True, warning_score=50, source=source, message=f"{name} is above the recommended threshold.", priority="Medium", recommendation=f"Investigate and improve {name} to meet the recommended threshold.")
    return measured_check(name, False, value=f"{value} {unit}", expected=f"≤ {good_max} {unit}", weight=weight, source=source, message=f"{name} is in the poor range.", priority="High", recommendation=f"Prioritize work to reduce {name} below {good_max} {unit}.")
