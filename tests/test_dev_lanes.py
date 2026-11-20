import json, os, re, unittest
CFG = os.path.join(os.path.dirname(__file__), "..", "config", "dev_lanes.json")
RUNNER = os.path.join(os.path.dirname(__file__), "..", "scripts", "devpipe", "run_stage.sh")
STAGES = ("design", "design_review", "build", "build_review", "review_fix", "re_review", "audit")
QWEN = "qwen:qwen4-max-preview"

class DevLanes(unittest.TestCase):
    def test_order(self):
        c = json.load(open(CFG))
        self.assertEqual(c["design"], ["codex:gpt-6.2-astra", QWEN, "deepseek:deepseek-flash", "glm", "swe2:swe-2-high"])
        self.assertEqual(c["design_review"], ["deepseek:deepseek-flash", QWEN, "codex:gpt-6.2-sol", "glm", "swe2:swe-2-high"])
        self.assertEqual(c["build"], ["codex:gpt-6.2-sol", QWEN, "deepseek:deepseek-flash", "glm", "swe2:swe-2-high"])
        self.assertEqual(c["build_review"], ["glm", QWEN, "deepseek:deepseek-flash", "codex:gpt-6.2-sol", "swe2:swe-2-high"])
        self.assertEqual(c["review_fix"], ["deepseek:deepseek-flash", QWEN, "codex:gpt-6.2-sol", "glm", "swe2:swe-2-high"])
        self.assertEqual(c["re_review"], ["devin:adaptive", QWEN, "deepseek:deepseek-flash", "codex:gpt-6.2-sol", "swe2:swe-2-high"])
        self.assertEqual(c["audit"], ["codex:gpt-6.2-astra", QWEN, "glm", "swe2:swe-2-high"])
        self.assertEqual(c["re_review"][0], "devin:adaptive")
        self.assertEqual(c["audit_every_prs"], 10)
        for k in STAGES:
            self.assertEqual(c[k][1], QWEN)
            self.assertEqual(c[k][-1], "swe2:swe-2-high")

    def test_runners_cover_lane_kinds(self):
        text = open(RUNNER).read()
        kinds = set()
        c = json.load(open(CFG))
        for k in STAGES:
            for lane in c[k]:
                kinds.add(lane.split(":", 1)[0])
        for kind in kinds:
            self.assertRegex(
                text,
                r"(?m)^\s+(?:[a-z0-9]+\|)*" + re.escape(kind) + r"(?:\|[a-z0-9]+)*\)",
            )
        self.assertIn("qwen env missing", text)
        self.assertIn("/workspace/qwen_env.sh", text)
        self.assertIn("token_plan_key", text)
        self.assertIn("PB_DEV_LANES", text)

if __name__ == "__main__":
    unittest.main()
