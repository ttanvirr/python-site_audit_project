from auditor.models import Check, CategoryResult
from auditor.scoring import category_result, grade_for, overall_score, score_checks


def test_grade_bands():
    assert grade_for(95) == "Excellent"
    assert grade_for(85) == "Very Good"
    assert grade_for(75) == "Good"
    assert grade_for(65) == "Needs Improvement"
    assert grade_for(45) == "Poor"
    assert grade_for(20) == "Critical"
    assert grade_for(None) == "N/A"


def test_weighted_score_excludes_na_and_info():
    checks = [
        Check("pass", "pass", weight=2, score=100),
        Check("warning", "warning", weight=1, score=50),
        Check("na", "na", weight=100, score=None),
        Check("info", "info", weight=100, score=None),
    ]
    assert score_checks(checks) == 83.3


def test_category_score_and_findings():
    result = category_result("SEO", [
        Check("missing title", "fail", score=0, message="No title", recommendation="Add one"),
        Check("canonical", "pass", score=100),
    ])
    assert result.score == 50
    assert result.grade == "Poor"
    assert result.findings[0]["title"] == "missing title"


def test_overall_is_equal_weight_and_provisional_with_na():
    categories = {
        "Speed": CategoryResult("Speed", 80, "Very Good", "Measured", ""),
        "SEO": CategoryResult("SEO", 60, "Needs Improvement", "Measured", ""),
        "GEO": CategoryResult("GEO", None, "N/A", "N/A", ""),
    }
    overall = overall_score(categories)
    assert overall["score"] == 70
    assert overall["coverage_percent"] == 28.6
    assert overall["status"] == "Provisional"
