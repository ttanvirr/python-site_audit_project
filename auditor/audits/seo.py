from __future__ import annotations

import json
import re
from collections import Counter

from auditor.checks import measured_check
from auditor.context import SiteContext, PageData
from auditor.models import Check, CategoryResult
from auditor.scoring import category_result


def _robots_noindex(page: PageData) -> bool:
    robots = page.soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    googlebot = page.soup.find("meta", attrs={"name": re.compile(r"^googlebot$", re.I)})
    values = []
    if robots:
        values.append(str(robots.get("content", "")))
    if googlebot:
        values.append(str(googlebot.get("content", "")))
    x_robots = page.headers.get("x-robots-tag", "")
    values.append(x_robots)
    return any("noindex" in item.lower() for item in values)


def _jsonld_objects(soup) -> list[dict]:
    found = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                found.extend(item for item in node["@graph"] if isinstance(item, dict))
            found.append(node)
    return found


def run_seo(ctx: SiteContext, external=None, config: dict | None = None) -> CategoryResult:
    config = config or {}
    checks: list[Check] = []
    pages = ctx.pages
    root = next((page for page in pages if page.url == ctx.final_url), pages[0] if pages else None)
    if root is None:
        checks.append(measured_check("HTML/SEO page inspection", None, source="Python HTML parser"))
        return category_result("SEO", checks, summary="The page could not be crawled, so on-page SEO checks are unavailable.")

    pagespeed = ctx.external_service_results.get("pagespeed", {}).get("summaries", {})
    for strategy in ("mobile", "desktop"):
        score = pagespeed.get(strategy, {}).get("seo_score")
        checks.append(Check(
            name=f"Lighthouse SEO ({strategy})",
            status="na" if score is None else "pass" if score >= 90 else "warning" if score >= 50 else "fail",
            value=f"{score}/100" if score is not None else None,
            expected="≥ 90/100", message="Lighthouse SEO category score.",
            source="Google PageSpeed Insights / Lighthouse", source_url="https://pagespeed.web.dev/analysis",
            weight=2.0, score=score,
            recommendation="Review the Lighthouse SEO audits and fix the failed checks." if score is not None and score < 90 else None,
            priority="High" if score is not None and score < 50 else "Medium",
        ))

    titles = [page.title.strip() for page in pages]
    descriptions = [page.description.strip() for page in pages]
    checks.append(measured_check(
        "Page title", bool(root.title), value=root.title or None, expected="A unique, descriptive title on each indexable page", weight=1.5,
        source="HTML meta inspection", message="The root page has no title element." if not root.title else "The root page has a title element.",
        priority="High", recommendation="Add a concise, unique <title> describing the page's primary subject." if not root.title else None,
    ))
    title_length_ok = 15 <= len(root.title) <= 60
    checks.append(measured_check(
        "Page title length", title_length_ok, value=f"{len(root.title)} characters", expected="15–60 characters (heuristic)", weight=0.5,
        source="HTML meta inspection", warning=bool(root.title) and not title_length_ok, warning_score=70,
        message="Title length is outside the display-oriented heuristic range." if not title_length_ok else "Title length is within the heuristic range.",
        recommendation="Write a concise title that accurately describes the page; avoid keyword stuffing." if not title_length_ok else None,
        priority="Low",
    ))
    checks.append(measured_check(
        "Meta description", bool(root.description), value=root.description or None, expected="A descriptive meta description on each important page", weight=1.25,
        source="HTML meta inspection", message="The root page has no meta description." if not root.description else "The root page has a meta description.",
        priority="Medium", recommendation="Add a useful, unique meta description summarizing the page's value." if not root.description else None,
    ))
    desc_length_ok = 70 <= len(root.description) <= 160
    checks.append(measured_check(
        "Meta description length", desc_length_ok, value=f"{len(root.description)} characters", expected="70–160 characters (heuristic)", weight=0.5,
        source="HTML meta inspection", warning=bool(root.description) and not desc_length_ok, warning_score=70,
        message="Meta description length is outside the heuristic range." if not desc_length_ok else "Meta description length is within the heuristic range.",
        recommendation="Make the description concise and useful; treat the character range as a writing heuristic, not a ranking rule." if not desc_length_ok else None,
        priority="Low",
    ))
    h1_ok = root.h1_count == 1
    checks.append(measured_check(
        "H1 heading count", h1_ok, value=root.h1_count, expected="One descriptive H1 per page (recommended)", weight=1.0,
        source="HTML heading inspection", warning=root.h1_count != 1, warning_score=65,
        message=f"Root page contains {root.h1_count} H1 element(s)." if not h1_ok else "Root page contains one H1.",
        recommendation="Use one clear primary H1 and organize subsections with H2/H3 headings." if not h1_ok else None,
    ))
    headings = [(int(tag.name[1]), tag.get_text(" ", strip=True)) for tag in root.soup.find_all(re.compile(r"^h[1-6]$"))]
    hierarchy_gaps = []
    prev = 0
    for level, text in headings:
        if prev and level > prev + 1:
            hierarchy_gaps.append({"from": prev, "to": level, "text": text})
        prev = level
    checks.append(measured_check(
        "Heading hierarchy", not hierarchy_gaps, value=hierarchy_gaps, expected="Avoid skipping heading levels", weight=0.5,
        source="HTML heading inspection", warning=bool(hierarchy_gaps), warning_score=75,
        message=f"Found {len(hierarchy_gaps)} heading-level jump(s)." if hierarchy_gaps else "No skipped heading levels detected.",
        recommendation="Use headings in a logical hierarchy (H1 → H2 → H3) to structure content." if hierarchy_gaps else None,
        priority="Low",
    ))
    canonical_tags = root.soup.find_all("link", rel=lambda value: value and "canonical" in value)
    canonical_href = str(canonical_tags[0].get("href", "")) if canonical_tags else ""
    canonical_ok = len(canonical_tags) == 1 and canonical_href.startswith(("http://", "https://"))
    checks.append(measured_check(
        "Canonical URL", canonical_ok, value=canonical_href or None, expected="One absolute canonical link", weight=0.75,
        source="HTML link metadata", message="Canonical link is missing, duplicated, or not absolute." if not canonical_ok else "One absolute canonical URL is present.",
        recommendation="Add one absolute rel=canonical URL that matches the preferred indexable URL." if not canonical_ok else None,
    ))
    noindex = _robots_noindex(root)
    checks.append(measured_check(
        "Indexability directive", not noindex, value={"noindex": noindex, "x_robots_tag": root.headers.get("x-robots-tag")},
        expected="No unintended noindex directive", weight=1.0, source="HTML meta + HTTP headers",
        message="The root page declares noindex." if noindex else "No noindex directive detected on the root page.",
        priority="Critical", recommendation="Remove noindex from pages intended to appear in search results; keep it on private or duplicate pages." if noindex else None,
    ))
    html_tag = root.soup.find("html")
    lang = html_tag.get("lang") if html_tag else None
    checks.append(measured_check("HTML language attribute", bool(lang), value=lang, expected="A valid lang attribute on <html>", weight=0.5, source="HTML document", message="The html element has no lang attribute." if not lang else "HTML language is declared.", recommendation='Set <html lang="en"> (or the correct page language). ' if not lang else None, priority="Low"))

    total_images = sum(page.image_count for page in pages)
    missing_alt = sum(page.images_missing_alt for page in pages)
    alt_ratio = 1 - (missing_alt / total_images) if total_images else 1.0
    checks.append(measured_check(
        "Image alternative text coverage", alt_ratio >= 0.9, value={"images": total_images, "missing_alt": missing_alt, "coverage_percent": round(alt_ratio * 100, 1)},
        expected="At least 90% of informative images have meaningful alt text", weight=1.0, source="HTML image inspection",
        warning=total_images > 0 and alt_ratio < 0.9, warning_score=65,
        message=f"{missing_alt} of {total_images} images lack alt text." if missing_alt else "All discovered images have alt attributes (decorative images may use empty alt).",
        recommendation="Add meaningful alt text to informative images and alt=\"\" to purely decorative images." if missing_alt else None,
    ))
    og = {key: bool(root.soup.find("meta", property=f"og:{key}")) for key in ("title", "description", "image")}
    checks.append(measured_check("Open Graph metadata", all(og.values()), value=og, expected="og:title, og:description, and og:image", weight=0.5, source="HTML social metadata", warning=not all(og.values()), warning_score=70, message="One or more core Open Graph properties are missing." if not all(og.values()) else "Core Open Graph metadata is present.", recommendation="Add Open Graph title, description, and image tags for consistent sharing previews." if not all(og.values()) else None, priority="Low"))

    all_schema = [obj for page in pages for obj in _jsonld_objects(page.soup)]
    schema_types = []
    for obj in all_schema:
        schema_type = obj.get("@type", [])
        schema_types.extend(schema_type if isinstance(schema_type, list) else [schema_type])
    checks.append(measured_check("Structured data (JSON-LD)", bool(all_schema), value=sorted(set(map(str, schema_types))), expected="Valid structured data appropriate to page content", weight=0.75, source="HTML JSON-LD inspection", warning=not bool(all_schema), warning_score=75, message="No JSON-LD structured data block was detected." if not all_schema else "JSON-LD structured data is present.", recommendation="Add relevant Schema.org JSON-LD (such as Organization, WebSite, Article, Product, or BreadcrumbList) and validate its properties." if not all_schema else None, priority="Low"))

    title_counts = Counter(title.lower() for title in titles if title)
    desc_counts = Counter(desc.lower() for desc in descriptions if desc)
    duplicate_titles = [title for title, count in title_counts.items() if count > 1]
    duplicate_descriptions = [description[:120] for description, count in desc_counts.items() if count > 1]
    checks.append(measured_check("Duplicate page titles", not duplicate_titles, value=duplicate_titles, expected="Unique titles across crawled pages", weight=0.75, source="Limited internal crawl", warning=bool(duplicate_titles), warning_score=65, message=f"Found {len(duplicate_titles)} duplicated title value(s)." if duplicate_titles else "No duplicate titles found in the crawled sample.", recommendation="Give each indexable page a distinct title that reflects its unique content." if duplicate_titles else None))
    checks.append(measured_check("Duplicate meta descriptions", not duplicate_descriptions, value=duplicate_descriptions, expected="Unique descriptions across crawled pages", weight=0.5, source="Limited internal crawl", warning=bool(duplicate_descriptions), warning_score=70, message=f"Found {len(duplicate_descriptions)} duplicated description value(s)." if duplicate_descriptions else "No duplicate descriptions found in the crawled sample.", recommendation="Write distinct descriptions for important indexable pages." if duplicate_descriptions else None))
    broken_count = len(ctx.broken_internal_links)
    checks.append(measured_check("Internal broken-page targets", broken_count == 0, value=ctx.broken_internal_links, expected="No internal 4xx/5xx targets in the crawl sample", weight=0.75, source="Limited internal crawl", warning=0 < broken_count <= 2, warning_score=65, message=f"The crawl found {broken_count} inaccessible internal target(s).", recommendation="Repair, redirect, or remove broken internal links." if broken_count else None))

    if ctx.robots_parser is not None:
        checks.append(measured_check("Robots.txt and crawl directives", True, value=ctx.robots_url, expected="Valid and intentional robots rules", weight=0.5, source="Python RobotFileParser"))
    else:
        checks.append(measured_check("Robots.txt and crawl directives", None, value=ctx.robots_error, source="Python RobotFileParser"))

    search_console = None
    credentials_file = config.get("gsc_credentials_file")
    property_url = config.get("gsc_property_url")
    if credentials_file and property_url and external:
        try:
            search_console = external.search_console(credentials_file, property_url)
            summary = search_console.get("summary") or {}
            checks.append(Check(
                name="Google Search Console search performance (28 days)", status="info", value=summary or "No rows returned",
                expected="Read-only property data; not a score input", message="Search Console aggregate metrics are actual property-authorized data and are not used to infer rankings or backlinks.",
                source="Google Search Console API", source_url="https://developers.google.com/webmaster-tools/v1/searchanalytics/query", weight=0, score=None,
            ))
        except Exception as exc:
            search_console = {"error": f"{type(exc).__name__}: {exc}"}
            checks.append(measured_check("Google Search Console search performance (28 days)", None, value=search_console["error"], source="Google Search Console API"))
    else:
        search_console = {"status": "Not configured"}

    return category_result(
        "SEO", checks,
        summary=f"On-page technical SEO checks cover {len(pages)} crawled page(s); no ranking, backlink, impression, or traffic claims are made.",
        raw={"pages_checked": len(pages), "duplicate_titles": duplicate_titles, "duplicate_descriptions": duplicate_descriptions, "schema_types": sorted(set(map(str, schema_types))), "google_search_console": search_console},
        recommendations=["Ensure each indexable page has a unique title, description, canonical URL, and useful heading structure.", "Fix unintended noindex directives and broken internal links.", "Add valid structured data only where it accurately describes visible page content."],
    )
