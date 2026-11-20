"""Evidence-backed overlay for clean builds of the committed site."""
from collections import defaultdict
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from pbdata import timeutil
from pbdata.collect.net import strip_tracking_params
from pbdata.collect.shadow.policy import Stop, canonical_url

from . import deploy, status

DEFAULT_REPO = Path("/workspace/pitchboard-site")
OUTLET_URL_FIELDS = ("source_url", "pitch_guidelines_url", "submission_url", "submission_guidelines_url")


class BuildError(Exception):
    pass


def collect_overlay(svc, ledger):
    source_urls = defaultdict(list)
    sources = {}
    for row in svc.execute("""
        SELECT source_id, owner_outlet_id, canonical_url FROM pb_source
        WHERE authority='official_primary' AND owner_outlet_id IS NOT NULL AND retired_reason IS NULL
            AND permission IN ('allowed_public','allowed_conditional')
        ORDER BY canonical_url
        """):
        try:
            canonical_url(row["canonical_url"])
        except Stop:
            continue
        source_urls[row["owner_outlet_id"]].append(row["canonical_url"])
        sources[row["source_id"]] = row["canonical_url"]
    checked = {}
    details = () if ledger is None else ledger.execute("SELECT detail FROM batches WHERE state='processed' ORDER BY finished_at,batch_id")
    for (detail_json,) in details:
        for item in json.loads(detail_json)["items"]:
            source_id = item["source_id"]
            if not item.get("accepted") or sources.get(source_id) != item.get("url"):
                continue
            checked[source_id] = {field: item[field] for field in (
                "outlet_id", "url", "final_url", "checked_on", "source_id", "raw_id", "fact_ids")}

    candidates = defaultdict(list)
    for row in svc.execute("""
        SELECT c.call_id, c.outlet_id, c.title_raw, f.fact_id, f.source_id, s.canonical_url AS url,
            json_extract(f.value_json,'$.date') AS deadline, r.raw_id, r.fetched_at, r.final_url
        FROM pb_call_edition c JOIN pb_fact f ON f.subject_id=c.call_id
        JOIN pb_source s ON s.source_id=f.source_id AND s.owner_outlet_id=c.outlet_id
        JOIN pb_evidence e ON e.evidence_id=f.evidence_id
        JOIN pb_source_revision rev ON rev.revision_id=e.revision_id
        JOIN pb_raw_ref r ON r.raw_id=(
            SELECT latest.raw_id FROM pb_raw_ref latest
            WHERE latest.source_id=s.source_id AND latest.outcome='ok' AND (
                latest.text_hash=rev.content_hash OR EXISTS (
                    SELECT 1 FROM pb_fact_confirmation conf JOIN pb_source_revision ver ON ver.revision_id=conf.revision_id
                    WHERE conf.fact_id=f.fact_id AND ver.content_hash=latest.text_hash
                )
            )
            ORDER BY latest.fetched_at DESC, latest.raw_id DESC LIMIT 1
        )
        WHERE c.status IN ('open','announced','extended') AND f.subject_type='call'
            AND f.predicate='deadline.closes_on' AND f.value_state='stated' AND f.epistemic='verified_fact'
            AND f.extractor='rules' AND f.superseded_at IS NULL
            AND NOT EXISTS (SELECT 1 FROM pb_conflict_group g WHERE g.group_id=f.conflict_group_id AND g.resolved_at IS NULL)
        ORDER BY c.call_id, r.fetched_at DESC, f.fact_id
        """):
        if row["source_id"] in sources and valid_date(row["deadline"]):
            candidates[row["call_id"]].append(row)
    calls = []
    for rows in candidates.values():
        if len({row["deadline"] for row in rows}) != 1:
            continue
        row = rows[0]
        call = {field: row[field] for field in ("call_id", "outlet_id", "title_raw", "deadline", "source_id", "url", "final_url", "raw_id")}
        call.update(last_checked=timeutil.parse_iso(row["fetched_at"]).astimezone(timeutil.KST).date().isoformat(),
                    fact_ids=sorted(r["fact_id"] for r in rows))
        calls.append(call)
    return {"source_urls": dict(source_urls), "outlets": [checked[key] for key in sorted(checked)], "calls": calls}


def valid_date(value):
    try:
        return timeutil.parse_date(value)
    except (TypeError, ValueError):
        return None


def url_key(url):
    try:
        p = urlsplit(strip_tracking_params(url))
        if p.scheme in ("http", "https") and p.hostname:
            return (p.hostname.lower().removeprefix("www.").rstrip("."), p.path.rstrip("/") or "/", p.query)
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def apply_overlay(outlets, calls, overlay, today):
    keys = defaultdict(set)
    by_id = {outlet["id"]: outlet for outlet in outlets}
    for outlet in outlets:
        for field in OUTLET_URL_FIELDS:
            key = url_key(outlet.get(field))
            if key:
                keys[key].add(outlet["id"])

    def match(entry):
        direct = set().union(*(keys.get(url_key(entry.get(field)), set()) for field in ("url", "final_url")))
        if not direct:
            direct = set().union(*(keys.get(url_key(url), set()) for url in overlay.get("source_urls", {}).get(entry["outlet_id"], [])))
        return by_id[next(iter(direct))] if len(direct) == 1 else None

    changes = 0
    checked_by_outlet = {}
    for entry in overlay.get("outlets", []):
        outlet = match(entry)
        if outlet is not None:
            outlet_id = outlet["id"]
            checked_by_outlet[outlet_id] = max(entry["checked_on"], checked_by_outlet.get(outlet_id, ""))
    for outlet_id, checked_on in checked_by_outlet.items():
        outlet = by_id[outlet_id]
        if checked_on > (outlet.get("last_checked") or ""):
            outlet["last_checked"] = checked_on
            changes += 1

    active = [call for call in calls["calls"]
              if (valid_date(call.get("deadline")) or dt.date.max) >= today]
    changes += len(calls["calls"]) - len(active)
    existing = {(call["outlet"].casefold(), call.get("deadline")) for call in active}
    ids = {call.get("id") for call in active}
    for entry in overlay.get("calls", []):
        outlet = match(entry)
        deadline = valid_date(entry.get("deadline"))
        if outlet is None or deadline is None or deadline < today:
            continue
        call_id = "auto-" + entry["call_id"]
        key = (outlet["name"].casefold(), entry["deadline"])
        if key in existing or call_id in ids:
            continue
        active.append(dict(id=call_id, title=entry["title_raw"] or "Open call", outlet=outlet["name"], type="Open call",
                           opens=None, deadline=entry["deadline"], link=entry["url"], pay=None,
                           details="Deadline from the outlet's own page.", last_checked=entry["last_checked"]))
        existing.add(key)
        ids.add(call_id)
        changes += 1
    if changes:
        calls["calls"] = active
        calls["updated"] = today.isoformat()
    return changes


def git(repo, *args, **kwargs):
    return subprocess.run(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL, **kwargs)


def site_commit(repo, *, fetch=True):
    if fetch:
        try:
            subprocess.run(["timeout", "60", "git", "-C", str(repo), "fetch", "-q", "origin", "main"],
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    local = git(repo, "rev-parse", "--verify", "refs/heads/main", stdout=subprocess.PIPE, text=True)
    if local.returncode != 0:
        raise BuildError("site_main_unavailable")
    remote = git(repo, "rev-parse", "--verify", "refs/remotes/origin/main", stdout=subprocess.PIPE, text=True)
    if remote.returncode == 0 and git(repo, "merge-base", "--is-ancestor", local.stdout.strip(),
                                     remote.stdout.strip(), stdout=subprocess.DEVNULL).returncode == 0:
        return remote.stdout.strip()
    return local.stdout.strip()


def build_site(root, repo, today, result, *, no_deploy=False, fetch=True, deploy_site=deploy.deploy, after_build=None):
    commit = site_commit(repo, fetch=fetch)
    result["commit"] = commit
    with tempfile.TemporaryDirectory(prefix="pb-handoff-site-") as tmp:
        work = Path(tmp)
        archive = work / "site.tar"
        git(repo, "archive", "--format=tar", "--output=" + str(archive), commit, stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(work)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        archive.unlink()
        outlets_path = work / "data/outlets.json"
        calls_path = work / "data/calls.json"
        outlets, calls = status.read_json(outlets_path, None), status.read_json(calls_path, None)
        overlay = status.read_json(root / "handoff/site_overlay.json", {})
        result["overlay_changes"] = apply_overlay(outlets, calls, overlay, today)
        if result["overlay_changes"]:
            status.atomic_json(outlets_path, outlets)
            status.atomic_json(calls_path, calls)
        build = subprocess.run([sys.executable, "build.py"], cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if build.returncode:
            raise BuildError("site_build_failed")
        dist = work / "dist"
        result["pages"] = sum(path.is_file() for path in dist.rglob("*.html"))
        if not (dist / "index.html").is_file() or result["pages"] < 80:
            raise BuildError("site_verification_failed")
        try:
            if not no_deploy:
                result.update(deploy_site(dist))
        finally:
            if after_build is not None:
                after_build(work / "data")
