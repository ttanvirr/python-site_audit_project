from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests import Response

LOG = logging.getLogger(__name__)
REDIRECT_CODES = {301, 302, 303, 307, 308}


class UnsafeTargetError(ValueError):
    """Raised when a URL could resolve to a non-public network target."""


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("URL cannot be empty")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only public HTTP and HTTPS URLs are supported")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must contain a hostname and must not include credentials")
    if parts.port is not None and not (1 <= parts.port <= 65535):
        raise ValueError("Invalid port")
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def assert_public_host(hostname: str, port: int | None = None) -> list[str]:
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [str(literal)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeTargetError(f"DNS resolution failed for {hostname}: {exc}") from exc
        addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise UnsafeTargetError(f"No IP addresses found for {hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise UnsafeTargetError(f"Host {hostname} resolves to a non-public IP address ({address})")
    return addresses


@dataclass
class FetchResult:
    response: Response
    elapsed_ms: float
    redirects: list[str]


class SafeHttpClient:
    def __init__(self, timeout: float = 20.0, user_agent: str = "SiteAuditBot/1.0 (+technical website audit)"):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        })

    def request(self, method: str, url: str, *, max_redirects: int = 5, allowed_hosts: set[str] | None = None, **kwargs) -> FetchResult:
        current = normalize_url(url)
        redirects: list[str] = []
        request_timeout = kwargs.pop("timeout", self.timeout)
        started = time.perf_counter()
        for _ in range(max_redirects + 1):
            parts = urlsplit(current)
            assert_public_host(parts.hostname or "", parts.port)
            response = api_request(
                self.session,
                method,
                current,
                attempts=3,
                base_delay=0.5,
                timeout=request_timeout,
                allow_redirects=False,
                raise_for_status=False,
                **kwargs,
            )
            if response.status_code not in REDIRECT_CODES or not response.headers.get("Location"):
                return FetchResult(response=response, elapsed_ms=(time.perf_counter() - started) * 1000, redirects=redirects)
            location = urljoin(current, response.headers["Location"])
            next_url = normalize_url(location)
            next_host = (urlsplit(next_url).hostname or "").lower()
            if allowed_hosts is not None and next_host not in {host.lower() for host in allowed_hosts}:
                response.close()
                raise UnsafeTargetError(f"Redirect to a host outside the allowed crawl scope was blocked: {next_host}")
            redirects.append(next_url)
            response.close()
            current = next_url
        raise requests.TooManyRedirects(f"More than {max_redirects} redirects for {url}")

    def get(self, url: str, **kwargs) -> FetchResult:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> FetchResult:
        return self.request("POST", url, **kwargs)


def retry_request(callable_, *, attempts: int = 3, base_delay: float = 1.0):
    last_error = None
    for attempt in range(attempts):
        try:
            return callable_()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_delay * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError("retry_request failed without an exception")


def api_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    raise_for_status: bool = True,
    **kwargs,
) -> requests.Response:
    """HTTP helper with retries; optionally preserve non-2xx responses for site auditing.

    External API integrations use the default ``raise_for_status=True``. The
    target-site client sets it to False so real 4xx/5xx response codes can be
    reported as measured site-health failures instead of transport errors.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_delay * (2 ** attempt))
            continue

        if response.status_code == 429 or 500 <= response.status_code <= 599:
            if attempt < attempts - 1:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(30.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = base_delay * (2 ** attempt)
                response.close()
                time.sleep(delay)
                continue
        if raise_for_status:
            response.raise_for_status()
        return response

    if last_error:
        raise last_error
    raise requests.HTTPError(f"Request failed after {attempts} attempts: {method} {url}")
