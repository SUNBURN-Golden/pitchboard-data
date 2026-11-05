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

from .common import Stop, digest
from ..net import CHALLENGE_RE, PASSWORD_RE, LOGIN_RE, NoRedirect
from ..parse import parse_html

BLOCKLIST = Path(__file__).resolve().parents[4] / "config/blocked_hosts.txt"
MAX_BODY = 4_000_000
USER_AGENT = "PitchboardBot/0.1 (+https://pitchboard.soulbound.co.kr; research collector; honors robots.txt)"


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
        if any(host == b or host.endswith("." + b) for b in {"instagram.com", *blocked}):
            raise Stop("blocked_host")
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None
        if host == "localhost" or host.endswith(".localhost") or (addr is not None and not addr.is_global):
            raise Stop("blocked_network")
        return urlunsplit((p.scheme, host, p.path or "/", p.query, ""))
    except Stop:
        raise
    except Exception:
        raise Stop("bad_url") from None


def public_socket(host, port, timeout, source_address=None):
    """Resolve once, check every answer, then connect to a vetted numeric address."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos or any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise Stop("blocked_network")
    # No gateway-CIDR exception; pin the address to prevent DNS rebinds.
    return socket.create_connection((infos[0][4][0], port), timeout, source_address)


class PublicHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = public_socket(self.host, self.port, self.timeout, self.source_address)


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = public_socket(self.host, self.port, self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PublicHTTP, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPS, req, context=self._context)


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


class PolicyFetcher:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                                  PublicHTTPHandler, PublicHTTPSHandler)
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
                resp = self.opener.open(req, timeout=15)
            except urllib.error.HTTPError as e:
                resp = e
            with resp:
                status, headers = resp.code, resp.headers
                body = resp.read(limit + 1)
            if status in (401, 402, 403, 429, 451) or status >= 500:
                self.halted.add(origin)
            if len(body) > limit or headers.get("Content-Encoding", "identity").lower() != "identity":
                raise Stop("invalid_transport_body")
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
                raise Stop("denied_robots")
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
            delay = self.allowed(current)
            hops.append(current)
            status, headers, body = self._get(current, "text/html,application/xhtml+xml", MAX_BODY, delay)
            if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                current = canonical_url(urljoin(current, headers["Location"]))
                continue
            if not 200 <= status < 300:
                raise Stop("denied_http")
            if headers.get("Content-Type", "").split(";")[0].lower() not in ("text/html", "application/xhtml+xml"):
                raise Stop("unsupported_content")
            page = Page(requested, current, body, tuple(hops), "live_verified")
            page.doc()
            return page
        raise Stop("redirect_limit")
