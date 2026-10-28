"""Browser render for dynamic pages only (never to get around a denial). Renders the ALREADY-fetched, robots-cleared HTML
from a local temp file with the network disabled (inline scripts only). Pages that need XHR stay 'needs_render' failures."""
from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile
from typing import Optional

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
        try:
            p = subprocess.run([b, "--no-sandbox", "--disable-gpu", "--proxy-server=socks5://127.0.0.1:9", "--virtual-time-budget=4000",
                                "--dump-dom", "file://" + path], capture_output=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError):
            return None
        return p.stdout or None
