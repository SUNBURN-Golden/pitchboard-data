"""Loopback SOCKS5 egress for collector web fetches. Standard library only.

When PITCHBOARD_SOCKS5_PROXY is set, HTTP(S) clients in this package connect
through that proxy with remote DNS (SOCKS5h). The proxy must be a loopback
address. If the variable is set but invalid, or the proxy is unreachable,
fetches fail and never fall back to a direct connect.

DeepSeek calls in ``shadow.runner.provider`` do not use this module and keep
an empty ProxyHandler so they stay direct.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlsplit

import http.client

KST = timezone(timedelta(hours=9))
ENV_PROXY = "PITCHBOARD_SOCKS5_PROXY"
ENV_LOG = "PITCHBOARD_PROXY_TRAFFIC_LOG"
DEFAULT_LOG = "/workspace/pitchboard/data-root/logs/proxy_traffic.log"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MEDIA_PREFIXES = ("image/", "video/", "audio/", "font/")
MEDIA_EXACT = {
    "application/font-woff", "application/font-woff2", "application/font-sfnt",
    "application/vnd.ms-fontobject", "application/x-font-ttf", "application/x-font-otf",
    "application/x-font-woff",
}
MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff", ".avif",
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}


class ProxyConfigError(OSError):
    """The SOCKS5 proxy environment is set but unusable."""


def configured_proxy() -> Optional[Tuple[str, int]]:
    """Return (host, port) when a valid loopback proxy is configured.

    ``None`` means the variable is unset (callers may use the historical
    direct path). A set-but-invalid value raises ``ProxyConfigError`` so
    callers can fail closed instead of connecting directly.
    """
    raw = os.environ.get(ENV_PROXY)
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if "://" in text:
        parts = urlsplit(text)
        try:
            port = parts.port
        except ValueError as exc:
            raise ProxyConfigError("invalid_proxy") from exc
        if parts.scheme not in ("socks5", "socks5h") or parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ProxyConfigError("invalid_proxy")
        host = parts.hostname
    else:
        if text.count(":") != 1:
            raise ProxyConfigError("invalid_proxy")
        host, _, port_s = text.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise ProxyConfigError("invalid_proxy") from None
    if not host or port is None or not (1 <= port <= 65535):
        raise ProxyConfigError("invalid_proxy")
    host = host.lower().strip("[]")
    if host == "localhost":
        host = "127.0.0.1"
    if host not in LOOPBACK_HOSTS:
        raise ProxyConfigError("invalid_proxy")
    return host, int(port)


def preflight(timeout: float = 5.0) -> Tuple[str, int]:
    """Handshake the configured proxy. Raise if unset, invalid, or down."""
    proxy = configured_proxy()
    if proxy is None:
        raise ProxyConfigError("proxy_not_configured")
    sock = socket.create_connection(proxy, timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("socks5_auth")
    finally:
        sock.close()
    return proxy


def is_binary_media(url: str, content_type: str = "") -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if any(ctype.startswith(p) for p in MEDIA_PREFIXES) or ctype in MEDIA_EXACT:
        return True
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in MEDIA_EXT)


def log_traffic(nbytes: int, host: str, note: str = "") -> None:
    path = os.environ.get(ENV_LOG) or DEFAULT_LOG
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = "%s\t%d\t%s%s\n" % (
            datetime.now(KST).isoformat(timespec="seconds"),
            int(nbytes),
            host or "-",
            ("\t" + note) if note else "",
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        return


def chrome_flags() -> Optional[list]:
    """Chromium flags for render.py. ``None`` means fail closed (do not launch)."""
    try:
        proxy = configured_proxy()
    except ProxyConfigError:
        return None
    if proxy is None:
        # Historical behaviour: dead SOCKS port, so file:// render cannot leak a public IP.
        return ["--proxy-server=socks5://127.0.0.1:9"]
    host, port = proxy
    return [
        "--proxy-server=socks5://%s:%d" % (host, port),
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE %s" % host,
        "--blink-settings=imagesEnabled=false",
        "--disable-remote-fonts",
        "--mute-audio",
    ]


def urlopen(req, timeout=None):
    """urllib.urlopen that uses the loopback SOCKS5 proxy when configured."""
    try:
        proxy = configured_proxy()
    except ProxyConfigError:
        raise
    if proxy is None:
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        SocksHTTPHandler(proxy),
        SocksHTTPSHandler(proxy),
    )
    return opener.open(req, timeout=timeout)


def opener(*handlers):
    """build_opener that disables env proxies and inserts SOCKS handlers when configured.

    Returns (opener, proxy_or_none). Raises ProxyConfigError if the env is invalid.
    """
    proxy = configured_proxy()
    extra = [urllib.request.ProxyHandler({}), *handlers]
    if proxy is not None:
        extra.extend([SocksHTTPHandler(proxy), SocksHTTPSHandler(proxy)])
    return urllib.request.build_opener(*extra), proxy


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("socks5_eof")
        buf.extend(chunk)
    return bytes(buf)


def _dest_bytes(host: str, port: int) -> bytes:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        raw = host.encode("idna")
        if not raw or len(raw) > 255:
            raise OSError("socks5_host")
        return b"\x03" + bytes([len(raw)]) + raw + int(port).to_bytes(2, "big")
    atyp = b"\x01" if ip.version == 4 else b"\x04"
    return atyp + ip.packed + int(port).to_bytes(2, "big")


def socks5h_connect(proxy_host: str, proxy_port: int, dest_host: str, dest_port: int, timeout=None) -> socket.socket:
    """TCP connect via SOCKS5, sending the destination hostname for remote DNS."""
    sock = socket.create_connection((proxy_host, proxy_port), timeout)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("socks5_auth")
        sock.sendall(b"\x05\x01\x00" + _dest_bytes(dest_host, dest_port))
        hdr = _recv_exact(sock, 4)
        if hdr[0] != 5 or hdr[1] != 0:
            raise OSError("socks5_connect")
        atyp = hdr[3]
        if atyp == 1:
            _recv_exact(sock, 4 + 2)
        elif atyp == 4:
            _recv_exact(sock, 16 + 2)
        elif atyp == 3:
            ln = _recv_exact(sock, 1)[0]
            _recv_exact(sock, ln + 2)
        else:
            raise OSError("socks5_atyp")
        return sock
    except Exception:
        sock.close()
        raise


class SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, proxy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pb_proxy = proxy

    def connect(self):
        if not self._pb_proxy:
            raise OSError("socks5_not_configured")
        self.sock = socks5h_connect(self._pb_proxy[0], self._pb_proxy[1], self.host, self.port, self.timeout)


class SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, proxy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pb_proxy = proxy

    def connect(self):
        if not self._pb_proxy:
            raise OSError("socks5_not_configured")
        sock = socks5h_connect(self._pb_proxy[0], self._pb_proxy[1], self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy):
        super().__init__()
        self._pb_proxy = proxy

    def http_open(self, req):
        def factory(*args, **kwargs):
            return SocksHTTPConnection(*args, proxy=self._pb_proxy, **kwargs)
        return self.do_open(factory, req)


class SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy, **kwargs):
        super().__init__(**kwargs)
        self._pb_proxy = proxy

    def https_open(self, req):
        def factory(*args, **kwargs):
            return SocksHTTPSConnection(*args, proxy=self._pb_proxy, **kwargs)
        return self.do_open(factory, req, context=self._context)
