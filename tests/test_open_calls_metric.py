"""Metrics list editions, retaining their source evidence without URL-based merging."""
import datetime as dt
import unittest
from unittest.mock import patch

from helpers import NOW, add_call, add_outlet, add_revision, add_source, service
from pbdata import db, facts
from pbdata.collect import metrics
from pbdata.model import FactInput


class FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 5)


class OpenCallsMetricTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.infra = db.connect(':memory:')
        db.migrate(self.infra, 'infra')
        self.outlet = add_outlet(self.svc)
        self.addCleanup(self.svc.close)
        self.addCleanup(self.infra.close)

    def deadline(self, call, url, date='2026-12-13', epistemic='source_claim'):
        src = add_source(self.svc, url, outlet=self.outlet)
        rev = add_revision(self.svc, src)
        excerpt = 'Submissions close ' + date + '.'
        ev = facts.add_evidence(self.svc, rev, src, excerpt, 'Call', 0, 0, NOW)
        kw = dict(verifier='rules', verified_at=NOW, fresh_until='2027-01-01T00:00:00Z') if epistemic == 'verified_fact' else {}
        return facts.append(self.svc, FactInput('call', call, 'deadline.closes_on', src, 'stated', epistemic,
                            NOW, 'rules', '1', value={'date':date, 'tz_source':'unknown'}, evidence_id=ev,
                            source_authority='official_primary', **kw), NOW)

    def compute(self):
        with patch('datetime.date', FrozenDate):
            return metrics.compute(self.svc, self.infra, {'targets':[{'id':'test'}]})

    def test_url_variants_are_one_edition_with_all_evidence(self):
        call = add_call(self.svc, self.outlet, key='2026-12-13', status='open')
        # Insert referral first: output must be deterministic regardless of ingestion order.
        urls = ['https://example.org/contest?about=1&utm_source=listing', 'https://example.org/contest']
        for url in urls:
            self.deadline(call, url, epistemic='source_claim' if url == urls[0] else 'verified_fact')
        result = self.compute()
        self.assertEqual(result['open_calls_current'], 1)
        self.assertEqual(len(result['open_calls_list']), 1)
        item = result['open_calls_list'][0]
        self.assertEqual(item['call_id'], call)
        self.assertEqual(item['source'], urls[1])
        self.assertEqual({e['source'] for e in item['deadline_evidence']}, set(urls))
        self.assertEqual(self.svc.execute('SELECT count(*) FROM pb_fact').fetchone()[0], 2)

    def test_different_calls_with_identical_summary_fields_stay_separate(self):
        # Same outlet, deadline, and source still do not establish call identity.
        calls = [add_call(self.svc, self.outlet, key='2026-12-13', scope=scope, status='open')
                 for scope in ['book_review', 'unspecified']]
        src = 'https://example.org/board.es?mid=a&list_no=123'
        self.deadline(calls[0], src)
        # Share the same evidence-backed source for a second distinct edition.
        f = self.svc.execute('SELECT * FROM pb_fact LIMIT 1').fetchone()
        facts.append(self.svc, FactInput('call', calls[1], 'deadline.closes_on', f['source_id'], 'stated',
                     'source_claim', NOW, 'rules', '1', value={'date':'2026-12-13','tz_source':'unknown'},
                     evidence_id=f['evidence_id']), NOW)
        result = self.compute()
        self.assertEqual(result['open_calls_current'], 2)
        self.assertEqual({r['call_id'] for r in result['open_calls_list']}, set(calls))
        self.assertTrue(all(r['source'] == src for r in result['open_calls_list']))

    def test_conflicting_deadlines_remain_evidence_of_one_call(self):
        call = add_call(self.svc, self.outlet, status='open')
        self.deadline(call, 'https://example.org/a', '2026-12-15', 'verified_fact')
        self.deadline(call, 'https://example.org/b', '2026-12-13')
        self.deadline(call, 'https://example.org/c', '2026-12-13', 'verified_fact')
        result = self.compute()
        self.assertEqual(result['open_calls_current'], 1)
        item = result['open_calls_list'][0]
        self.assertEqual(item['deadline'], '2026-12-13')
        # The fact store demotes conflicting verified values to source_claim.
        self.assertEqual(item['source'], 'https://example.org/b')
        self.assertEqual({e['deadline'] for e in item['deadline_evidence']}, {'2026-12-13','2026-12-15'})
        self.assertEqual(len(item['deadline_evidence']), 3)

    def test_closed_cancelled_superseded_and_old_deadlines_excluded(self):
        for status in ['closed', 'cancelled', 'superseded']:
            call = add_call(self.svc, self.outlet, key=status, status=status)
            self.deadline(call, 'https://example.org/' + status)
        call = add_call(self.svc, self.outlet, key='past', status='open')
        self.deadline(call, 'https://example.org/past', '2026-11-04')
        result = self.compute()
        self.assertEqual(result['open_calls_current'], 0)
        self.assertEqual(result['open_calls_list'], [])

    def test_limit_applies_to_distinct_calls(self):
        for i in range(51):
            call = add_call(self.svc, self.outlet, key=str(i), status='open')
            for j in range(2):
                self.deadline(call, 'https://example.org/call/%s?variant=%s' % (i, j))
        result = self.compute()
        self.assertEqual(result['open_calls_current'], 51)
        self.assertEqual(len(result['open_calls_list']), 50)
        self.assertEqual(len({r['call_id'] for r in result['open_calls_list']}), 50)
        self.assertTrue(all(len(r['deadline_evidence']) == 2 for r in result['open_calls_list']))


if __name__ == '__main__':
    unittest.main()
