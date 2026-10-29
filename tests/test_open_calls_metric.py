"""open_calls_current must not count superseded (review-retired) call editions."""
import inspect

from pbdata.collect import metrics


def test_open_calls_query_excludes_superseded():
    src = inspect.getsource(metrics.compute)
    assert "NOT IN ('closed','cancelled','superseded')" in src
