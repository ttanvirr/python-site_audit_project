from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from auditor.checks import measured_check
from auditor.context import SiteContext
from auditor.models import Check, CategoryResult
from auditor.scoring import category_result
from auditor.services.external import ExternalAudits
from auditor.audits.seo import _jsonld_objects

AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "ClaudeBot", "Google-Extended"]


def _objects(ctx: SiteContext) -> list[dict]:
    return [obj for page in ctx.pages for obj in _jsonld_objects(page.soup)]


def _walk_values(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def run_geo(ctx: SiteContext, external: ExternalAudits, config: dict) -> CategoryResult:
    checks: list[Check] = []
    raw: dict = {"mode": "GEO technical readiness", "actual_ai_visibility_tested": False}
    root = ctx.root_soup
    if root is None:
        checks.append(measured_check("GEO page-content inspection", None, source="Python HTML parser"))
        return category_result("GEO", checks, summary="The page could not be fetched; GEO readiness is unavailable.")

    # Check a small, relevant set of user-agent groups. No robots file means we cannot verify crawl policy.
    if ctx.robots_parser is not None:
        crawler_results = {agent: ctx.robots_parser.can_fetch(agent, ctx.final_url) for agent in AI_CRAWLERS}
        allowed_count = sum(crawler_results.values())
        checks.append(measured_check(
            "AI search crawler access (robots.txt)", allowed_count == len(AI_CRAWLERS),
            value=crawler_results, expected=f"Allow all checked agents: {', '.join(AI_CRAWLERS)}", weight=1.5,
            source="robots.txt / RobotFileParser", warning=allowed_count > 0, warning_score=60,
            message=f"{len(AI_CRAWLERS) - allowed_count} of {len(AI_CRAWLERS)} checked AI crawler user agents are disallowed.",
            priority="High", recommendation="Review robots.txt groups for the AI crawlers you intend to permit; only allow bots consistent with your content and access policies." if allowed_count < len(AI_CRAWLERS) else None,
        ))
    else:
        checks.append(measured_check("AI search crawler access (robots.txt)", None, value=ctx.robots_error, source="robots.txt / RobotFileParser"))
        raw["crawler_policy_note"] = "No parseable robots.txt was available; crawler access cannot be determined."

    # llms.txt is reported as an informational file presence check, not a ranking signal.
    llms_url = urljoin(ctx.origin, "/llms.txt")
    if not ctx.can_fetch(llms_url):
        raw["llms_txt"] = {"url": llms_url, "status": "not fetched; disallowed by robots.txt"}
        checks.append(Check(name="llms.txt presence (informational)", status="info", value=raw["llms_txt"], expected="Informational only; not a guaranteed ranking factor", message="llms.txt was not fetched because robots.txt disallows this audit user agent.", source="robots.txt policy", source_url="https://llmstxt.org/", weight=0, score=None))
    else:
        try:
            response = ctx.client.get(llms_url, allowed_hosts={ctx.hostname})
            exists = response.response.status_code == 200 and bool(response.response.text.strip())
            raw["llms_txt"] = {"url": llms_url, "status_code": response.response.status_code, "bytes": len(response.response.content)}
            checks.append(Check(name="llms.txt presence (informational)", status="info", value=raw["llms_txt"], expected="Informational only; not a guaranteed ranking factor", message="llms.txt is present." if exists else "llms.txt was not found or was empty.", source="Python HTTP check", source_url="https://llmstxt.org/", weight=0, score=None))
        except Exception as exc:
            raw["llms_txt"] = {"url": llms_url, "error": f"{type(exc).__name__}: {exc}"}
            checks.append(Check(name="llms.txt presence (informational)", status="info", value=raw["llms_txt"], expected="Informational only; not a guaranteed ranking factor", message="Could not check llms.txt.", source="Python HTTP check", source_url="https://llmstxt.org/", weight=0, score=None))

    objects = _objects(ctx)
    types, names, details = set(), [], []
    flattened = [item for obj in objects for item in _walk_values(obj)]
    for obj in flattened:
        schema_type = obj.get("@type")
        if schema_type:
            types.update(schema_type if isinstance(schema_type, list) else [schema_type])
        if obj.get("name"):
            names.append(str(obj["name"]))
        details.append(obj)
    organization = any(str(item).lower() in {"organization", "localbusiness", "corporation", "professionalservice"} for item in types)
    og_site = root.find("meta", property="og:site_name")
    title_text = root.title.get_text(" ", strip=True) if root.title else ""
    identity = organization or bool(og_site and og_site.get("content")) or bool(title_text)
    checks.append(measured_check(
        "Clear brand/business identity", identity, value={"organization_schema": organization, "og_site_name": og_site.get("content") if og_site else None, "title": title_text, "schema_names": sorted(set(names))[:10]},
        expected="Explicit Organization/LocalBusiness identity or consistent site name", weight=1.25,
        source="HTML metadata + Schema.org JSON-LD", warning=identity and not organization, warning_score=70,
        message="A clear Organization/LocalBusiness identity was not detected." if not identity else "Brand/site identity signals were detected.",
        recommendation="Add consistent business name, URL, logo, and contact details in Organization or LocalBusiness structured data." if not organization else None,
        priority="Medium",
    ))
    business_details = any(any(key in obj for key in ("address", "telephone", "email", "contactPoint", "logo")) for obj in flattened)
    checks.append(measured_check(
        "Organization contact/brand attributes", business_details, value=[key for key in ("address", "telephone", "email", "contactPoint", "logo") if any(key in obj for obj in flattened)],
        expected="Useful organization attributes when applicable", weight=0.75, source="Schema.org JSON-LD",
        warning=bool(objects) and not business_details, warning_score=70,
        message="Structured data lacks common organization identity/contact attributes." if not business_details else "Organization/contact/brand attributes were found.",
        recommendation="Add accurate logo and public contact or address details where appropriate; do not invent organization data." if not business_details else None,
    ))
    author_present = bool(root.find("meta", attrs={"name": re.compile(r"^author$", re.I)})) or any("author" in obj for obj in flattened) or bool(root.select_one("[rel='author'], .author, .byline"))
    checks.append(measured_check("Author information", author_present, value=author_present, expected="Author/byline or structured author for editorial content", weight=0.75, source="HTML + Schema.org", warning=not author_present, warning_score=70, message="No author/byline signal was detected on the root page." if not author_present else "Author information was detected.", recommendation="Show a responsible author or organization for editorial and expert content." if not author_present else None, priority="Low"))

    published = bool(root.find("meta", property="article:published_time") or root.find("meta", property="article:modified_time") or root.find("time", attrs={"datetime": True})) or any("datePublished" in obj or "dateModified" in obj for obj in flattened)
    checks.append(measured_check("Publication/update dates", published, value=published, expected="Visible or structured publication/update dates on dated content", weight=0.5, source="HTML + Schema.org", warning=not published, warning_score=75, message="No publication or modified date signal was detected." if not published else "Publication/update date metadata was detected.", recommendation="Display original publication and meaningful update dates for dated content." if not published else None, priority="Low"))

    word_count = len(root.get_text(" ", strip=True).split())
    headings = len(root.find_all(re.compile(r"^h[1-6]$")))
    checks.append(measured_check("Extractable textual content and hierarchy", word_count >= 150 and headings >= 2, value={"static_html_words": word_count, "headings": headings}, expected="Substantial visible text and structured headings", weight=1.25, source="Static HTML content inspection", warning=word_count >= 40, warning_score=60, message=f"Static HTML contains {word_count} words and {headings} headings." if word_count < 150 or headings < 2 else "Static HTML contains substantive text and headings.", recommendation="Ensure important explanatory content is present in accessible HTML and organized with descriptive headings." if word_count < 150 or headings < 2 else None, priority="Medium"))

    faq_schema = any(str(t).lower() in {"faqpage", "qapage"} for t in types)
    question_headings = [tag.get_text(" ", strip=True) for tag in root.find_all(re.compile(r"^h[1-6]$")) if "?" in tag.get_text() or re.match(r"^(what|how|why|when|where|who|which)\b", tag.get_text(" ", strip=True), re.I)]
    qa = faq_schema or bool(question_headings)
    checks.append(measured_check("Question-and-answer content signals", qa, value={"faq_or_qa_schema": faq_schema, "question_headings": question_headings[:10]}, expected="Useful question-led content where relevant", weight=0.5, source="HTML + Schema.org", warning=not qa, warning_score=75, message="No FAQ/Q&A schema or question-led headings were detected." if not qa else "Question-and-answer content signals were found.", recommendation="Add concise question-and-answer sections when they genuinely help users; do not add unnecessary FAQ markup." if not qa else None, priority="Low"))

    external_links = [link for page in ctx.pages for link in page.external_links]
    checks.append(measured_check("Citations and references", bool(external_links), value={"external_link_count": len(external_links), "examples": external_links[:10]}, expected="Relevant sources/references for factual claims where appropriate", weight=0.5, source="Limited internal crawl", warning=not external_links, warning_score=75, message="No external reference links were found in the crawled pages." if not external_links else "External reference links were found; their quality was not fact-checked.", recommendation="Cite authoritative references for factual, technical, and research claims where they add evidence." if not external_links else None, priority="Low"))

    # Compare static HTML with Chromium-rendered text where the browser audit succeeded.
    scripts = len(root.find_all("script", src=True))
    rendered = ctx.external_service_results.get("rendered_content")
    if rendered:
        static_count = int(rendered.get("static_html_words", word_count))
        rendered_count = int(rendered.get("rendered_dom_words", 0))
        js_hidden = rendered_count >= 100 and static_count < rendered_count * 0.5
        checks.append(measured_check("Important content available in initial HTML", not js_hidden, value={"static_html_words": static_count, "rendered_dom_words": rendered_count, "external_scripts": scripts}, expected="Core content is available in initial HTML or reliably rendered for crawlers", weight=1.0, source="Static HTML vs Playwright-rendered DOM", warning=js_hidden, warning_score=55, message="A large share of visible text appeared only after JavaScript rendering." if js_hidden else "Static HTML contains a substantial share of the rendered text.", recommendation="Server-render or pre-render important copy and verify that supported search crawlers can render the page." if js_hidden else None, priority="Medium"))
    else:
        js_heavy = word_count < 100 and scripts >= 8
        checks.append(measured_check("Important content available in initial HTML", not js_heavy, value={"static_html_words": word_count, "external_scripts": scripts}, expected="Core page text should be discoverable without requiring extensive client-side rendering", weight=1.0, source="Static HTML heuristic", warning=js_heavy, warning_score=55, message="The root HTML has little text relative to the number of external scripts; this may indicate client-side content rendering." if js_heavy else "No strong static-HTML signal of JavaScript-hidden content was found.", recommendation="Server-render or pre-render important copy and ensure crawlers can access meaningful text without interaction." if js_heavy else None, priority="Medium"))

    # Automated scanning cannot certify the factual accuracy of prose or claims.
    checks.append(Check(name="Factual claim verification (manual review)", status="info", value="Not automatically verified", expected="Review claims against authoritative evidence and cite sources", message="This tool checks whether references exist; it does not fact-check claims or certify the authority of linked sources.", source="Audit limitation", weight=0, score=None))

    # llms.txt and schema are not assumed to guarantee ranking or citation.
    raw["schema_types"] = sorted(map(str, types))
    raw["external_reference_count"] = len(external_links)
    raw["static_html_words"] = word_count
    raw["ai_visibility"] = {"status": "Not tested", "reason": "Technical readiness checks do not establish actual mentions or citations in generated answers."}

    queries = config.get("geo_queries") or []
    if config.get("enable_paid_apis") and config.get("dataforseo_login") and config.get("dataforseo_password") and queries:
        try:
            mentions = external.dataforseo_mentions(config["dataforseo_login"], config["dataforseo_password"], queries, ctx.hostname)
            raw["dataforseo_ai_visibility"] = mentions
            raw["ai_visibility"] = {"status": "Measured via DataForSEO", "domain_citations": len(mentions.get("domain_citations", [])), "queries": queries, "note": "Provider result sample only; coverage depends on query selection, platform, and provider data."}
        except Exception as exc:
            raw["dataforseo_error"] = f"{type(exc).__name__}: {exc}"
    elif config.get("dataforseo_login") or config.get("dataforseo_password"):
        raw["dataforseo_note"] = "Credentials were present but no paid call was made; pass --enable-paid-apis and at least one --geo-query to run a live query."
    if config.get("otterly_api_key"):
        raw["otterly_note"] = "OTTERLY_API_KEY is set, but automated endpoint mapping is not enabled in this version; no request was sent. See README limitations."

    return category_result(
        "GEO", checks,
        summary="GEO Readiness Score measures technical/content readiness only. It is not an AI answer visibility or citation score.",
        raw=raw,
        recommendations=["Publish consistent Organization/LocalBusiness details and appropriate structured data.", "Make important text, authorship, dates, and source references easy to identify.", "Review robots.txt for the AI crawlers relevant to your publishing and licensing preferences."],
    )
