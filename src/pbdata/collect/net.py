"""Polite, policy-bound HTTP fetcher. It never bypasses a denial: robots disallow, 401/403, 429, login walls and challenges
are recorded as failures and never retried with other identities, proxies or headless tricks."""
from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.request
import urllib.robotparser
from typing import Dict, Optional, Tuple
from urllib.parse import unquote, unquote_plus, urljoin, urlparse, urlunparse

from ..ids import sha256_hex
from . import egress

USER_AGENT = os.environ.get("PITCHBOARD_UA", "PitchboardBot/0.1 (+https://pitchboard.soulbound.co.kr; research collector; honors robots.txt)")
GATEWAY_CIDRS = [c.strip() for c in os.environ.get("PITCHBOARD_EGRESS_GATEWAY_CIDRS", "198.18.0.0/15").split(",") if c.strip()]
MAX_REDIRECTS = 4
MIN_INTERVAL = 2.0

LOGIN_RE = re.compile(r"(log ?in|sign ?in|로그인|member only|회원 전용|subscribe to (read|continue)|paywall)", re.I)
CHALLENGE_RE = re.compile(r"(cf-challenge|just a moment\.\.\.|g-recaptcha|hcaptcha|are you (a )?human|verify you are human|access denied|captcha)", re.I)
PASSWORD_RE = re.compile(r"<input[^>]+type=[\"']?password", re.I)
TRACKING_PARAMS = {"share", "amp-share", "replytocom", "fbclid", "gclid"}
BINARY_PAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
                          ".mp4", ".mov", ".webm", ".mp3", ".wav", ".zip", ".gz", ".rar", ".7z",
                          ".woff", ".woff2", ".ttf", ".css", ".js")


@dataclasses.dataclass
class FetchResult:
    url: str
    final_url: str
    status: Optional[int]
    content_type: str
    body: bytes
    outcome: str                 # ok | login_wall | challenge | empty_body | denied_robots | denied_http | not_found | server_error | timeout | too_large | blocked_network | error
    error_class: str = ""
    fetched_at: str = ""
    content_hash: str = ""
    requests_used: int = 0
    headers: Dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def registered_domain(host: str) -> str:
    """Cheap eTLD+1 (handles common 2-level suffixes seen in our targets)."""
    parts = host.lower().split(".")
    if len(parts) <= 2:
        return host.lower()
    if parts[-2] in ("co", "com", "or", "ne", "go", "ac", "org", "net", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def origin_of(url: str) -> str:
    p = urlparse(url)
    return "%s://%s" % (p.scheme or "https", p.netloc.lower())


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def _is_tracking_param(field: str) -> bool:
    key = unquote_plus(field.partition("=")[0]).lower()
    return key in TRACKING_PARAMS or key.startswith("utm_")


def strip_tracking_params(url: str) -> str:
    """Remove share/tracking parameters while keeping the other query fields verbatim."""
    base, fragment_sep, fragment = url.partition("#")
    path, _, query = base.partition("?")
    fields = query.split("&")
    kept = [field for field in fields if not _is_tracking_param(field)]
    if len(fields) == len(kept):
        return url
    return path + ("?" + "&".join(kept) if any(kept) else "") + fragment_sep + fragment


def junk_page_url(url: str) -> Optional[str]:
    """Classify a stored or discovered page URL without changing it."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    if any(_is_tracking_param(field) for field in p.query.split("&")):
        return "share_variant"
    if unquote(p.path).lower().endswith(BINARY_PAGE_EXTENSIONS):
        return "binary_asset"
    return None


def page_url(url: str) -> Optional[str]:
    """Canonical URL for a new page, or None for a binary asset."""
    url = normalize_url(strip_tracking_params(url.strip()))
    return None if junk_page_url(url) else url


class Fetcher:
    def __init__(self, infra: sqlite3.Connection, clock=time.time, allow_loopback: bool = False, sleep=time.sleep,
                 request_cap: Optional[int] = None):
        self.infra = infra
        self.clock = clock
        self.sleep = sleep
        self.allow_loopback = allow_loopback
        self.request_cap = request_cap
        self.requests = 0
        try:
            self.proxy = egress.configured_proxy()
        except egress.ProxyConfigError:
            self.proxy = False
        if self.proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect,
                egress.SocksHTTPHandler(self.proxy), egress.SocksHTTPSHandler(self.proxy))
        else:
            self.opener = urllib.request.build_opener(NoRedirect)
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    # ---- network safety -------------------------------------------------
    def _addr_ok(self, host: str, port: int) -> Tuple[bool, str]:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False, "dns_failure"
        nets = [ipaddress.ip_network(c) for c in GATEWAY_CIDRS]
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_loopback and self.allow_loopback:
                continue
            if any(ip in n for n in nets):
                continue            # sandbox egress gateway (fake-IP DNS); policy checks happen on the hostname
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False, "non_public_address"
        return True, ""

    def _check_url(self, url: str) -> Tuple[bool, str]:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False, "bad_scheme"
        if p.username or p.password:
            return False, "userinfo"
        port = p.port or (443 if p.scheme == "https" else 80)
        if not self.allow_loopback and port not in (80, 443):
            return False, "bad_port"
        if self.proxy is False:
            return False, "invalid_proxy"
        if self.proxy:
            try:
                ip = ipaddress.ip_address(p.hostname)
            except ValueError:
                return True, ""
            if ip.is_loopback and self.allow_loopback:
                return True, ""
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False, "non_public_address"
            return True, ""
        return self._addr_ok(p.hostname, port)

    # ---- host state -----------------------------------------------------
    def _host(self, host: str) -> sqlite3.Row:
        self.infra.execute("INSERT OR IGNORE INTO host_state(host) VALUES(?)", (host,))
        return self.infra.execute("SELECT * FROM host_state WHERE host=?", (host,)).fetchone()

    def halted(self, host: str) -> Optional[str]:
        h = self._host(host)
        if h["halted_until"] and h["halted_until"] > self.clock():
            return h["halt_reason"] or "halted"
        return None

    def _halt(self, host: str, seconds: float, reason: str) -> None:
        self.infra.execute("UPDATE host_state SET halted_until=?, halt_reason=? WHERE host=?", (self.clock() + seconds, reason, host))

    def _throttle(self, host: str, delay: Optional[float]) -> None:
        h = self._host(host)
        wait = max(MIN_INTERVAL, delay or 0) - (self.clock() - (h["last_request_at"] or 0))
        if wait > 0:
            self.sleep(wait)
        self.infra.execute("UPDATE host_state SET last_request_at=? WHERE host=?", (self.clock(), host))

    # ---- robots ---------------------------------------------------------
    def _raw_get(self, url: str, accept: str, max_bytes: int, timeout: float = 10.0):
        if self.proxy is False or self.opener is None:
            raise OSError("invalid_proxy")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en,ko;q=0.8"})
        self.requests += 1
        try:
            resp = self.opener.open(req, timeout=timeout)
            status, hdrs = resp.status, dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            resp, status, hdrs = e, e.code, dict(e.headers.items()) if e.headers else {}
        ctype = hdrs.get("Content-Type") or hdrs.get("content-type") or ""
        if self.proxy and egress.is_binary_media(url, ctype):
            try:
                resp.close()
            except Exception:
                pass
            if self.proxy:
                egress.log_traffic(0, urlparse(url).hostname or "", "skipped_binary_media")
            return status, hdrs, None
        data = resp.read(max_bytes + 1)
        if self.proxy:
            egress.log_traffic(len(data), urlparse(url).hostname or "")
        return status, hdrs, data

    def _robots_for(self, url: str) -> Tuple[Optional[urllib.robotparser.RobotFileParser], Optional[str], Optional[float]]:
        p = urlparse(url)
        host = p.netloc.lower()
        h = self._host(host)
        now = self.clock()
        if h["robots_fetched_at"] and now - h["robots_fetched_at"] < 86400 and h["robots_status"] is not None:
            text, status = h["robots_text"] or "", h["robots_status"]
        else:
            self._throttle(host, None)
            try:
                status, _, data = self._raw_get("%s://%s/robots.txt" % (p.scheme, host), "text/plain", 500_000, 15)
                text = data[:500_000].decode("utf-8", "replace")
            except Exception:
                return None, "robots_unreachable", None
            self.infra.execute("UPDATE host_state SET robots_text=?, robots_status=?, robots_fetched_at=? WHERE host=?", (text, status, now, host))
        if status in (401, 403):
            return None, "robots_forbidden", None            # conservative: treat as full disallow
        if status >= 500:
            return None, "robots_error", None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines() if status == 200 else [])
        delay = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*")
        return rp, None, float(delay) if delay else None

    def robots_sitemaps(self, origin: str) -> list:
        host = urlparse(origin).netloc.lower()
        h = self._host(host)
        return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", h["robots_text"] or "")

    def allowed(self, url: str) -> Tuple[bool, str]:
        ok, why = self._check_url(url)
        if not ok:
            return False, why
        if self.allow_loopback and urlparse(url).hostname in ("127.0.0.1", "localhost"):
            rp, err, _ = self._robots_for(url)
            return (rp is not None and rp.can_fetch(USER_AGENT, url)), err or "disallow"
        rp, err, _ = self._robots_for(url)
        if rp is None:
            return False, err or "robots"
        return rp.can_fetch(USER_AGENT, url), "disallow"

    # ---- main entry -----------------------------------------------------
    def fetch(self, url: str, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
              max_bytes: int = 4_000_000, now_iso: Optional[str] = None) -> FetchResult:
        from .. import timeutil
        stamp = now_iso or timeutil.iso(timeutil.utcnow())
        start_requests = self.requests
        cur = url
        try:
            for hop in range(MAX_REDIRECTS + 1):
                ok, why = self._check_url(cur)
                if not ok:
                    return FetchResult(url, cur, None, "", b"", "blocked_network", why, stamp, requests_used=self.requests - start_requests)
                host = urlparse(cur).netloc.lower()
                reason = self.halted(host)
                if reason:
                    return FetchResult(url, cur, None, "", b"", "denied_http", "host_halted:" + reason, stamp, requests_used=self.requests - start_requests)
                if self.request_cap is not None and self.requests >= self.request_cap:
                    return FetchResult(url, cur, None, "", b"", "error", "request_budget_exhausted", stamp, requests_used=self.requests - start_requests)
                if self.proxy and egress.is_binary_media(cur):
                    return FetchResult(url, cur, None, "", b"", "error", "skipped_binary_media", stamp, requests_used=self.requests - start_requests)
                rp, err, delay = self._robots_for(cur)
                if rp is None and err == "robots_unreachable":      # network failure, not a denial: nothing is fetched, retry later
                    return FetchResult(url, cur, None, "", b"", "error", "robots_unreachable", stamp, requests_used=self.requests - start_requests)
                if rp is None or not rp.can_fetch(USER_AGENT, cur):
                    return FetchResult(url, cur, None, "", b"", "denied_robots", err or "robots_disallow", stamp, requests_used=self.requests - start_requests)
                self._throttle(host, delay)
                status, hdrs, data = self._raw_get(cur, accept, max_bytes)
                if data is None:
                    return FetchResult(url, cur, status, "", b"", "error", "skipped_binary_media", stamp, requests_used=self.requests - start_requests)
                if status in (301, 302, 303, 307, 308) and hdrs.get("Location"):
                    cur = urljoin(cur, hdrs["Location"])
                    continue
                ctype = hdrs.get("Content-Type", "").split(";")[0].strip().lower()
                res = FetchResult(url, cur, status, ctype, data[:max_bytes], "ok", "", stamp, headers=hdrs)
                res.requests_used = self.requests - start_requests
                if status in (401, 402, 403, 451):
                    res.outcome, res.error_class = "denied_http", "http_%d" % status
                    res.body = b""
                elif status == 429:
                    res.outcome, res.error_class, res.body = "denied_http", "http_429", b""
                    self._halt(host, 6 * 3600, "http_429")
                elif status == 404 or status == 410:
                    res.outcome, res.error_class, res.body = "not_found", "http_%d" % status, b""
                elif status >= 500:
                    res.outcome, res.error_class, res.body = "server_error", "http_%d" % status, b""
                    self.infra.execute("UPDATE host_state SET consecutive_failures=consecutive_failures+1 WHERE host=?", (host,))
                    if self._host(host)["consecutive_failures"] >= 5:
                        self._halt(host, 3600, "repeated_5xx")
                elif len(data) > max_bytes:
                    res.outcome, res.error_class, res.body = "too_large", "over_%d" % max_bytes, b""
                elif len(data) == 0:
                    res.outcome, res.error_class = "empty_body", "zero_bytes"
                else:
                    self.infra.execute("UPDATE host_state SET consecutive_failures=0 WHERE host=?", (host,))
                    if ctype.startswith("text/html") or ctype == "application/xhtml+xml":
                        res = classify_html(res)
                res.content_hash = sha256_hex(data.decode("latin-1")) if res.body else ""
                return res
            return FetchResult(url, cur, None, "", b"", "error", "too_many_redirects", stamp, requests_used=self.requests - start_requests)
        except socket.timeout:
            return FetchResult(url, cur, None, "", b"", "timeout", "timeout", stamp, requests_used=self.requests - start_requests)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return FetchResult(url, cur, None, "", b"", "error", type(e).__name__, stamp, requests_used=self.requests - start_requests)


def _visible_len(html: str) -> int:
    s = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return len(" ".join(s.split()))


def classify_html(res: FetchResult) -> FetchResult:
    """Login walls, challenges and (JS) empty shells are failures, never data."""
    html = res.body[:600_000].decode("utf-8", "replace")
    vis = _visible_len(html)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = " ".join((title.group(1) if title else "").split())
    if CHALLENGE_RE.search(html[:20000]) and vis < 1500:
        res.outcome, res.error_class = "challenge", "bot_challenge"
    elif (PASSWORD_RE.search(html) or LOGIN_RE.search(title)) and vis < 1800:
        res.outcome, res.error_class = "login_wall", "login_required"
    elif vis < 120:
        scripts = len(re.findall(r"(?i)<script\b", html))
        res.outcome = "needs_render" if scripts >= 3 else "empty_body"
        res.error_class = "js_shell" if scripts >= 3 else "no_visible_text"
    if res.outcome != "ok":
        res.body = res.body if res.outcome == "needs_render" else b""
    return res
