from __future__ import annotations

import logging
import re
import shutil

from auditor.checks import measured_check
from auditor.context import SiteContext
from auditor.models import Check, CategoryResult
from auditor.scoring import category_result

LOG = logging.getLogger(__name__)
VIEWPORTS = [320, 375, 390, 768, 1024, 1280, 1440]


def run_responsiveness(ctx: SiteContext, timeout: float) -> CategoryResult:
    checks: list[Check] = []
    raw: dict = {"viewports": [], "browser": "Chromium via Playwright"}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
            browser = playwright.chromium.launch(headless=True, executable_path=system_chromium) if system_chromium else playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(int(timeout * 1000))
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(ctx.final_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            rendered_content = page.evaluate("""() => ({
              text: (document.body?.innerText || '').trim(),
              wordCount: (document.body?.innerText || '').trim().split(/\\s+/).filter(Boolean).length,
              headingCount: document.querySelectorAll('h1,h2,h3,h4,h5,h6').length
            })""")
            static_words = len(ctx.root_soup.get_text(" ", strip=True).split()) if ctx.root_soup else 0
            raw["rendered_content"] = {
                "static_html_words": static_words,
                "rendered_dom_words": rendered_content.get("wordCount", 0),
                "rendered_heading_count": rendered_content.get("headingCount", 0),
            }
            ctx.external_service_results["rendered_content"] = raw["rendered_content"]
            for width in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(150)
                metrics = page.evaluate("""() => {
                    const viewport = document.documentElement.clientWidth;
                    const elements = [...document.querySelectorAll('body *')];
                    const overflowing = elements.filter((el) => {
                      const r = el.getBoundingClientRect();
                      const style = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && style.position !== 'fixed' &&
                        (r.right > viewport + 1 || r.left < -1);
                    }).slice(0, 20).map((el) => ({tag: el.tagName.toLowerCase(), id: el.id || null, className: String(el.className || '').slice(0, 100)}));
                    const badImages = [...document.images].filter((img) => {
                      const r = img.getBoundingClientRect();
                      const parent = img.parentElement?.getBoundingClientRect();
                      return r.width > 0 && parent && r.width > parent.width + 1;
                    }).length;
                    const nav = [...document.querySelectorAll('nav,[role="navigation"]')].some((el) => {
                      const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
                    });
                    return {
                      viewportWidth: viewport,
                      documentWidth: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
                      horizontalOverflow: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0) > viewport + 1,
                      overflowingElements: overflowing,
                      badImages,
                      viewportMeta: !!document.querySelector('meta[name="viewport"]'),
                      viewportMetaContent: document.querySelector('meta[name="viewport"]')?.content || null,
                      visibleNavigation: nav
                    };
                }""")
                raw["viewports"].append(metrics)
                overflow = metrics["horizontalOverflow"]
                checks.append(measured_check(
                    f"Horizontal overflow at {width}px", not overflow,
                    value=f"document width {metrics['documentWidth']}px / viewport {width}px",
                    expected="No horizontal scrolling or clipped content", weight=2.0 if width <= 390 else 1.0,
                    source="Playwright Chromium", message=f"Horizontal overflow detected at {width}px." if overflow else "No horizontal overflow detected.",
                    priority="High" if width <= 390 and overflow else "Medium",
                    recommendation="Inspect the listed overflowing elements and constrain their width, wrapping, or positioning." if overflow else None,
                ))
                checks.append(measured_check(
                    f"Images fit their containers at {width}px", metrics["badImages"] == 0,
                    value=metrics["badImages"], expected=0, weight=0.75,
                    source="Playwright Chromium", message=f"{metrics['badImages']} image(s) exceed their parent width." if metrics["badImages"] else "Images fit their measured parent containers.",
                    priority="Medium", recommendation="Use max-width: 100%; height: auto; and responsive image sizing." if metrics["badImages"] else None,
                ))
            viewport_content = raw["viewports"][0].get("viewportMetaContent") if raw["viewports"] else None
            compact_viewport_content = re.sub(r"\s+", "", str(viewport_content or "").lower())
            viewport_meta = "width=device-width" in compact_viewport_content
            checks.append(measured_check(
                "Viewport meta tag", viewport_meta, value=viewport_content,
                expected="A viewport directive containing width=device-width", weight=1.5, source="Playwright Chromium",
                message="Viewport meta tag is missing or does not use width=device-width." if not viewport_meta else "Viewport meta tag contains width=device-width.",
                priority="High", recommendation='Use <meta name="viewport" content="width=device-width, initial-scale=1">.' if not viewport_meta else None,
            ))
            nav = bool(raw["viewports"] and raw["viewports"][0]["visibleNavigation"])
            checks.append(measured_check(
                "Visible navigation landmark", nav, value=nav, expected="A visible nav or navigation landmark", weight=0.5,
                source="Playwright Chromium", message="No visible navigation landmark was detected." if not nav else "A visible navigation landmark was detected.",
                warning=True, warning_score=70,
                recommendation="Use a semantic nav element or role=navigation and verify the mobile menu is operable." if not nav else None,
            ))
            raw["page_errors"] = page_errors[:20]
            browser.close()
    except Exception as exc:
        raw["error"] = f"{type(exc).__name__}: {exc}"
        checks.append(measured_check("Playwright responsive viewport suite", None, value=raw["error"], source="Playwright Chromium"))
        LOG.warning("Responsiveness audit unavailable: %s", exc)
    return category_result(
        "Responsiveness", checks,
        summary=f"Measured layout behavior at {len(raw.get('viewports', []))} viewport widths." if raw.get("viewports") else "Browser-based viewport testing was unavailable.",
        raw=raw,
        recommendations=["Fix horizontal overflow first at 320–390px widths.", "Test navigation and key interactions on real devices after automated viewport checks."],
    )
