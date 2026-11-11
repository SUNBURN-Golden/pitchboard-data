"""Browser render for dynamic pages only (never to get around a denial). Renders the ALREADY-fetched, robots-cleared HTML
from a local temp file. Without PITCHBOARD_SOCKS5_PROXY the network is disabled via a dead SOCKS port (inline scripts only).
With the loopback SOCKS5 proxy, remote DNS is forced and images, fonts, and media are blocked; a down or invalid proxy
fails closed and does not fall back to a direct connect."""
from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile
from typing import Optional

from . import egress

CANDIDATES = [os.environ.get("PB_RENDER_BIN", "")] + glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))


def binary() -> Optional[str]:
    for c in CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def render_html(body: bytes, base_url: str, timeout: int = 25) -> Optional[bytes]:
    b = binary()
    if not b:
        return None
    html = body.decode("utf-8", "replace")
    html = re.sub(r"(?is)<base\b[^>]*>", "", html)
    html = re.sub(r"(?i)<head([^>]*)>", r'<head\1><base href="%s">' % base_url.replace('"', ""), html, count=1)
    with tempfile.TemporaryDirectory(prefix="pbrender") as tmp:
        path = os.path.join(tmp, "page.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        flags = egress.chrome_flags()
        if flags is None:
            return None
        try:
            p = subprocess.run([b, "--no-sandbox", "--disable-gpu", "--virtual-time-budget=4000",
                                *flags, "--dump-dom", "file://" + path], capture_output=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError):
            return None
        return p.stdout or None
