from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from auditor.network import SafeHttpClient, normalize_url

LOG = logging.getLogger(__name__)


@dataclass
class PageData:
    url: str
    status_code: int
    elapsed_ms: float
    html: str
    soup: BeautifulSoup
    headers: dict[str, str] = field(default_factory=dict)
    title: str = ""
    description: str = ""
    h1_count: int = 0
    word_count: int = 0
    image_count: int = 0
    images_missing_alt: int = 0
    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)


@dataclass
class SiteContext:
    requested_url: str
    final_url: str
    hostname: str
    origin: str
    client: SafeHttpClient
    root_status: int | None = None
    root_elapsed_ms: float | None = None
    root_headers: dict[str, str] = field(default_factory=dict)
    root_html: str = ""
    root_soup: BeautifulSoup | None = None
    root_redirects: list[str] = field(default_factory=list)
    fetch_error: str | None = None
    robots_url: str = ""
    robots_text: str = ""
    robots_parser: RobotFileParser | None = None
    robots_error: str | None = None
    sitemap_url: str = ""
    sitemap_status: int | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    pages: list[PageData] = field(default_factory=list)
    broken_internal_links: list[dict] = field(default_factory=list)
    broken_resources: list[dict] = field(default_factory=list)
    resource_urls: list[str] = field(default_factory=list)
    crawl_errors: list[str] = field(default_factory=list)
    w3c_result: dict | None = None
    external_service_results: dict = field(default_factory=dict)

    def can_fetch(self, url: str, user_agent: str = "SiteAuditBot") -> bool:
        if not self.robots_parser:
            return True
        try:
            return self.robots_parser.can_fetch(user_agent, url)
        except Exception:
            return True


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _page_data(url: str, response, elapsed_ms: float, html: str, hostname: str) -> PageData:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = str(desc_tag.get("content", "")).strip() if desc_tag else ""
    internal, external = [], []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, str(anchor.get("href")))
        parts = urlsplit(href)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        normalized = _clean_url(href)
        if parts.hostname.lower() == hostname.lower():
            internal.append(normalized)
        else:
            external.append(normalized)
    images = soup.find_all("img")
    visible_text = soup.get_text(" ", strip=True)
    return PageData(
        url=url,
        status_code=response.status_code,
        elapsed_ms=elapsed_ms,
        html=html,
        soup=soup,
        headers={k.lower(): v for k, v in response.headers.items()},
        title=title,
        description=description,
        h1_count=len(soup.find_all("h1")),
        word_count=len(visible_text.split()),
        image_count=len(images),
        # Empty alt text is valid for decorative images; only missing attributes count here.
        images_missing_alt=sum(1 for img in images if not img.has_attr("alt")),
        internal_links=internal,
        external_links=external,
    )


def build_context(url: str, pages_limit: int, timeout: float) -> SiteContext:
    requested = normalize_url(url)
    parsed = urlsplit(requested)
    hostname = parsed.hostname or ""
    client = SafeHttpClient(timeout=timeout)
    ctx = SiteContext(
        requested_url=requested,
        final_url=requested,
        hostname=hostname,
        origin=f"{parsed.scheme}://{parsed.netloc}",
        client=client,
    )
    try:
        root = client.get(requested)
        ctx.final_url = root.response.url
        final_parts = urlsplit(ctx.final_url)
        ctx.hostname = final_parts.hostname or hostname
        ctx.origin = f"{final_parts.scheme}://{final_parts.netloc}"
        ctx.root_status = root.response.status_code
        ctx.root_elapsed_ms = round(root.elapsed_ms, 1)
        ctx.root_headers = {k.lower(): v for k, v in root.response.headers.items()}
        ctx.root_redirects = root.redirects
        ctx.root_html = root.response.text if "html" in root.response.headers.get("content-type", "").lower() or not root.response.headers.get("content-type") else ""
        ctx.root_soup = BeautifulSoup(ctx.root_html, "html.parser") if ctx.root_html else None
    except Exception as exc:  # preserve audit operation even if site is unreachable
        ctx.fetch_error = f"{type(exc).__name__}: {exc}"
        LOG.warning("Could not fetch root page: %s", ctx.fetch_error)
        return ctx

    # robots.txt: a missing or inaccessible file is recorded, not assumed to block crawling.
    ctx.robots_url = urljoin(ctx.origin, "/robots.txt")
    try:
        robots_response = client.get(ctx.robots_url, allowed_hosts={ctx.hostname})
        if robots_response.response.status_code == 200:
            ctx.robots_text = robots_response.response.text[:500_000]
            parser = RobotFileParser()
            parser.set_url(ctx.robots_url)
            parser.parse(ctx.robots_text.splitlines())
            ctx.robots_parser = parser
        else:
            ctx.robots_error = f"HTTP {robots_response.response.status_code}"
    except Exception as exc:
        ctx.robots_error = f"{type(exc).__name__}: {exc}"

    # Check a bounded sample of same-host resources referenced by the root HTML.
    if ctx.root_soup is not None:
        asset_urls = []
        for tag in ctx.root_soup.find_all(src=True):
            asset_urls.append(str(tag.get("src", "")))
        for tag in ctx.root_soup.find_all("link", href=True):
            rel = tag.get("rel") or []
            if any(item in rel for item in ("stylesheet", "icon", "preload", "modulepreload")):
                asset_urls.append(str(tag.get("href", "")))
        seen_assets = set()
        for asset in asset_urls:
            asset_url = urljoin(ctx.final_url, asset)
            parts = urlsplit(asset_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.hostname.lower() != ctx.hostname.lower():
                continue
            asset_url = _clean_url(asset_url)
            if asset_url in seen_assets:
                continue
            seen_assets.add(asset_url)
            if not ctx.can_fetch(asset_url):
                ctx.crawl_errors.append(f"Resource blocked by robots.txt: {asset_url}")
                continue
            ctx.resource_urls.append(asset_url)
            if len(ctx.resource_urls) >= 30:
                break
        for asset_url in ctx.resource_urls:
            try:
                resource_response = client.get(asset_url, allowed_hosts={ctx.hostname})
                if resource_response.response.status_code >= 400:
                    ctx.broken_resources.append({"url": asset_url, "status_code": resource_response.response.status_code})
            except Exception as exc:
                ctx.broken_resources.append({"url": asset_url, "error": f"{type(exc).__name__}: {exc}"})
            time.sleep(0.1)

    # Prefer sitemap references declared in robots.txt, otherwise probe /sitemap.xml.
    sitemap_candidates = []
    for line in ctx.robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_candidates.append(line.split(":", 1)[1].strip())
    if not sitemap_candidates:
        sitemap_candidates.append(urljoin(ctx.origin, "/sitemap.xml"))
    for candidate in sitemap_candidates[:3]:
        try:
            if (urlsplit(candidate).hostname or "").lower() != ctx.hostname.lower():
                ctx.crawl_errors.append(f"External sitemap host was not fetched: {candidate}")
                continue
            if not ctx.can_fetch(candidate):
                ctx.crawl_errors.append(f"Sitemap blocked by robots.txt: {candidate}")
                continue
            sitemap_response = client.get(candidate, allowed_hosts={ctx.hostname})
            ctx.sitemap_url = sitemap_response.response.url
            ctx.sitemap_status = sitemap_response.response.status_code
            if sitemap_response.response.status_code == 200:
                try:
                    xml_root = ET.fromstring(sitemap_response.response.content[:2_000_000])
                    ctx.sitemap_urls = [element.text.strip() for element in xml_root.iter() if element.tag.endswith("loc") and element.text][:1000]
                except ET.ParseError as parse_error:
                    ctx.crawl_errors.append(f"Sitemap XML parse error: {parse_error}")
                if ctx.sitemap_urls:
                    break
        except Exception as exc:
            ctx.crawl_errors.append(f"Sitemap fetch failed ({candidate}): {exc}")

    # Crawl only same-host pages and honor robots.txt for SiteAuditBot.
    queue = deque([ctx.final_url])
    seen: set[str] = set()
    while queue and len(ctx.pages) < max(1, pages_limit):
        current = _clean_url(queue.popleft())
        if current in seen:
            continue
        seen.add(current)
        if (urlsplit(current).hostname or "").lower() != ctx.hostname.lower():
            continue
        if not ctx.can_fetch(current):
            ctx.crawl_errors.append(f"Blocked by robots.txt: {current}")
            continue
        try:
            fetched = client.get(current, allowed_hosts={ctx.hostname})
            response = fetched.response
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code >= 400:
                ctx.broken_internal_links.append({"url": current, "status_code": response.status_code})
                continue
            if "html" not in content_type and current != ctx.final_url:
                continue
            html = response.text[:5_000_000]
            page = _page_data(response.url, response, fetched.elapsed_ms, html, ctx.hostname)
            ctx.pages.append(page)
            for link in page.internal_links:
                if link not in seen and len(queue) < pages_limit * 8:
                    if len(ctx.pages) + len(queue) < pages_limit * 4 and ctx.can_fetch(link):
                        queue.append(link)
        except Exception as exc:
            ctx.broken_internal_links.append({"url": current, "error": f"{type(exc).__name__}: {exc}"})
        crawl_delay = ctx.robots_parser.crawl_delay("SiteAuditBot") if ctx.robots_parser else None
        time.sleep(min(5.0, max(0.15, float(crawl_delay or 0.15))))  # obey crawl-delay when published

    return ctx
