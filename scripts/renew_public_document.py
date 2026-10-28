#!/usr/bin/env python3
"""Weekly renewal of the live crawler's public_document approvals (approved default #3, standing permission 2026-10-28).

For every URL whose newest `public` policy expires within --horizon hours: skip the never-collect domains
(config/blocked_hosts.txt), re-check anonymously (robots.txt allows our UA, plain GET returns 200 with no login wall),
then call the crawler's own InboxStore.approve_public() for 7 days. Backs up the crawler DB first (sqlite online backup).
Never restarts or signals the crawler daemon. URLs that fail the check are NOT renewed and are listed in the report.
"""
import argparse, json, os, sqlite3, sys, time, urllib.request, urllib.robotparser
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "PitchboardBot/0.1 (+https://pitchboard.soulbound.co.kr; research collector; honors robots.txt)"


def blocked():
    out = set()
    for line in open(os.path.join(HERE, "..", "config", "blocked_hosts.txt")):
        line = line.split("#")[0].strip().lower()
        if line:
            out.add(line)
    return out


def is_blocked(host, bl):
    host = host.lower().split(":")[0]
    return any(host == b or host.endswith("." + b) for b in bl)


def anon_ok(url):
    s = urlsplit(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = urllib.request.urlopen(urllib.request.Request("%s://%s/robots.txt" % (s.scheme, s.netloc), headers={"User-Agent": UA}), timeout=15)
        rp.parse(r.read(500000).decode("utf-8", "replace").splitlines())
        if not rp.can_fetch(UA, url):
            return False, "robots_disallow"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "robots_%d" % e.code
    except Exception as e:
        return False, "robots_error:%s" % type(e).__name__
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20)
        body = r.read(800000).decode("utf-8", "replace").lower()
        if r.status != 200:
            return False, "http_%d" % r.status
        if ("type=\"password\"" in body or "type='password'" in body) and len(body) < 20000:
            return False, "login_wall"
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, "http_%d" % e.code
    except Exception as e:
        return False, "fetch_error:%s" % type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawler", default="/workspace/pitchboard/crawler")
    ap.add_argument("--root", default=os.environ.get("PB_DATA_ROOT", "/workspace/pitchboard/data-root"))
    ap.add_argument("--horizon", type=float, default=48.0, help="renew when newest approval expires within N hours")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dbp = os.path.join(a.crawler, "var", "inbox.sqlite3")
    now = time.time()
    ro = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
    rows = ro.execute("SELECT url, max(expires), (SELECT data FROM policy_registry p2 WHERE p2.kind='public' AND p2.url=p.url ORDER BY expires DESC LIMIT 1) "
                      "FROM policy_registry p WHERE kind='public' GROUP BY url").fetchall()
    ro.close()
    bl = blocked()
    due = [(u, exp, json.loads(d or "{}")) for u, exp, d in rows if exp < now + a.horizon * 3600]
    report = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "urls_total": len(rows), "due": len(due), "renewed": [], "skipped_blocked": [], "not_renewed": []}
    if not due:
        print(json.dumps(report, indent=1)); return 0
    if not a.dry_run:
        os.makedirs(os.path.join(a.root, "backups"), exist_ok=True)
        bk = os.path.join(a.root, "backups", "inbox_before_renewal_%s.sqlite3" % time.strftime("%Y%m%dT%H%M%S"))
        src, dst = sqlite3.connect(dbp, timeout=30), sqlite3.connect(bk)
        src.backup(dst); dst.close(); src.close()
        report["backup"] = bk
        sys.path.insert(0, a.crawler)
        from inbox_pipeline import InboxStore
        store = InboxStore(dbp)
    for u, exp, data in due:
        host = urlsplit(u).hostname or ""
        if is_blocked(host, bl):
            report["skipped_blocked"].append(u); continue
        ok, why = anon_ok(u)
        if not ok:
            report["not_renewed"].append({"url": u, "reason": why}); continue
        if a.dry_run:
            report["renewed"].append({"url": u, "dry_run": True}); continue
        pid = store.approve_public(u, "operator:weekly-renewal-%s" % time.strftime("%Y-%m-%d"), data.get("sections", []), expires_in=a.days * 86400)
        report["renewed"].append({"url": u, "policy_id": pid})
        time.sleep(1)
    out = os.path.join(a.root, "exports", "renewal_%s.json" % time.strftime("%Y%m%d"))
    json.dump(report, open(out, "w"), indent=1)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
