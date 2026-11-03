import json, os, unittest
CFG = os.path.join(os.path.dirname(__file__), "..", "config", "dev_lanes.json")
class DevLanes(unittest.TestCase):
    def test_order(self):
        c = json.load(open(CFG))
        self.assertEqual(c["design"], ["codex:gpt-6-astra", "deepseek:deepseek-flash", "glm", "swe2:swe-2-high"])
        self.assertEqual(c["re_review"][0], "devin:adaptive")
        self.assertEqual(c["audit"], ["codex:gpt-6-astra", "glm", "swe2:swe-2-high"])
        self.assertEqual(c["audit_every_prs"], 10)
        for k in ("design","design_review","build","build_review","review_fix","re_review","audit"):
            self.assertEqual(c[k][-1], "swe2:swe-2-high")
if __name__ == "__main__": unittest.main()
