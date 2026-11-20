import datetime as dt
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from pbdata import timeutil
from pbdata.handoff import card_layout, cards, daemon, deploy, status

THURSDAY = dt.date(2026, 11, 19)
TUESDAY = dt.date(2026, 11, 17)


def render_fixture(svg_path, png_path, width, height):
    ElementTree.parse(svg_path)
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height))


def outlet(name, sort_value, pay=None, **fields):
    return dict(name=name, pay_usd_max=sort_value, pays=pay, last_checked="2026-11-15", **fields)


def call(id_, *, deadline=THURSDAY, opens=None, pay=None, **fields):
    return dict(id=id_, title=f"Call {id_}", outlet=f"Review {id_}", pay=pay,
                deadline=deadline.isoformat() if deadline else None, opens=opens, **fields)


def svg_text(path):
    return [node.text for node in ElementTree.parse(path).getroot().findall("{*}text")]


class CardTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.out = self.root / "cards"

    def write(self, filename, value):
        status.atomic_json(self.data / filename, value)

    def generate(self, kind, date=THURSDAY, **opts):
        return cards.generate_card(kind, date, self.data, self.out,
                                   renderer=opts.pop("renderer", render_fixture), **opts)

    def test_top10_uses_site_sort_and_verbatim_pay(self):
        outlets = [
            outlet("Featured", 9999, "$9,999"),
            outlet("Paid slot", 9100, "$9,100", sponsored=True),
            outlet("Unknown", None),
            outlet("Zebra", 81, "CAD $65-$112"),
            outlet("amber", 81, "£61", status="closed"),
            outlet("Closed High", 200, "$200", status="closed"),
            outlet("Reviewed", 55, "$55 per review"),
        ]
        self.write("outlets.json", outlets)
        self.write("sponsors.json", {"featured": [{"outlet": "Featured"}]})
        dest = self.generate("top10", TUESDAY)
        meta = json.loads((dest / "top10-meta.json").read_text())
        rows = meta["rows"]
        self.assertEqual(["Closed High", "amber", "Zebra", "Reviewed"], [r["outlet_name"] for r in rows])
        self.assertEqual(["$200", "£61", "CAD $65-$112", "$55 per review"], [r["rate_as_listed"] for r in rows])
        self.assertEqual(["", "", "", "per review"], [r["unit"] for r in rows])
        self.assertEqual("2026-11-15", meta["source_data_date"])
        self.assertFalse(meta["posted"])
        self.assertIn("Ranking uses the directory’s dated USD conversion.", (dest / "top10-x.svg").read_text())

    def test_deadlines_uses_open_calls_and_an_inclusive_eastern_window(self):
        eastern_start = THURSDAY - dt.timedelta(days=1)
        self.write("calls.json", {"updated": "2026-11-12", "calls": [
            call("rolling", deadline=None),
            call("last", deadline=eastern_start + dt.timedelta(days=7), pay="Honorarium paid after publication"),
            call("past", deadline=eastern_start - dt.timedelta(days=1)),
            call("future", deadline=eastern_start + dt.timedelta(days=8)),
            call("not-open", opens=THURSDAY.isoformat()),
            call("cancelled", status="cancelled"),
            call("invalid-opening-date", opens="28 Nov"),
            call("z", deadline=eastern_start, pay="Unpaid"),
            call("a", deadline=eastern_start),
        ]})
        dest = self.generate("deadlines", deadline_start=eastern_start)
        meta = json.loads((dest / "deadlines-meta.json").read_text())
        self.assertEqual("2026-11-12", meta["source_data_date"])
        self.assertEqual({"start": "2026-11-18", "end": "2026-11-25", "timezone": "America/New_York"},
                         meta["deadline_window"])
        self.assertEqual(["Call a", "Call z", "Call last"], [r["call_title"] for r in meta["rows"]])
        self.assertEqual(["Not recorded", "Unpaid", "Honorarium"], [r["rate_as_listed"] for r in meta["rows"]])
        self.assertIn("Rates as listed on 19 Nov 2026. Check each outlet for current terms.",
                      (dest / "deadlines-x.svg").read_text())

    def test_ellipsis_limits_and_portrait_wrap(self):
        name = "The Journal of Very Interesting Book Reviews and Critical Essays"
        title = "A Weekly Reading Period for Critical Essays and Very Long Book Reviews"
        self.write("outlets.json", [outlet(name, 1000, "On web, 75c/word up to $1000",
                                          unit="per printed review page")])
        self.write("calls.json", {"calls": [dict(call("one", pay="AUD $7,000 for a fellowship"),
                                                title=title, outlet=name)]})
        dest = self.generate("top10")
        top_x = svg_text(dest / "top10-x.svg")
        self.assertIn("The Journal of Very Interesting…", top_x)
        self.assertIn("per printed…", top_x)
        self.assertIn("$1000…", top_x)
        top_portrait = svg_text(dest / "top10-portrait.svg")
        self.assertIn("The Journal of Very Interesting", top_portrait)
        self.assertIn("Book…", top_portrait)
        dest = self.generate("deadlines")
        deadline_x = svg_text(dest / "deadlines-x.svg")
        self.assertIn(card_layout.ellipsize(title, 42), deadline_x)
        self.assertIn("The Journal of Very…  ·  closes 19 Nov 2026  ·  AUD $7,000 as listed", deadline_x)
        deadline_portrait = svg_text(dest / "deadlines-portrait.svg")
        lines = card_layout.portrait_lines(title, 42, 56)
        self.assertEqual(2, len(lines))
        self.assertLessEqual(len(" ".join(lines)), 56)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(all(line in deadline_portrait for line in lines))
        self.assertEqual("one two…", card_layout.ellipsize("one two three", 8))
        self.assertEqual("…", card_layout.ellipsize("Unbreakable", 5))

    def test_row_limits_notes_and_channel_specific_copy(self):
        self.write("outlets.json", [outlet(f"Review {i}", i, "$100") for i in range(1, 13)])
        calls = [call(f"{i:02d}", pay="$100", sponsored=(i == 1)) for i in range(1, 13)]
        self.write("calls.json", {"calls": calls, "updated": "2026-11-19"})
        for kind, path, counts in (("top10", "/outlets/", (10, 10)), ("deadlines", "/calls/", (8, 10))):
            with self.subTest(kind=kind):
                dest = self.generate(kind)
                self.assertEqual(8, len(list(dest.glob(f"{kind}-*"))))
                for layout, count in zip(cards.LAYOUTS, counts):
                    texts = svg_text(dest / f"{kind}-{layout}.svg")
                    self.assertEqual([f"{i:02d}" for i in range(1, count + 1)],
                                     [text for text in texts if text.isdigit()])
                    if kind == "deadlines":
                        self.assertIn(f"And {12 - count} more on The Pitchboard", texts[-1])
                        self.assertIn("Sponsored", texts)
                copies = set()
                for channel in cards.CHANNELS:
                    body = (dest / f"{kind}-copy-{channel}.txt").read_text()
                    copies.add(body.split("\n\n", 1)[-1].split("https://", 1)[0])
                    link = "https://" + body.split("https://", 1)[-1].strip()
                    url = urlsplit(link)
                    self.assertEqual(("pitchboard.soulbound.co.kr", path), (url.netloc, url.path))
                    query = dict(utm_source=[channel], utm_medium=["social"], utm_campaign=["pb_weekly"],
                                 utm_content=[f"20261119_{kind}"])
                    if kind == "top10":
                        query["sort"] = ["pay"]
                    self.assertEqual(query, parse_qs(url.query))
                    self.assertIn("19 Nov 2026", body)
                    if channel == "x":
                        self.assertLessEqual(len(body.strip()) - len(link) + 23, 280)
                    if channel == "threads":
                        self.assertLessEqual(len(body.strip()), 500)
                    if channel == "reddit":
                        self.assertIn("I run The Pitchboard", body)
                        self.assertEqual(10, len([line for line in body.splitlines() if line.split(".")[0].isdigit()]))
                        if kind == "deadlines":
                            self.assertIn("[Sponsored]", body)
                self.assertEqual(3, len(copies))

    def test_placeholder_and_empty_slots_fail_without_outputs(self):
        for marker in cards.PLACEHOLDERS:
            with self.subTest(marker=marker):
                self.write("outlets.json", [outlet("Review", 100, f"$100 per review ({marker})")])
                with self.assertRaisesRegex(cards.CardError, "forbidden marker"):
                    self.generate("top10")
                self.assertFalse((self.out / THURSDAY.isoformat()).exists())
        self.write("outlets.json", [outlet("", 100, "$100")])
        with self.assertRaisesRegex(cards.CardError, "empty outlet_name"):
            self.generate("top10")
        self.write("calls.json", {"calls": [call("rolling", deadline=None)]})
        with self.assertRaisesRegex(cards.CardError, "no eligible rows"):
            self.generate("deadlines")
        self.assertFalse(self.out.exists())

    def test_svg_guard_and_renderer_failure_publish_nothing(self):
        self.write("outlets.json", [outlet("Review & Journal", 100, "$100")])
        fake_layout = {"x": cards.TEMPLATES["top10"]["x"],
                       "portrait": lambda *_: '<svg><text>Outlet Name</text></svg>'}
        with patch.dict(cards.TEMPLATES, top10=fake_layout):
            with self.assertRaisesRegex(cards.CardError, "portrait SVG contains forbidden marker"):
                self.generate("top10")
        self.assertEqual([], list(self.out.iterdir()))
        with self.assertRaisesRegex(cards.CardError, "PNG renderer did not create"):
            self.generate("top10", renderer=lambda *_: None)
        self.assertEqual([], list(self.out.iterdir()))
        with patch.dict("os.environ", {"PB_CARDS_PYTHON": str(self.root / "missing-python")}):
            with self.assertRaisesRegex(cards.CardError, "PNG rendering failed"):
                self.generate("top10", renderer=cards.render_png)
        self.assertEqual([], list(self.out.iterdir()))

    def test_complete_outputs_are_reused_and_failed_force_preserves_them(self):
        self.write("outlets.json", [outlet("First", 100, "$100")])
        dest = self.generate("top10")
        copy = dest / "top10-copy-reddit.txt"
        self.write("outlets.json", [outlet("Outlet Name", 100, "$100")])
        rerender = Mock()
        self.assertEqual(dest, self.generate("top10", renderer=rerender))
        rerender.assert_not_called()
        with self.assertRaises(cards.CardError):
            self.generate("top10", force=True)
        self.assertIn("First — $100", copy.read_text())
        self.write("outlets.json", [outlet("Second", 150, "$150")])
        self.assertEqual(dest, self.generate("top10", force=True))
        self.assertIn("Second — $150", copy.read_text())


class CardDaemonTest(unittest.TestCase):
    def test_schedule_is_in_kst(self):
        for day in range(7):
            date = dt.date(2026, 11, 16) + dt.timedelta(days=day)
            self.assertEqual({1: ("top10",), 3: ("deadlines",)}.get(day, ()), cards.scheduled_kinds("auto", date))
        self.assertEqual(("top10", "deadlines"), cards.scheduled_kinds("both", THURSDAY))
        self.assertEqual((), cards.scheduled_kinds("none", THURSDAY))
        now = timeutil.parse_iso("2026-11-18T15:01:00Z")
        with tempfile.TemporaryDirectory() as temp, patch.object(daemon, "database_phase"):
            root = Path(temp)
            generate = Mock(return_value=root / "cards")

            def build(*args, after_build, **kwargs):
                after_build(root / "overlaid-data")

            record = daemon.run(root, root, clock=lambda: now, build_site=build,
                                alerts=lambda: None, generate_card=generate)
            generate.assert_called_once_with("deadlines", THURSDAY, root / "overlaid-data",
                                             deadline_start=dt.date(2026, 11, 18))
            self.assertTrue(record["cards"][0]["ok"])

    def test_failure_in_one_card_does_not_stop_other_card_or_alerts(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(daemon, "database_phase"):
            root = Path(temp)
            alerts = Mock(return_value=204)
            generate = Mock(side_effect=[RuntimeError("renderer failed"), root / "cards"])

            def build(*args, after_build, **kwargs):
                after_build(root / "data")

            with self.assertLogs("pbdata.handoff.daemon", level="ERROR"):
                record = daemon.run(root, root, cards_mode="both", build_site=build, alerts=alerts,
                                    clock=lambda: timeutil.parse_iso("2026-11-19T04:00:00Z"), generate_card=generate)
            self.assertEqual("ok", record["outcome"])
            self.assertEqual(204, record["alerts_status"])
            alerts.assert_called_once()
            self.assertEqual([
                {"kind": "top10", "ok": False, "out_dir": None, "error": "renderer failed"},
                {"kind": "deadlines", "ok": True, "out_dir": str(root / "cards"), "error": None},
            ], status.read_json(root / "handoff/status.json", {})["last_daemon_run"]["cards"])

    def test_daemon_records_site_and_card_results_independently(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(daemon, "database_phase"):
            root = Path(temp)
            generate = Mock(return_value=root / "cards")

            def build(*args, after_build, **kwargs):
                try:
                    raise deploy.DeployError("deploy_fixture")
                finally:
                    after_build(root / "overlaid-data")

            record = daemon.run(root, root, cards_mode="top10", build_site=build, alerts=lambda: 204,
                                generate_card=generate)
            self.assertEqual("deploy_fixture", record["outcome"])
            self.assertEqual(204, record["alerts_status"])
            self.assertTrue(record["cards"][0]["ok"])
