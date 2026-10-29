"""open_calls_current must not count superseded (review-retired) call editions."""
import inspect
import unittest

from pbdata.collect import metrics


class OpenCallsMetricTest(unittest.TestCase):
    def test_open_calls_query_excludes_superseded(self):
        src = inspect.getsource(metrics.compute)
        self.assertIn("NOT IN ('closed','cancelled','superseded')", src)


if __name__ == "__main__":
    unittest.main()
