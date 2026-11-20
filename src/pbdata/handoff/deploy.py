"""Direct Cloudflare Pages uploads and the post-deploy alerts trigger."""
import base64
import json
import mimetypes
from pathlib import Path
import re
import secrets
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from pbdata.collect.net import NoRedirect

TOKEN_FILE = Path("/home/box/.secrets/pitchboard_cf_pages_token.txt")
ALERTS_SECRET = Path("/home/box/.secrets/pitchboard_alerts_cron_secret")
ALERTS_URL = "https://pitchboard.soulbound.co.kr/api/internal/alerts/dispatch"
USER_AGENT = "PitchboardHandoffDaemon/1.0 (+https://pitchboard.soulbound.co.kr)"
API = "https://api.cloudflare.com/client/v4"
PROJECT = "/accounts/aa6bf084982ee841c3ea39b45895a15e/pages/projects/pitchboard"
SPECIAL_FILES = {
    "_worker.js": "application/javascript+module",
    "_headers": "text/plain",
    "_redirects": "text/plain",
    "_routes.json": "application/json",
}


class DeployError(Exception):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.http_status = http_status


def assets(dist, hasher):
    manifest, uploads = {}, {}
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(dist).as_posix()
        if relative in SPECIAL_FILES:
            continue
        encoded = base64.b64encode(path.read_bytes())
        extension = path.suffix[1:]
        digest = hasher(encoded + extension.encode("utf-8")).hexdigest()[:32]
        manifest["/" + relative] = digest
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        uploads.setdefault(digest, dict(key=digest, value=encoded.decode("ascii"),
                                       metadata={"contentType": content_type}, base64=True))
    return manifest, uploads


def deployment_body(dist, manifest):
    boundary = "pitchboard-" + secrets.token_hex(16)
    parts = []
    for name, value in (("manifest", json.dumps(manifest, sort_keys=True)), ("branch", "main")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, content_type in SPECIAL_FILES.items():
        path = dist / name
        if path.is_file():
            header = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                      f"Content-Type: {content_type}\r\n\r\n")
            parts.extend((header.encode(), path.read_bytes(), b"\r\n"))
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def direct_open():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open


def request(opener, path, token, data=None, content_type="application/json"):
    try:
        req = urllib.request.Request(API + path, data=data, headers={
            "Authorization": "Bearer " + token, "Content-Type": content_type, "User-Agent": USER_AGENT,
        })
        with opener(req, timeout=60) as response:
            if not 200 <= response.status < 300:
                raise DeployError("pages_http_error", response.status)
            envelope = json.load(response)
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            raise DeployError("pages_api_error")
        return envelope.get("result")
    except urllib.error.HTTPError as exc:
        exc.close()
        raise DeployError("pages_http_error", exc.code) from None
    except DeployError:
        raise
    except Exception:
        raise DeployError("pages_request_error") from None


def deploy(dist, *, token_file=TOKEN_FILE, opener=None, hasher=None):
    if hasher is None:
        try:
            from blake3 import blake3
        except ImportError:
            raise DeployError("blake3_unavailable") from None
        hasher = blake3
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise DeployError("pages_token_unavailable") from None
    if not token:
        raise DeployError("pages_token_unavailable")
    opener = opener or direct_open()
    manifest, uploads = assets(dist, hasher)
    result = request(opener, PROJECT + "/upload-token", token)
    jwt = result.get("jwt") if isinstance(result, dict) else None
    if not isinstance(jwt, str) or not jwt:
        raise DeployError("pages_upload_token_invalid")

    def send(path, value):
        return request(opener, "/pages/assets/" + path, jwt, json.dumps(value).encode())

    hashes = sorted(uploads)
    for start in range(0, len(hashes), 5000):
        group = hashes[start:start + 5000]
        missing = send("check-missing", {"hashes": group})
        if not isinstance(missing, list) or any(not isinstance(key, str) or key not in uploads for key in missing):
            raise DeployError("pages_missing_hashes_invalid")
        missing = sorted(set(missing))
        for offset in range(0, len(missing), 20):
            send("upload", [uploads[key] for key in missing[offset:offset + 20]])
        send("upsert-hashes", {"hashes": group})

    body, content_type = deployment_body(dist, manifest)
    try:
        result = request(opener, PROJECT + "/deployments", token, body, content_type)
    except DeployError as exc:
        if exc.http_status is None or not 500 <= exc.http_status < 600:
            raise
        result = request(opener, PROJECT + "/deployments", token, body, content_type)
    if not isinstance(result, dict):
        raise DeployError("pages_deployment_invalid")
    deployment_id, url = result.get("id"), result.get("url")
    if (not isinstance(deployment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", deployment_id)
            or not isinstance(url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+/?", url)
            or not urlsplit(url).hostname or any(s in deployment_id or s in url for s in (token, jwt))):
        raise DeployError("pages_deployment_invalid")
    return {"deploy_success": True, "deployment_id": deployment_id, "url": url}


def alerts_error(response):
    try:
        body = response.read(1024).strip()
        cf_error = re.fullmatch(rb"error code: ([0-9]{4})", body)
        if cf_error:
            return "cf_error_" + cf_error[1].decode("ascii")
        payload = json.loads(body)
        code = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
            return code
    except (OSError, ValueError):
        pass
    return ""


def dispatch_alerts(*, secret_file=ALERTS_SECRET, opener=None, dry_run=False):
    error = ""
    try:
        secret = secret_file.read_text(encoding="utf-8").strip()
        if not secret:
            return None
        url = ALERTS_URL + ("?dry_run=1" if dry_run else "")
        req = urllib.request.Request(url, data=b"{}", headers={
            "X-Pitchboard-Cron": secret, "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        with (opener or direct_open())(req, timeout=60) as response:
            http_status = response.status
            if not 200 <= http_status < 300:
                error = alerts_error(response)
    except urllib.error.HTTPError as exc:
        with exc:
            http_status = exc.code
            error = alerts_error(exc)
    except (OSError, ValueError):
        return None
    detail = f" {error}" if error and secret not in error else ""
    print(f"handoff alerts HTTP {http_status}{detail}", flush=True)
    return http_status
