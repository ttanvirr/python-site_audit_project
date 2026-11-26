from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Check:
    name: str
    status: str  # pass, fail, warning, info, na
    value: Any = None
    expected: Any = None
    message: str = ""
    source: str = "Python technical check"
    source_url: str | None = None
    weight: float = 1.0
    score: float | None = None
    priority: str = "Low"
    recommendation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CategoryResult:
    name: str
    score: float | None
    grade: str
    status: str
    summary: str
    checks: list[Check] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "grade": self.grade,
            "status": self.status,
            "summary": self.summary,
            "checks": [item.to_dict() for item in self.checks],
            "findings": self.findings,
            "recommendations": self.recommendations,
            "raw": self.raw,
        }
