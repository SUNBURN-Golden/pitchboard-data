"""eligibility.region stated values (pb-region-1): literal worldwide / restricted / excluded readings only."""
import unittest

from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as prof


def region(text, heading=""):
    seg = Segment(0, "p", text)
    seg.heading = heading
    return [(e.value_state, e.value) for e in prof.extract(Doc("html", segments=[seg])) if e.field == "eligibility.region"]


class RegionTests(unittest.TestCase):
    def test_worldwide_with_exclusions(self):
        r = region("WD writing competitions are open to U.S. and non-U.S. residents; however, due to U.S. Government restrictions "
                   "we are unable to accept entries from Syria, Iran, North Korea, or Crimea.")
        self.assertEqual(len(r), 1)
        st, v = r[0]
        self.assertEqual(st, "stated")
        self.assertTrue(v["worldwide"])
        self.assertEqual(sorted(v["excluded"]), ["IR", "KP", "SY", "UA-43"])
        self.assertTrue(v["korea_resident_eligible"])   # North Korea excluded does not exclude Korea

    def test_every_nationality_is_worldwide(self):
        self.assertEqual(region("We welcome and encourage submissions from writers of every nationality, race, religion, and gender.")[0][1]["scope"], "worldwide")

    def test_restricted_country(self):
        for t, code in (("The prize is open only to legal residents of the United States.", "US"),
                        ("Entrants must be a citizen of Canada.", "CA"), ("UK residents only.", "GB"),
                        ("Open to writers living in Australia or New Zealand.", "AU")):
            st, v = region(t)[0]
            self.assertEqual((st, v["scope"], v["regions"][0]), ("stated", "restricted", code), t)
            self.assertFalse(v["korea_resident_eligible"], t)

    def test_korean_restrictions(self):
        st, v = region("응모 자격: 대한민국 국적을 가진 자 (해외 거주자 포함 가능)")[0]
        self.assertEqual((v["scope"], v["regions"]), ("restricted", ["KR", "overseas_korean"]))
        self.assertTrue(v["korea_resident_eligible"])
        self.assertEqual(region("응모자격 | 국적 불문, 한국어로 쓴 미발표 작품")[0][1]["scope"], "worldwide")
        self.assertEqual(region("자격 | 문학, 예술 분야에서 창작 활동하고 있는 외국인")[0][1]["regions"], ["non-KR"])

    def test_incidental_mentions_are_not_eligibility(self):
        for t in ("박경리문학상은 대한민국 국민과 세계의 모든 민족을 하나로 연결되도록 이끄는 통합의 장이 될 것이다.",
                  "For the February 2019 issue, the journal will pay Australian-based poets $75 per poem published.",
                  "Living in the US is interesting and wonderful.", "Plumwood Mountain: An Australian and International Journal",
                  "We cannot accept submissions from anyone who sent us work this year.",
                  "응모자격 신인 및 기성작가 제한 없음", "The residents of the village were thrown out."):
            self.assertEqual(region(t), [], t)

    def test_old_worldwide_phrase_unchanged(self):
        self.assertEqual(region("We are open to writers from everywhere."), [("explicitly_none", {"scope": "worldwide"})])


if __name__ == "__main__":
    unittest.main()
