"""Conservative shadow-only fetch policy; no crawler state or service DB."""
from __future__ import annotations

import dataclasses
import http.client
import ipaddress
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

from .common import Stop, digest, source_content_hash
from ..net import CHALLENGE_RE, PASSWORD_RE, LOGIN_RE, NoRedirect
from ..parse import parse_html
from .. import egress as socks_egress

BLOCKLIST = Path(__file__).resolve().parents[4] / "config/blocked_hosts.txt"
MAX_BODY = 4_000_000
USER_AGENT = "PitchboardBot/0.1 (+https://pitchboard.soulbound.co.kr; research collector; honors robots.txt)"
# Fixed sandbox fake-IP range documented by collect/net.py. Not read from
# PITCHBOARD_EGRESS_GATEWAY_CIDRS, which can name arbitrary networks.
EGRESS_GATEWAY = ipaddress.ip_network("198.18.0.0/15")


def canonical_url(url, blocked=None):
    """Reject blocked hosts before any DNS/network operation, including redirects."""
    try:
        if not isinstance(url, str) or re.search(r"[\\\x00-\x20\x7f]", url):
            raise Stop("bad_url")
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            raise Stop("bad_url")
        if "%" in p.netloc or p.port not in (None, 443 if p.scheme == "https" else 80):
            raise Stop("bad_url")
        host = p.hostname.lower().removesuffix(".").encode("idna").decode("ascii")
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
            raise Stop("bad_url")
        if blocked is None:
            blocked = {s.strip().lower() for s in BLOCKLIST.read_text().splitlines() if s.strip() and not s.startswith("#")}
        if any(host == b or host.endswith("." + b) for b in {"instagram.com", "soulbound.co.kr", "soulbound.kr", *blocked}):
            raise Stop("blocked_host")
        if _blocked_network_host(host):
            raise Stop("blocked_network")
        return urlunsplit((p.scheme, host, p.path or "/", p.query, ""))
    except Stop:
        raise
    except Exception:
        raise Stop("bad_url") from None


def _normalized_ip(value):
    ip = ipaddress.ip_address(value)
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _route(value):
    """direct is a global address. gateway is only the documented fake-IP range."""
    ip = _normalized_ip(value)
    if ip in EGRESS_GATEWAY:
        return "gateway"
    if ip.is_global:
        return "direct"
    return "denied"


def _blocked_network_host(host):
    """Reject private, metadata, and literal gateway hosts before any DNS lookup."""
    if host == "localhost" or host == "metadata.goog" or host.endswith((".localhost", ".local", ".internal", ".localdomain")):
        return True
    labels = host.split(".")
    obfuscated_ip = all(re.fullmatch(r"(?:0x[0-9a-f]+|\d+)", label) for label in labels)
    try:
        route = _route(host)
    except ValueError:
        return obfuscated_ip
    # A name that is the gateway address is not a public target. DNS may still
    # return an address in that range for a hostname that passed this check.
    return route != "direct"


def public_socket(host, port, timeout, source_address=None, egress="direct"):
    """Resolve once, require one safe route, and pin that numeric address.

    egress direct: every answer must be global. egress sandbox: every answer
    must be global, or every answer must be inside the documented
    198.18.0.0/15 fake-IP range. The range is not configurable. Mixed answers
    and every other non-global address are denied. The pinned address is not
    resolved again.
    """
    if egress not in ("direct", "sandbox"):
        raise Stop("blocked_network")
    try:
        literal = _route(host)
    except ValueError:
        literal = None
    if literal not in (None, "direct"):
        raise Stop("blocked_network")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise Stop("blocked_network")
    routes = []
    for info in infos:
        try:
            route = _route(info[4][0])
        except ValueError:
            raise Stop("blocked_network") from None
        if route == "denied" or (route == "gateway" and egress != "sandbox"):
            raise Stop("blocked_network")
        routes.append(route)
    if len(set(routes)) != 1 or (literal == "direct" and routes[0] != "direct"):
        raise Stop("blocked_network")
    return socket.create_connection((infos[0][4][0], port), timeout, source_address)


def _bind_connection(base, egress):
    class Bound(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.egress = egress
    return Bound


class PublicHTTP(http.client.HTTPConnection):
    egress = "direct"

    def connect(self):
        self.sock = public_socket(self.host, self.port, self.timeout, self.source_address, self.egress)


class PublicHTTPS(http.client.HTTPSConnection):
    egress = "direct"

    def connect(self):
        sock = public_socket(self.host, self.port, self.timeout, self.source_address, self.egress)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, egress="direct"):
        super().__init__()
        self._connection = _bind_connection(PublicHTTP, egress)

    def http_open(self, req):
        return self.do_open(self._connection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, egress="direct"):
        super().__init__()
        self._connection = _bind_connection(PublicHTTPS, egress)

    def https_open(self, req):
        return self.do_open(self._connection, req, context=self._context)


def access_check(body):
    html = body.decode("utf-8", "replace")
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    visible = re.sub(r"<[^>]+>", " ", html)
    wall = re.search(r"please\s+(?:log|sign)\s*in|(?:log|sign)\s*in\s+to\s+(?:read|view|continue|access)|members?\s+only|회원\s*전용|로그인\s*(?:후|이\s*필요)|subscribe\s+to\s+(?:read|continue)|paywall", visible, re.I)
    if CHALLENGE_RE.search(html) or PASSWORD_RE.search(html) or LOGIN_RE.search(title[1] if title else "") or wall:
        raise Stop("access_wall")


@dataclasses.dataclass(frozen=True)
class Page:
    requested_url: str
    final_url: str
    body: bytes = dataclasses.field(repr=False)
    hops: tuple
    provenance: str  # live_verified | offline_cached (never claims robots verification)
    http_status: int = 200
    content_type: str = "text/html"

    def doc(self):
        canonical_url(self.requested_url)
        canonical_url(self.final_url)
        if not self.hops or self.hops[-1] != self.final_url:
            raise Stop("invalid_policy_envelope")
        for hop in self.hops:
            canonical_url(hop)
        if self.provenance not in ("live_verified", "offline_cached"):
            raise Stop("invalid_policy_envelope")
        if len(self.body) > MAX_BODY:
            raise Stop("page_too_large")
        access_check(self.body)
        doc = parse_html(self.body, self.final_url)
        if not doc.text().strip():
            raise Stop("empty_page")
        return doc

    @property
    def body_sha256(self):
        return digest(self.body)

    @property
    def source_content_hash(self):
        """The service ``pb_raw_ref.content_hash`` for this raw body."""
        return source_content_hash(self.body)


class FetchStop(Stop):
    def __init__(self, code, http_status=None, final_url=None):
        super().__init__(code)
        self.http_status = http_status
        self.final_url = final_url


class PolicyFetcher:
    def __init__(self, clock=time.monotonic, sleep=time.sleep, egress="direct", *, require_proxy=False, timeout=15):
        if egress not in ("direct", "sandbox"):
            raise Stop("invalid_config")
        self.clock, self.sleep, self.egress = clock, sleep, egress
        # Env HTTP_PROXY/HTTPS_PROXY/ALL_PROXY stay disabled. sandbox egress is a
        # pinned DNS answer, not an HTTP proxy. An explicit loopback SOCKS5
        # (PITCHBOARD_SOCKS5_PROXY) is a separate, fail-closed path.
        try:
            self.proxy = socks_egress.configured_proxy()
        except socks_egress.ProxyConfigError as exc:
            raise Stop("invalid_config") from exc
        if require_proxy and not self.proxy:
            raise Stop("proxy_not_configured")
        self.timeout = timeout
        if self.proxy:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                                      socks_egress.SocksHTTPHandler(self.proxy), socks_egress.SocksHTTPSHandler(self.proxy))
        else:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                                      PublicHTTPHandler(egress), PublicHTTPSHandler(egress))
        self.robots, self.last, self.halted = {}, {}, set()

    def _get(self, url, accept, limit, delay=2):
        url = canonical_url(url)
        origin = urlsplit(url).scheme + "://" + urlsplit(url).netloc
        if origin in self.halted:
            raise Stop("host_halted")
        wait = max(2, delay) - (self.clock() - self.last.get(origin, float("-inf")))
        if wait > 0:
            self.sleep(wait)
        self.last[origin] = self.clock()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                                  "Accept-Encoding": "identity"})
        try:
            try:
                resp = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                resp = e
            with resp:
                status, headers = resp.code, resp.headers
                if status in (401, 402, 403, 429, 451) or status >= 500:
                    self.halted.add(origin)
                if self.proxy and socks_egress.is_binary_media(url, headers.get("Content-Type", "")):
                    socks_egress.log_traffic(0, urlsplit(url).hostname or "", "skipped_binary_media")
                    code = "denied_http" if status in (401, 402, 403, 429, 451) else "unsupported_content"
                    raise FetchStop(code, status, url)
                body = resp.read(limit + 1)
            if len(body) > limit or headers.get("Content-Encoding", "identity").lower() != "identity":
                raise FetchStop("invalid_transport_body", status, url)
            if self.proxy:
                socks_egress.log_traffic(len(body), urlsplit(url).hostname or "")
            return status, headers, body
        except Stop:
            raise
        except Exception:
            self.halted.add(origin)
            raise Stop("fetch_error") from None

    def allowed(self, url):
        url = canonical_url(url)
        p = urlsplit(url)
        origin = p.scheme + "://" + p.netloc
        cached = self.robots.get(origin)
        if cached is None or self.clock() - cached[0] >= 3600:
            status, headers, body = self._get(origin + "/robots.txt", "text/plain", 500_000)
            rp = urllib.robotparser.RobotFileParser()
            if status in (404, 410):
                rp.parse(["User-agent: *", "Allow: /"])
            elif status == 200 and headers.get("Content-Type", "").split(";")[0].lower() == "text/plain" and body.strip():
                try:
                    text = body.decode("utf-8", "strict")
                except UnicodeError:
                    raise Stop("invalid_robots") from None
                if "\x00" in text or "<" in text or not re.search(r"(?im)^\s*user-agent\s*:", text):
                    raise Stop("invalid_robots")
                rp.parse(text.splitlines())
            else:
                try:
                    access_check(body)
                except Stop as exc:
                    self.halted.add(origin)
                    raise FetchStop(str(exc), status) from None
                raise FetchStop("denied_robots", status)
            self.robots[origin] = (self.clock(), rp)
        else:
            rp = cached[1]
        if not rp.can_fetch(USER_AGENT, url):
            raise Stop("denied_robots")
        return max(2, rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*") or 0)

    def fetch(self, url):
        requested = canonical_url(url)
        current, hops = requested, []
        for _ in range(5):
            current = canonical_url(current)
            try:
                delay = self.allowed(current)
            except Stop as exc:
                raise FetchStop(str(exc), getattr(exc, "http_status", None), current) from None
            hops.append(current)
            try:
                status, headers, body = self._get(current, "text/html,application/xhtml+xml", MAX_BODY, delay)
            except Stop as exc:
                raise FetchStop(str(exc), getattr(exc, "http_status", None), current) from None
            if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                try:
                    current = canonical_url(urljoin(current, headers["Location"]))
                except Stop as exc:
                    raise FetchStop(str(exc), status, current) from None
                continue
            try:
                if not 200 <= status < 300:
                    access_check(body)
                    raise Stop("denied_http")
                content_type = headers.get("Content-Type", "").split(";")[0].lower()
                if content_type not in ("text/html", "application/xhtml+xml"):
                    raise Stop("unsupported_content")
                page = Page(requested, current, body, tuple(hops), "live_verified", status, content_type)
                page.doc()
            except Stop as exc:
                if str(exc) == "access_wall":
                    p = urlsplit(current)
                    self.halted.add(p.scheme + "://" + p.netloc)
                raise FetchStop(str(exc), status, current) from None
            return page
        raise Stop("redirect_limit")
