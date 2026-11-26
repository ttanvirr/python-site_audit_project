from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._") or "website"


def generate_reports(data: dict[str, Any], output_dir: str | Path, template_dir: str | Path) -> Path:
    now = datetime.now().astimezone()
    domain = data.get("website", {}).get("domain", "website")
    folder_name = f"{_safe_filename(domain)}_{now.strftime('%Y-%m-%d_%H%M%S')}"
    target = Path(output_dir).expanduser().resolve() / folder_name
    target.mkdir(parents=True, exist_ok=False)

    # JSON is the complete machine-readable audit record, including provider payloads and limitations.
    (target / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    env = Environment(
        loader=FileSystemLoader(str(Path(template_dir).resolve())),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")
    html = template.render(report=data, generated_at=now.isoformat())
    (target / "report.html").write_text(html, encoding="utf-8")

    with (target / "scores.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "score_out_of_100", "grade", "status", "tests", "passed", "failed", "warnings"])
        for name, category in data.get("categories", {}).items():
            checks = category.get("checks", [])
            writer.writerow([
                name, category.get("score"), category.get("grade"), category.get("status"), len(checks),
                sum(item.get("status") == "pass" for item in checks),
                sum(item.get("status") == "fail" for item in checks),
                sum(item.get("status") == "warning" for item in checks),
            ])
        overall = data.get("overall", {})
        writer.writerow(["Overall", overall.get("score"), overall.get("grade"), overall.get("status"), "", "", "", ""])
    return target
