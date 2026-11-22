"""Synthetic fixtures only: an unchanged refetch relabels content last labeled by an older labeler."""
import unittest
from unittest.mock import Mock, patch

from helpers import NOW, add_source, service
from pbdata.collect import executor
from pbdata.collect.net import FetchResult
from pbdata.profiles import pitchboard as prof

URL = "https://example.org/positions"
T2 = "2026-10-29T00:00:00Z"
T3 = "2026-10-30T00:00:00Z"
HIRING = (b"<html><head><title>Open positions at the joint committee</title></head><body>"
          b"<h1>Open positions</h1><p>We are hiring a research assistant. Applications are open now.</p>"
          b"<p>Application deadline: December 4, 2026.</p></body></html>")


class RelabelUnchangedTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.sid = add_source(self.svc, URL)

    def fetch(self, now, body=HIRING):
        fetcher = Mock()
        fetcher.fetch.return_value = FetchResult(URL, URL, 200, "text/html", body, "ok")
        return executor.fetch_page(self.svc, fetcher, URL, self.sid, now)

    def versions(self, raw_id):
        return {r[0] for r in self.svc.execute("SELECT labeler_version FROM pb_label WHERE raw_id=?", (raw_id,))}

    def test_unchanged_content_relabeled_once_after_labeler_bump(self):
        with patch.object(prof, "LABELER_VERSION", "pb-labels-old"):
            first = self.fetch(NOW)
        self.assertEqual(first.change_state, "new")
        self.assertEqual(self.versions(first.raw_id), {"pb-labels-old"})
        second = self.fetch(T2)
        self.assertEqual(second.change_state, "unchanged")
        self.assertTrue(second.reextracted)
        self.assertEqual(self.versions(second.raw_id), {prof.LABELER_VERSION})
        self.assertTrue(second.labels)
        third = self.fetch(T3)
        self.assertEqual(third.change_state, "unchanged")
        self.assertFalse(third.reextracted)            # current labels exist for this content: no churn
        self.assertEqual(self.versions(third.raw_id), set())

    def test_current_labels_do_not_trigger_reextract(self):
        self.fetch(NOW)
        again = self.fetch(T2)
        self.assertEqual(again.change_state, "unchanged")
        self.assertFalse(again.reextracted)

    def test_content_never_labeled_is_not_stale(self):
        first = self.fetch(NOW)
        self.svc.execute("DELETE FROM pb_label WHERE raw_id=?", (first.raw_id,))
        self.assertFalse(executor.labels_stale(self.svc, self.sid, "nohash"))
        again = self.fetch(T2)
        self.assertFalse(again.reextracted)

    def test_staleness_is_per_source_and_content(self):
        with patch.object(prof, "LABELER_VERSION", "pb-labels-old"):
            first = self.fetch(NOW)
        th = self.svc.execute("SELECT text_hash FROM pb_raw_ref WHERE raw_id=?", (first.raw_id,)).fetchone()[0]
        self.assertTrue(executor.labels_stale(self.svc, self.sid, th))
        self.assertFalse(executor.labels_stale(self.svc, self.sid, "other-content"))
        self.assertFalse(executor.labels_stale(self.svc, None, th))


if __name__ == "__main__":
    unittest.main()
