import base64
from contextlib import closing, nullcontext, redirect_stdout
from copy import deepcopy
import datetime as dt
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import fixture_site
from helpers import add_outlet, add_source, service
from pbdata import db, timeutil
from pbdata.collect.shadow import policy, schema
from pbdata.handoff import batch, bot, daemon, deploy, replay, site, status

NOW = timeutil.parse_iso("2026-11-20T04:00:00Z")
URL = "https://fixture.example/calls/2026-fall"
SECRET = "qwen-fixture-key-should-never-be-persisted"


def provider_response(fields):
    return {"model": schema.QWEN_MODEL, "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(fields)}}]}


class FakeFetcher:
    def __init__(self, **options):
        self.options = options
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        _, _, html = fixture_site.build("/calls/2026-fall")
        html += b"<p>Write to submissions@example.org or 555-123-4567 for more information.</p>"
        return policy.Page(url, url, html, (url,), "live_verified")


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "handoff/qwen"
        self.inbox = self.folder / "inbox"
        self.live = batch.service_path(self.root)
        self.live.parent.mkdir()
        with closing(service()) as seed, closing(sqlite3.connect(self.live)) as destination:
            self.outlet_id = add_outlet(seed, "Fixture Review")
            self.source_id = add_source(seed, URL, outlet=self.outlet_id)
            seed.backup(destination)
        self.caps = status.read_json(bot.CONFIG, {})

    def item(self, *, qwen=None):
        item, fetched = bot.collect_page(
            {"source_id": self.source_id, "outlet_id": self.outlet_id, "url": URL},
            FakeFetcher(), "2026-11-20", self.caps, SECRET,
            lambda *args: provider_response(dict.fromkeys(schema.FIELDS)), {"llm_calls": 0}, lambda: NOW,
        )
        self.assertTrue(fetched)
        self.assertEqual("ok", item["outcome"])
        if qwen is not None:
            item["qwen"].update(qwen)
        return item

    def publish(self, *items, seconds=0):
        batch_id = batch.write_batch(self.inbox, items, NOW + dt.timedelta(seconds=seconds), "unknown", self.caps, 0,
                                     dict.fromkeys(batch.TOKEN_FIELDS, 0))
        return self.inbox / batch_id

    def run_daemon(self, **options):
        return daemon.run(self.root, self.root / "unused-site", clock=lambda: NOW, no_deploy=True,
                          build_site=options.pop("build_site", Mock()), alerts=options.pop("alerts", Mock(return_value=None)),
                          **options)

    def rows(self, sql):
        with closing(batch.readonly(self.live)) as svc:
            return [tuple(row) for row in svc.execute(sql)]

    def test_atomic_batch_manifest_last_and_digest(self):
        steps = []

        def write(path, data):
            self.assertTrue(path.parent.name.startswith(".tmp-qb"))
            self.assertEqual([], list(batch.ready_batches(self.inbox)))
            steps.append(path.name)
            status.write_file(path, data)

        with patch.object(batch, "write_file", side_effect=write):
            path = self.publish(self.item())
        self.assertEqual(["items.jsonl", "manifest.json"], steps)
        self.assertEqual([path], [entry for entry, _ in batch.ready_batches(self.inbox)])
        self.assertEqual([path], list(self.inbox.iterdir()))
        manifest = json.loads((path / "manifest.json").read_bytes())
        self.assertEqual(batch.SCHEMA, manifest["schema"])
        self.assertEqual(hashlib.sha256((path / "items.jsonl").read_bytes()).hexdigest(), manifest["items_sha256"])
        self.assertEqual(1, manifest["counts"]["ok"])

    def test_daemon_ignores_incomplete_and_corrupt_batches(self):
        incomplete = self.publish(self.item())
        (incomplete / "manifest.json").unlink()
        corrupt = self.publish(self.item(), seconds=1)
        (corrupt / "items.jsonl").write_bytes(b"damaged")
        staged = self.publish(self.item(), seconds=2)
        staged.rename(self.inbox / (".tmp-" + staged.name))
        build = Mock()
        result = self.run_daemon(build_site=build)
        self.assertEqual(0, result["batches_consumed"])
        self.assertEqual(0, result["batches_failed"])
        self.assertEqual([], self.rows("SELECT * FROM pb_raw_ref"))
        self.assertIsNone(result["backup_path"])
        build.assert_called_once()

    def test_daemon_logs_only_exception_classes_for_unexpected_phase_failures(self):
        build = Mock(side_effect=ValueError(SECRET))
        alerts = Mock(return_value=None)
        with patch.object(daemon, "database_phase", side_effect=sqlite3.OperationalError(SECRET)):
            with redirect_stdout(io.StringIO()) as logged:
                record = self.run_daemon(build_site=build, alerts=alerts, cards_mode="none")

        self.assertEqual([
            "handoff db phase failed (OperationalError)",
            "handoff site phase failed (ValueError)",
            "handoff daemon site_phase_error",
        ], logged.getvalue().splitlines())
        self.assertEqual("db_phase_error", record["db_error"])
        self.assertEqual("site_phase_error", record["site"]["error_code"])
        alerts.assert_called_once()

    def test_daemon_idempotency_and_review_only_values(self):
        item = self.item(qwen={"deadline": {"value": {"date": "2026-12-22", "timezone": None, "text": "December 22, 2026"},
                                             "quote": "December 22, 2026"}})
        path = self.publish(item)
        record = self.run_daemon()
        self.assertEqual((1, 1, 1), (record["batches_consumed"], record["items_ok"], record["calls_created"]))
        self.assertGreater(record["facts_promoted"], 0)
        self.assertTrue((self.folder / "processed" / path.name).is_dir())
        self.assertEqual([("2026-11-30",)], self.rows(
            "SELECT json_extract(value_json,'$.date') FROM pb_fact WHERE predicate='deadline.closes_on'"))
        self.assertEqual([("verified_fact", "rules")], self.rows("SELECT DISTINCT epistemic,extractor FROM pb_fact"))
        self.assertNotIn("2026-12-22", self.rows("SELECT detail FROM pb_qwen_handoff_receipt")[0][0])
        review = status.read_json(self.folder / "review" / (path.name + ".json"), {})
        self.assertEqual(["qwen_only", "rules_only"], review["items"][0]["agreements"]["deadline"])
        self.assertEqual(item["qwen"], review["items"][0]["qwen"])
        overlay = status.read_json(self.root / "handoff/site_overlay.json", {})
        self.assertEqual("2026-11-30", overlay["calls"][0]["deadline"])
        self.assertEqual(review["items"][0]["raw_id"], overlay["calls"][0]["raw_id"])
        self.assertTrue(overlay["calls"][0]["fact_ids"])
        projection = status.read_json(self.root / "exports/projection_latest.json", {})
        self.assertEqual("2026-11-30", projection["calls"][0]["closes_on"])

        # Simulate a prior ledger commit followed by a crash before moving the directory.
        (self.folder / "processed" / path.name).rename(path)
        self.assertEqual(0, self.run_daemon()["batches_consumed"])
        self.assertEqual([(1,)], self.rows("SELECT count(*) FROM pb_raw_ref"))
        self.assertEqual([(1,)], self.rows("SELECT count(*) FROM pb_call_edition"))
        with closing(batch.readonly(self.folder / "ledger.sqlite3")) as ledger:
            self.assertEqual([(path.name, "processed")],
                             [tuple(row) for row in ledger.execute("SELECT batch_id,state FROM batches")])
        self.assertEqual(1, status.read_json(self.root / "handoff/status.json", {})["batches_consumed_total"])

    def test_services_receipt_recovers_before_sidecar_ledger_commit(self):
        path = self.publish(self.item())
        with closing(db.connect(self.live)) as svc:
            replay.apply_batch(svc, path, (path / "items.jsonl").read_bytes(), timeutil.iso(NOW))
        self.assertFalse((self.folder / "ledger.sqlite3").exists())
        self.assertEqual(1, self.run_daemon()["batches_consumed"])
        self.assertEqual([(1,)], self.rows("SELECT count(*) FROM pb_raw_ref"))
        self.assertTrue((self.folder / "processed" / path.name).exists())

    def test_failed_batch_rolls_back_and_next_batch_applies(self):
        item = self.item()
        broken = self.publish(item, dict(item, page_text=None))
        valid = self.publish(item, seconds=1)
        result = self.run_daemon()
        self.assertEqual((1, 1), (result["batches_consumed"], result["batches_failed"]))
        self.assertTrue((self.folder / "failed" / broken.name).is_dir())
        self.assertEqual([(valid.name,)], self.rows("SELECT run_id FROM pb_raw_ref"))
        with closing(batch.readonly(self.folder / "ledger.sqlite3")) as ledger:
            state, detail = ledger.execute("SELECT state,detail FROM batches WHERE batch_id=?", (broken.name,)).fetchone()
        self.assertEqual("failed", state)
        self.assertEqual({"error_code": "invalid_item"}, json.loads(detail))
        self.assertEqual(0, self.run_daemon()["batches_failed"])
        self.assertEqual(1, status.read_json(self.root / "handoff/status.json", {})["batches_failed_total"])

    def test_daemon_applies_oldest_batches_up_to_limit(self):
        newer = self.publish(self.item(), seconds=10)
        older = self.publish(self.item())
        self.assertEqual(1, self.run_daemon(max_batches=1)["batches_consumed"])
        self.assertTrue(newer.is_dir())
        self.assertTrue((self.folder / "processed" / older.name).is_dir())

    def test_backup_precedes_writes_and_rotation_keeps_fourteen(self):
        backups = self.root / "backups"
        backups.mkdir()
        for day in range(1, 17):
            (backups / (replay.BACKUP_PREFIX + f"202611{day:02d}T040000.sqlite3")).touch()
        outside_rotation = backups / (replay.BACKUP_PREFIX + "manual.sqlite3")
        outside_rotation.touch()
        self.publish(self.item())
        record = self.run_daemon()
        self.assertEqual(14, len([path for path in backups.iterdir() if replay.BACKUP_NAME.fullmatch(path.name)]))
        self.assertTrue(outside_rotation.exists())
        with closing(batch.readonly(Path(record["backup_path"]))) as saved:
            self.assertEqual(0, saved.execute("SELECT count(*) FROM pb_raw_ref").fetchone()[0])
            self.assertIsNone(saved.execute(
                "SELECT name FROM sqlite_master WHERE name='pb_qwen_handoff_receipt'").fetchone())
        self.assertEqual([(1,)], self.rows("SELECT count(*) FROM pb_raw_ref"))

    def test_backup_failure_still_builds(self):
        path = self.publish(self.item())
        build = Mock()
        with patch.object(replay, "backup", side_effect=OSError("private details")):
            result = self.run_daemon(build_site=build)
        self.assertEqual("backup_failed", result["db_error"])
        self.assertEqual([], self.rows("SELECT * FROM pb_raw_ref"))
        self.assertFalse((self.folder / "ledger.sqlite3").exists())
        self.assertTrue(path.is_dir())
        build.assert_called_once()

    def test_busy_pipeline_still_builds_and_dispatches(self):
        build, alerts = Mock(), Mock(return_value=204)
        with patch.object(status, "locks", return_value=nullcontext(False)):
            result = self.run_daemon(build_site=build, alerts=alerts)
        self.assertEqual("qwen_bot_busy", result["outcome"])
        self.assertEqual(204, result["alerts_status"])
        self.assertFalse((self.folder / "ledger.sqlite3").exists())
        build.assert_called_once()
        alerts.assert_called_once()

    def test_bot_caps_include_repairs_and_success_ledger(self):
        with closing(db.connect(self.live)) as svc:
            for suffix in ("/more", "/third", "/fourth"):
                add_source(svc, "https://fixture.example" + suffix, outlet=self.outlet_id)
        caps = self.root / "caps.json"
        status.atomic_json(caps, dict(self.caps, max_pages_per_run=3, max_llm_calls_per_run=2, max_page_utf8_bytes=400))
        fetcher = FakeFetcher()
        transport = Mock(side_effect=[provider_response({"unexpected": None}), provider_response(dict.fromkeys(schema.FIELDS))])
        result = bot.run(self.root, config=caps, probe=lambda: True, fetcher_factory=Mock(return_value=fetcher),
                         transport=transport, key_loader=lambda: SECRET, clock=lambda: NOW)
        self.assertEqual((2, 1), (result["llm_calls"], result["pages_ok"]))
        self.assertEqual(1, len(fetcher.urls))
        self.assertEqual(3, len(transport.call_args.args[0]["messages"]))
        self.assertEqual(50, result["tokens"]["total_tokens"])
        path = self.inbox / result["batch_id"]
        item = json.loads((path / "items.jsonl").read_bytes())
        self.assertLessEqual(len(item["page_text"].encode()), 400)
        self.assertIn("[email]", item["page_text"])
        self.assertIn("[phone]", item["page_text"])
        self.assertNotIn(SECRET, (path / "items.jsonl").read_text())
        self.assertIn(fetcher.urls[0], status.read_json(self.folder / "state/success.json", {}))
        with closing(batch.readonly(self.live)) as svc:
            targets = list(bot.select_targets(svc, NOW, {fetcher.urls[0]: timeutil.iso(NOW)}))
        self.assertNotIn(fetcher.urls[0], [url for url, _ in targets])
        self.assertEqual([(0,)], self.rows("SELECT count(*) FROM pb_raw_ref"))

    def test_bot_page_cap_and_all_failures_still_publish(self):
        with closing(db.connect(self.live)) as svc:
            add_source(svc, "https://fixture.example/other", outlet=self.outlet_id)
        caps = self.root / "caps.json"
        status.atomic_json(caps, dict(self.caps, max_pages_per_run=1))
        fetcher = Mock()
        fetcher.fetch.side_effect = policy.FetchStop("denied_http", 403, URL)
        transport = Mock()
        result = bot.run(self.root, config=caps, probe=lambda: True, fetcher_factory=Mock(return_value=fetcher),
                         transport=transport, key_loader=lambda: SECRET, clock=lambda: NOW)
        self.assertEqual(1, result["pages_failed"])
        fetcher.fetch.assert_called_once()
        transport.assert_not_called()
        item = json.loads((self.inbox / result["batch_id"] / "items.jsonl").read_bytes())
        self.assertEqual(("blocked", 403), (item["outcome"], item["http_status"]))

    def test_bot_proxy_failure_is_closed_and_records_skip(self):
        fetcher_factory = Mock()
        result = bot.run(self.root, probe=lambda: False, fetcher_factory=fetcher_factory, clock=lambda: NOW)
        self.assertEqual("proxy_unavailable", result["outcome"])
        self.assertFalse(self.inbox.exists())
        self.assertEqual(result, json.loads((self.folder / "usage.jsonl").read_bytes()))
        self.assertEqual(result, status.read_json(self.root / "handoff/status.json", {})["last_bot_run"])
        fetcher_factory.assert_not_called()

    def test_target_priority_and_deduplication(self):
        self.publish(self.item())
        self.assertEqual(1, self.run_daemon()["batches_consumed"])
        unvisited = "https://fixture.example/uncrawled"
        recent = "https://fixture.example/recent"
        with closing(db.connect(self.live)) as svc:
            add_source(svc, unvisited, outlet=self.outlet_id)
            add_source(svc, recent, outlet=self.outlet_id)
            add_source(svc, "https://fixture.example/pending", outlet=self.outlet_id, permission="pending_review")
            add_source(svc, "https://fixture.example/?utm_source=copy", outlet=self.outlet_id)
            svc.execute("UPDATE pb_source SET last_success_at=? WHERE canonical_url=?",
                        (timeutil.iso(NOW - dt.timedelta(days=2)), recent))
            self.assertEqual([URL, unvisited, recent], [url for url, _ in bot.select_targets(svc, NOW, {})])
            self.assertEqual([unvisited, recent], [url for url, _ in bot.select_targets(svc, NOW, {URL: timeutil.iso(NOW)})])

    def test_proxy_probe_retries_socks_only(self):
        sleep = Mock()
        with patch.object(bot.egress, "preflight", return_value=("127.0.0.1", 1080)), \
                patch.object(bot.subprocess, "run", return_value=Mock(returncode=1)) as curl:
            self.assertFalse(bot.probe_proxy(sleep=sleep))
        self.assertEqual(5, curl.call_count)
        self.assertEqual([(60,)] * 4, [call.args for call in sleep.call_args_list])
        self.assertIn("--socks5-hostname", curl.call_args.args[0])
        self.assertIn("127.0.0.1:1080", curl.call_args.args[0])

    def test_policy_protects_robots_and_challenges(self):
        robots = b"User-agent: *\nDisallow: /private\n"
        with patch.object(policy.socks_egress, "configured_proxy", return_value=None):
            with self.assertRaisesRegex(policy.Stop, "proxy_not_configured"):
                policy.PolicyFetcher(require_proxy=True)
        with patch.object(policy.socks_egress, "configured_proxy", return_value=("127.0.0.1", 1080)):
            fetcher = policy.PolicyFetcher(require_proxy=True, sleep=lambda _: None)
        fetcher._get = Mock(side_effect=[(200, {"Content-Type": "text/plain"}, robots),
                                        (503, {"Content-Type": "text/html"}, b"<title>Just a moment...</title>")])
        with self.assertRaisesRegex(policy.Stop, "denied_robots"):
            fetcher.fetch("https://fixture.example/private")
        fetcher._get.assert_called_once()
        with self.assertRaisesRegex(policy.FetchStop, "access_wall") as blocked:
            fetcher.fetch(URL)
        self.assertEqual(503, blocked.exception.http_status)


class SiteTest(unittest.TestCase):
    def test_overlay_monotonic_idempotent_and_evidence_backed(self):
        outlets = [{"id": 1, "name": "Fixture Review", "source_url": "https://www.fixture.example/submit/",
                    "last_checked": "2026-11-01", "pays": "external", "notes": "curated"},
                   {"id": 2, "name": "Other", "source_url": "https://other.example/", "last_checked": "2026-11-30"}]
        calls = {"calls": [{"id": "past", "outlet": "Fixture Review", "deadline": "2026-11-19"},
                           {"id": "rolling", "outlet": "Fixture Review", "deadline": None},
                           {"id": "present", "outlet": "Fixture Review", "deadline": "2026-11-20"}], "updated": "2026-11-01"}
        observe = {"outlet_id": "db_outlet", "url": "https://fixture.example/submit", "checked_on": "2026-11-20",
                   "source_id": "src_fixture", "raw_id": "raw_fixture", "fact_ids": ["fact_fixture"]}
        candidate = dict(observe, call_id="call_fixture", title_raw="Fixture Writing Call",
                         deadline="2026-12-01", last_checked="2026-11-20", url=URL)
        overlay = {"source_urls": {"db_outlet": [observe["url"]]},
                   "outlets": [observe, dict(observe, outlet_id="other", url="https://other.example/", checked_on="2026-11-21")],
                   "calls": [candidate, dict(candidate, call_id="call_dup", deadline="2026-11-20")]}
        today = dt.date(2026, 11, 20)
        self.assertEqual(3, site.apply_overlay(outlets, calls, overlay, today))
        self.assertEqual(["rolling", "present", "auto-call_fixture"], [call["id"] for call in calls["calls"]])
        self.assertEqual("2026-11-20", outlets[0]["last_checked"])
        self.assertEqual("2026-11-30", outlets[1]["last_checked"])
        self.assertEqual(("external", "curated"), (outlets[0]["pays"], outlets[0]["notes"]))
        self.assertIsNone(calls["calls"][-1]["pay"])
        self.assertIsNone(calls["calls"][-1]["opens"])
        self.assertEqual("2026-11-20", calls["updated"])
        before = deepcopy((outlets, calls))
        self.assertEqual(0, site.apply_overlay(outlets, calls, overlay, today))
        self.assertEqual(before, (outlets, calls))

    def test_build_uses_committed_tree_and_checks_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "site"
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL, text=True).strip()
            git("init", "-b", "main")
            (repo / "data").mkdir()
            (repo / "data/outlets.json").write_text("[]")
            (repo / "data/calls.json").write_text(
                '{"calls": [{"outlet": "Fixture Review", "deadline": "2026-11-19"}]}'
            )
            build = repo / "build.py"
            build.write_text("from pathlib import Path\np = Path('dist')\np.mkdir()\n"
                             "(p / 'index.html').touch()\nfor i in range(79): (p / f'{i}.html').touch()\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-m", "fixture site")
            commit = git("rev-parse", "HEAD")
            build.write_text("raise RuntimeError('dirty worktree')")
            result = {}
            deploy_fake = Mock()
            site.build_site(root, repo, dt.date(2026, 11, 19), result, no_deploy=True, fetch=False, deploy_site=deploy_fake)
            self.assertEqual({"commit": commit, "pages": 80, "overlay_changes": 0}, result)
            deploy_fake.assert_not_called()
            plan = io.StringIO()
            with redirect_stdout(plan):
                self.assertEqual(0, daemon.main(["--root", str(root), "--site-repo", str(repo), "--dry-run"]))
            self.assertEqual(commit, json.loads(plan.getvalue())["site_commit"])
            self.assertFalse((root / "handoff").exists())

            deploy_fake.side_effect = deploy.DeployError("fixture failure")
            card_inputs = []

            def inspect_site_data(data_dir):
                card_inputs.append(status.read_json(data_dir / "calls.json", None))

            with self.assertRaisesRegex(deploy.DeployError, "fixture failure"):
                site.build_site(root, repo, NOW.date(), {}, fetch=False, deploy_site=deploy_fake,
                                after_build=inspect_site_data)
            deploy_fake.assert_called_once()
            self.assertEqual([{"calls": [], "updated": "2026-11-20"}], card_inputs)


class ApiTest(unittest.TestCase):
    def test_deploy_manifest_multipart_and_retry_with_fake_http(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dist = root / "dist"
            dist.mkdir()
            (dist / "index.html").write_bytes(b"hello")
            (dist / "other.html").write_bytes(b"hello")
            (dist / "file").write_bytes(b"data")
            (dist / ".hidden").write_bytes(b"data")
            for filename in deploy.SPECIAL_FILES:
                (dist / filename).write_bytes(b"special")
            token_file = root / "token"
            token_file.write_text("test-api-token\n")
            requests = []

            def opener(req, *, timeout):
                self.assertEqual(60, timeout)
                requests.append(req)
                url = req.full_url
                if url.endswith("/upload-token"):
                    self.assertEqual("GET", req.get_method())
                    self.assertEqual("Bearer test-api-token", req.get_header("Authorization"))
                    result = {"jwt": "test-upload-jwt"}
                elif "/pages/assets/" in url:
                    self.assertEqual("Bearer test-upload-jwt", req.get_header("Authorization"))
                    result = json.loads(req.data)["hashes"] if url.endswith("/check-missing") else []
                else:
                    self.assertTrue(url.endswith("/deployments"))
                    self.assertEqual("Bearer test-api-token", req.get_header("Authorization"))
                    if sum(request.full_url == url for request in requests) == 1:
                        raise HTTPError(url, 503, "fake", None, io.BytesIO(b"private response"))
                    result = {"id": "deployment-fixture", "url": "https://fixture.pitchboard.pages.dev"}
                response = io.BytesIO(json.dumps({"success": True, "result": result}).encode())
                response.status = 200
                return response

            result = deploy.deploy(dist, token_file=token_file, opener=opener, hasher=hashlib.sha256)
        self.assertEqual("deployment-fixture", result["deployment_id"])
        self.assertEqual(["upload-token", "check-missing", "upload", "upsert-hashes", "deployments", "deployments"],
                         [req.full_url.rsplit("/", 1)[1] for req in requests])
        fields = {}
        req = requests[-1]
        self.assertEqual(requests[-2].data, req.data)
        mime = BytesParser(policy=email_policy.default).parsebytes(
            ("Content-Type: " + req.get_header("Content-type") + "\r\n\r\n").encode() + req.data)
        for part in mime.iter_parts():
            fields[part.get_param("name", header="Content-Disposition")] = part
        html_hash = hashlib.sha256(base64.b64encode(b"hello") + b"html").hexdigest()[:32]
        file_hash = hashlib.sha256(base64.b64encode(b"data")).hexdigest()[:32]
        self.assertEqual({"/.hidden": file_hash, "/file": file_hash, "/index.html": html_hash, "/other.html": html_hash},
                         json.loads(fields["manifest"].get_payload(decode=True)))
        self.assertEqual(b"main", fields["branch"].get_payload(decode=True))
        for filename, content_type in deploy.SPECIAL_FILES.items():
            self.assertEqual(content_type, fields[filename].get_content_type())
            self.assertEqual(b"special", fields[filename].get_payload(decode=True))
        self.assertEqual({file_hash, html_hash}, {asset["key"] for asset in json.loads(requests[2].data)})

    def test_alerts_without_secret_are_quiet_and_offline(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as logged:
            opener = Mock()
            self.assertIsNone(deploy.dispatch_alerts(secret_file=Path(temp) / "absent", opener=opener))
            opener.assert_not_called()
            self.assertEqual("", logged.getvalue())


if __name__ == "__main__":
    unittest.main()
