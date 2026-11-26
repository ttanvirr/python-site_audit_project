from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auditor.models import CategoryResult
from auditor.scoring import grade_for, score_checks

DEFAULT_SCORING = {
    "category_weights": {
        "Speed": 1, "Responsiveness": 1, "Optimization": 1, "Site Health": 1,
        "Security": 1, "SEO": 1, "GEO": 1,
    },
    "check_weight_multipliers": {},
    "check_overrides": {},
}


def load_scoring_config(path: str | None) -> dict[str, Any]:
    if not path:
        return DEFAULT_SCORING.copy()
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Scoring config must be a JSON object")
    merged = DEFAULT_SCORING.copy()
    for key in merged:
        if key in config:
            if not isinstance(config[key], dict):
                raise ValueError(f"Scoring config field '{key}' must be an object")
            merged[key] = {**merged[key], **config[key]}
    return merged


def apply_scoring_config(categories: dict[str, CategoryResult], config: dict[str, Any]) -> None:
    multipliers = config.get("check_weight_multipliers", {})
    overrides = config.get("check_overrides", {})
    for category in categories.values():
        for check in category.checks:
            if check.name in multipliers:
                check.weight *= float(multipliers[check.name])
            override = overrides.get(check.name, {})
            if isinstance(override, dict):
                if "weight" in override:
                    check.weight = float(override["weight"])
                if check.status in override:
                    check.score = float(override[check.status])
        category.score = score_checks(category.checks)
        category.grade = grade_for(category.score)
        category.status = "N/A" if category.score is None else "Measured"
