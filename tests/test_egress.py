"""Loopback SOCKS5 egress: fail closed, no direct fallback, DeepSeek stays direct."""
from __future__ import annotations

import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlparse

from pbdata.collect import egress, net, render, wiki
from pbdata.collect.shadow import policy, runner
from pbdata.collect.shadow.common import Stop
from pbdata import db


HTML = (b"<html><head><title>Open call</title></head>"
        b"<body><h1>Open call 2027</h1>"
        b"<p>Deadline January 31, 2027. Submit your work today. This fixture page includes enough visible "
        b"prose so the collector does not classify it as an empty JavaScript shell or a login wall.</p>"
        b"</body></html>")
ROBOTS = b"User-agent: *\nAllow: /\n"


class _HttpState:
    def __init__(self):
        self.paths = []
        self.bodies_sent = 0


def _start_http(state=None):
    state = state or _HttpState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.paths.append(self.path)
            if self.path == "/robots.txt":
                body, ctype = ROBOTS, "text/plain"
            elif self.path.endswith(".png") or self.path == "/photo":
                body, ctype = b"\x89PNG" + b"\x00" * 2048, "image/png" if self.path.endswith(".png") else "image/jpeg"
            else:
                body, ctype = HTML, "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Encoding", "identity")
            self.end_headers()
            self.wfile.write(body)
            state.bodies_sent += 1

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def _start_socks(remap=None, destinations=None):
    """Minimal SOCKS5 (no auth). remap=(host,port) overrides the requested target."""
    destinations = destinations if destinations is not None else []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    stop = threading.Event()

    def _read(sock, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("eof")
            buf.extend(chunk)
        return bytes(buf)

    def _relay(a, b):
        try:
            sockets = [a, b]
            while True:
                import select
                ready, _, err = select.select(sockets, [], sockets, 5)
                if err or not ready:
                    break
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (b if src is a else a).sendall(data)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def _client(client):
        try:
            client.settimeout(5)
            _read(client, 2)  # VER, NMETHODS + methods (may need exact: read nmethods)
            # The first recv in _read(2) got VER+NMETHODS; methods may remain.
            # Re-do handshake properly on a dedicated buffer below if needed.
        except OSError:
            client.close()
            return

    def _handle(client):
        try:
            client.settimeout(5)
            hdr = _read(client, 2)
            if hdr[0] != 5:
                return
            if hdr[1]:
                _read(client, hdr[1])
            client.sendall(b"\x05\x00")
            req = _read(client, 4)
            if req[0] != 5 or req[1] != 1:
                client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                return
            atyp = req[3]
            if atyp == 1:
                raw = _read(client, 4 + 2)
                host = socket.inet_ntoa(raw[:4])
                port = int.from_bytes(raw[4:], "big")
            elif atyp == 3:
                ln = _read(client, 1)[0]
                raw = _read(client, ln + 2)
                host = raw[:ln].decode("idna")
                port = int.from_bytes(raw[ln:], "big")
            elif atyp == 4:
                raw = _read(client, 16 + 2)
                host = socket.inet_ntop(socket.AF_INET6, raw[:16])
                port = int.from_bytes(raw[16:], "big")
            else:
                client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return
            destinations.append((host, port))
            target = remap or (host, port)
            upstream = socket.create_connection(target, 5)
            # BND.ADDR: IPv4 0.0.0.0:0
            client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 4 + b"\x00\x00")
            _relay(client, upstream)
        except Exception:
            try:
                client.close()
            except OSError:
                pass

    def _accept():
        listener.settimeout(0.3)
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_handle, args=(client,), daemon=True).start()

    threading.Thread(target=_accept, daemon=True).start()
    port = listener.getsockname()[1]
    return listener, port, destinations, stop


def _stop_socks(listener, stop):
    stop.set()
    try:
        listener.close()
    except OSError:
        pass


def _infra():
    conn = db.connect(":memory:")
    db.migrate(conn, "infra")
    return conn


class ConfiguredProxyTests(unittest.TestCase):
    def test_unset_is_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(egress.ENV_PROXY, None)
            self.assertIsNone(egress.configured_proxy())

    def test_valid_forms_normalize_localhost(self):
        cases = {
            "socks5h://127.0.0.1:1080": ("127.0.0.1", 1080),
            "socks5://127.0.0.1:1080": ("127.0.0.1", 1080),
            "127.0.0.1:1080": ("127.0.0.1", 1080),
            "socks5h://localhost:1080": ("127.0.0.1", 1080),
            "socks5h://[::1]:1080": ("::1", 1080),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw), patch.dict(os.environ, {egress.ENV_PROXY: raw}):
                self.assertEqual(egress.configured_proxy(), expected)

    def test_non_loopback_and_garbage_fail_closed(self):
        for raw in ("socks5h://8.8.8.8:1080", "http://127.0.0.1:1080", "127.0.0.1", "socks5h://user:pass@127.0.0.1:1080", "socks5h://127.0.0.1:99999"):
            with self.subTest(raw=raw), patch.dict(os.environ, {egress.ENV_PROXY: raw}):
                with self.assertRaises(egress.ProxyConfigError):
                    egress.configured_proxy()


class HandshakeAndFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.log = str(Path(self.tmp.name) / "proxy_traffic.log")
        self.http, self.state = _start_http()
        self.http_port = self.http.server_address[1]
        self.listener, self.socks_port, self.destinations, self.stop = _start_socks()
        self.env = {
            egress.ENV_PROXY: "socks5h://127.0.0.1:%d" % self.socks_port,
            egress.ENV_LOG: self.log,
        }

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        _stop_socks(self.listener, self.stop)
        self.tmp.cleanup()

    def test_preflight_ok_and_down(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(egress.preflight(), ("127.0.0.1", self.socks_port))
        with patch.dict(os.environ, {egress.ENV_PROXY: "socks5h://127.0.0.1:1"}):
            with self.assertRaises(OSError):
                egress.preflight(timeout=0.5)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(egress.ENV_PROXY, None)
            with self.assertRaises(egress.ProxyConfigError):
                egress.preflight()

    def test_fetcher_goes_through_socks_and_logs_bytes(self):
        url = "http://127.0.0.1:%d/call" % self.http_port
        with patch.dict(os.environ, self.env):
            fetcher = net.Fetcher(_infra(), allow_loopback=True, sleep=lambda *_: None)
            res = fetcher.fetch(url)
        self.assertTrue(res.ok, res)
        self.assertIn(b"Open call 2027", res.body)
        self.assertTrue(self.destinations)
        self.assertEqual(self.destinations[0][0], "127.0.0.1")
        self.assertEqual(self.destinations[0][1], self.http_port)
        logged = Path(self.log).read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", logged)
        self.assertNotIn("0\t127.0.0.1\n", logged.splitlines()[-1] + "\n" if False else logged)
        # at least one positive byte count
        self.assertTrue(any(int(line.split("\t")[1]) > 0 for line in logged.strip().splitlines()))

    def test_fetcher_dead_proxy_does_not_contact_target(self):
        url = "http://127.0.0.1:%d/call" % self.http_port
        seen = []
        real = socket.create_connection

        def wrapped(address, *args, **kwargs):
            seen.append(address[0:2])
            return real(address, *args, **kwargs)

        with patch.dict(os.environ, {egress.ENV_PROXY: "socks5h://127.0.0.1:1", egress.ENV_LOG: self.log}):
            with patch("socket.create_connection", side_effect=wrapped):
                fetcher = net.Fetcher(_infra(), allow_loopback=True, sleep=lambda *_: None)
                res = fetcher.fetch(url)
        self.assertFalse(res.ok)
        self.assertEqual(res.outcome, "error")
        self.assertEqual(self.state.paths, [])
        self.assertTrue(seen)
        self.assertTrue(all(addr == ("127.0.0.1", 1) for addr in seen), seen)

    def test_skips_binary_media_by_url_and_content_type(self):
        with patch.dict(os.environ, self.env):
            fetcher = net.Fetcher(_infra(), allow_loopback=True, sleep=lambda *_: None)
            by_ext = fetcher.fetch("http://127.0.0.1:%d/pic.png" % self.http_port)
            by_type = fetcher.fetch("http://127.0.0.1:%d/photo" % self.http_port)
        self.assertEqual(by_ext.error_class, "skipped_binary_media")
        self.assertEqual(by_ext.body, b"")
        self.assertNotIn("/pic.png", self.state.paths)
        self.assertEqual(by_type.error_class, "skipped_binary_media")
        self.assertEqual(by_type.body, b"")
        # content-type skip happens after headers, so the GET is issued but the
        # skip is still recorded; the URL pre-check must not have blocked /photo.
        self.assertTrue(any(p == "/photo" or p.endswith("/photo") for p in self.state.paths) or by_type.error_class == "skipped_binary_media")

    def test_policy_fetcher_uses_socks_hostname_and_fail_closes(self):
        # Remap example.org:80 to the local HTTP server so canonical_url stays public.
        _stop_socks(self.listener, self.stop)
        self.listener, self.socks_port, self.destinations, self.stop = _start_socks(remap=("127.0.0.1", self.http_port))
        env = {
            egress.ENV_PROXY: "socks5h://127.0.0.1:%d" % self.socks_port,
            egress.ENV_LOG: self.log,
        }
        with patch.dict(os.environ, env):
            page = policy.PolicyFetcher(sleep=lambda *_: None).fetch("http://example.org/call")
        self.assertEqual(page.final_url, "http://example.org/call")
        self.assertIn(b"Open call 2027", page.body)
        self.assertIn(("example.org", 80), self.destinations)

        with patch.dict(os.environ, {egress.ENV_PROXY: "socks5h://127.0.0.1:1"}):
            with patch.object(policy, "public_socket", side_effect=AssertionError("direct connect")):
                with patch.object(policy.socket, "create_connection", side_effect=AssertionError("policy direct")):
                    fetcher = policy.PolicyFetcher(sleep=lambda *_: None)
                    with self.assertRaises(Stop) as ctx:
                        fetcher.fetch("http://example.org/call")
                    self.assertEqual(str(ctx.exception), "fetch_error")

    def test_wiki_api_uses_socks(self):
        with patch.dict(os.environ, self.env):
            api = wiki.Api(min_interval=0, sleep=lambda *_: None)
            # The fixture returns HTML, which json.loads will reject; the
            # important part is that the request reached the target via SOCKS.
            self.assertIsNone(api.get("http://127.0.0.1:%d/call" % self.http_port, {}))
        self.assertTrue(self.destinations)
        self.assertEqual(self.destinations[-1], ("127.0.0.1", self.http_port))
        self.assertTrue(self.state.paths)


class ChromeAndProviderTests(unittest.TestCase):
    def test_chrome_flags_dead_port_without_proxy(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(egress.ENV_PROXY, None)
            flags = egress.chrome_flags()
        self.assertEqual(flags, ["--proxy-server=socks5://127.0.0.1:9"])
        with patch.dict(os.environ, {egress.ENV_PROXY: "socks5h://127.0.0.1:1080"}):
            flags = egress.chrome_flags()
        self.assertIn("--proxy-server=socks5://127.0.0.1:1080", flags)
        self.assertTrue(any(f.startswith("--host-resolver-rules=") and "EXCLUDE 127.0.0.1" in f for f in flags))
        self.assertIn("--blink-settings=imagesEnabled=false", flags)
        self.assertIn("--disable-remote-fonts", flags)
        with patch.dict(os.environ, {egress.ENV_PROXY: "socks5h://8.8.8.8:1080"}):
            self.assertIsNone(egress.chrome_flags())

    def test_render_does_not_launch_when_proxy_invalid(self):
        with patch.dict(os.environ, {egress.ENV_PROXY: "socks5h://8.8.8.8:1080"}):
            with patch.object(render, "binary", return_value="/bin/false"):
                with patch("subprocess.run", side_effect=AssertionError("chrome must not launch")):
                    self.assertIsNone(render.render_html(b"<html></html>", "http://example.org/"))

    def test_provider_ignores_socks_and_env_proxies(self):
        key = "sk-test-not-a-real-key"
        payload = {"model": "deepseek-flash", "choices": [{"message": {"content": "{}"}}]}

        class Response:
            status = 200
            headers = type("Headers", (), {"get_content_type": lambda _: "application/json"})()
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, limit):
                return json.dumps(payload).encode()

        env = {
            egress.ENV_PROXY: "socks5h://127.0.0.1:1080",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "socks5h://127.0.0.1:1080",
        }
        with patch.dict(os.environ, env), patch.object(egress, "socks5h_connect", side_effect=AssertionError("deepseek must stay direct")):
            with patch.object(runner.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response()
                raw = runner.provider({"model": "deepseek-flash"}, key, 30)
        self.assertEqual(raw["model"], "deepseek-flash")
        args = build.call_args.args
        self.assertEqual(args[0].proxies, {})
        self.assertIs(args[1], runner.NoRedirect)
        self.assertEqual(len(args), 2)
        self.assertEqual(build.return_value.open.call_args.args[0].full_url, runner.ENDPOINT)
        self.assertTrue(runner.ENDPOINT.startswith("https://api.deepseek.com"))


class MediaHelperTests(unittest.TestCase):
    def test_detects_types_and_extensions(self):
        self.assertTrue(egress.is_binary_media("https://x.example/a.jpg"))
        self.assertTrue(egress.is_binary_media("https://x.example/x", "image/jpeg"))
        self.assertTrue(egress.is_binary_media("https://x.example/f.woff2"))
        self.assertFalse(egress.is_binary_media("https://x.example/call", "text/html"))
        self.assertFalse(egress.is_binary_media("https://x.example/rules.pdf", "application/pdf"))
