"""Safe HTTP fetching for prospect websites.

Three protections, all load-bearing:

1. SSRF guard. The URLs fetched here come from third-party data (a `website`
   field in a Places result, a link in a tender document). Without a guard,
   a crafted value like http://169.254.169.254/ or http://localhost:6379/
   turns this worker into a proxy into our own network. Every hostname is
   resolved first and refused if it lands on a private, loopback, link-local
   or reserved range - including after redirects, which is where naive
   implementations leak.

2. robots.txt. Parsed and cached per host, obeyed for our user-agent.

3. Hard limits. Timeout, redirect cap, and a byte ceiling enforced while
   streaming, so a multi-gigabyte response cannot exhaust memory.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx
import tldextract

from app.config import settings

# tldextract normally fetches the public suffix list on first use. Pin it to the
# bundled snapshot so the app works offline and start-up is not network-bound.
_extract = tldextract.TLDExtract(suffix_list_urls=())

MAX_REDIRECTS = 3
_robots_cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
_ROBOTS_TTL = 3600.0
_last_hit: dict[str, float] = {}
_HOST_DELAY = 1.5  # seconds between requests to the same host


class UnsafeURLError(Exception):
    """The URL resolves somewhere we must not fetch."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    text: str = ""
    ok: bool = False
    error: str | None = None
    elapsed_ms: int = 0
    ttfb_ms: int = 0
    bytes_len: int = 0
    headers: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "ignore")).hexdigest()


def registrable_domain(url_or_host: str) -> str:
    """example.co.uk from https://www.shop.example.co.uk/path - public-suffix aware."""
    value = (url_or_host or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = urlparse(value).hostname or ""
    ext = _extract(value)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def normalize_url(raw: str) -> str:
    """Repair common typos and return a well-formed URL, or '' if unusable.

    OpenStreetMap and directory data contain values like 'http//x.com' and
    'hejdhfdguh/fd/kli'. The second is not a URL and must not become one.
    """
    u = (raw or "").strip().split(";")[0].strip().strip('"\'')
    if not u or " " in u:
        return ""
    u = u.replace("http//", "http://").replace("https//", "https://")
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if not host or "." not in host or not registrable_domain(host):
        return ""
    if host.endswith((".png", ".jpg", ".gif", ".webp", ".pdf")):
        return ""
    return urlunparse((p.scheme, p.netloc, p.path or "/", "", p.query, ""))


# NAT64 (RFC 6052) embeds a real IPv4 address in the low 32 bits of this
# prefix. Networks behind NAT64 resolve public sites to addresses in it, and
# Python flags the whole range as reserved. Unwrap it and judge the IPv4
# address instead, so the guard stays strict without breaking such networks.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _unwrap(addr: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in _NAT64_PREFIX:
            return ipaddress.ip_address(int(addr) & 0xFFFFFFFF)
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped:
            return mapped
    return addr


def _ip_is_public(ip: str) -> bool:
    try:
        addr = _unwrap(ipaddress.ip_address(ip))
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_safe_url(url: str) -> None:
    """Raise UnsafeURLError unless every resolved address is public."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme not allowed: {p.scheme}")
    host = p.hostname
    if not host:
        raise UnsafeURLError("no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"dns failure: {exc}") from exc
    if not infos:
        raise UnsafeURLError("no addresses")
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise UnsafeURLError(f"{host} resolves to non-public address {ip}")


def _robots_for(base: str) -> urllib.robotparser.RobotFileParser | None:
    now = time.time()
    cached = _robots_cache.get(base)
    if cached and now - cached[0] < _ROBOTS_TTL:
        return cached[1]
    parser: urllib.robotparser.RobotFileParser | None = None
    try:
        robots_url = base.rstrip("/") + "/robots.txt"
        assert_safe_url(robots_url)
        with httpx.Client(timeout=8, follow_redirects=True) as client:
            r = client.get(robots_url, headers={"User-Agent": settings.scraper_user_agent})
        if r.status_code == 200 and len(r.text) < 500_000:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(r.text.splitlines())
    except Exception:
        parser = None  # no robots.txt, or unreachable -> allowed by convention
    _robots_cache[base] = (now, parser)
    return parser


def robots_allows(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    parser = _robots_for(base)
    if parser is None:
        return True
    try:
        return parser.can_fetch(settings.scraper_user_agent, url)
    except Exception:
        return True


def _throttle(host: str) -> None:
    last = _last_hit.get(host)
    now = time.time()
    if last is not None and now - last < _HOST_DELAY:
        time.sleep(_HOST_DELAY - (now - last))
    _last_hit[host] = time.time()


def fetch(url: str, *, respect_robots: bool = True, timeout: int | None = None) -> FetchResult:
    """Fetch one page with every guard applied. Never raises for network errors."""
    normalized = normalize_url(url)
    if not normalized:
        return FetchResult(url=url, final_url="", status=None, error="malformed url")

    result = FetchResult(url=normalized, final_url=normalized, status=None)
    started = time.perf_counter()
    try:
        assert_safe_url(normalized)
        if respect_robots and not robots_allows(normalized):
            result.error = "disallowed by robots.txt"
            return result

        host = urlparse(normalized).hostname or ""
        _throttle(host)

        headers = {
            "User-Agent": settings.scraper_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }
        with httpx.Client(
            timeout=timeout or settings.fetch_timeout_seconds,
            follow_redirects=False,
            headers=headers,
        ) as client:
            current = normalized
            for _ in range(MAX_REDIRECTS + 1):
                assert_safe_url(current)  # re-check each hop, not just the first
                with client.stream("GET", current) as resp:
                    result.status = resp.status_code
                    result.final_url = str(resp.url)
                    if resp.is_redirect and resp.headers.get("location"):
                        current = str(resp.next_request.url) if resp.next_request else ""
                        if not current:
                            break
                        continue
                    if result.ttfb_ms == 0:
                        result.ttfb_ms = int((time.perf_counter() - started) * 1000)
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > settings.max_fetch_bytes:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    result.bytes_len = total
                    result.headers = dict(resp.headers)
                    ctype = resp.headers.get("content-type", "")
                    if "html" in ctype or "text" in ctype or not ctype:
                        encoding = resp.encoding or "utf-8"
                        result.text = raw.decode(encoding, errors="replace")
                    result.ok = resp.status_code < 400
                    break
    except UnsafeURLError as exc:
        result.error = f"blocked: {exc}"
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - never let a fetch kill a run
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def site_is_live(url: str) -> tuple[bool, str]:
    """Does this website actually respond? Returns (live, working_url).

    A 401/403/429 means the server exists but is blocking us, which still
    proves the business has a website. 404/410/5xx and DNS failures do not.
    """
    normalized = normalize_url(url)
    if not normalized:
        return False, ""
    candidates = [normalized]
    if normalized.startswith("https://"):
        candidates.append("http://" + normalized[len("https://") :])
    for candidate in candidates:
        r = fetch(candidate, respect_robots=False, timeout=10)
        if r.status is not None and (r.status < 400 or r.status in (401, 403, 429)):
            return True, r.final_url or candidate
    return False, ""
