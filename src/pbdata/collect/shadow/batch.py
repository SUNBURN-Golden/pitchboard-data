"""Fail-closed shared batch outcome classification and serial dispatch."""
import sys

from .. import egress as socks_egress
from .common import Stop, digest, dumps, load_config, load_key
from .policy import PolicyFetcher
from .state import State

CONTINUE_REASONS = frozenset({
    "bad_url", "blocked_host", "blocked_network", "denied_robots", "host_halted",
    "fetch_error", "denied_http", "unsupported_content", "invalid_transport_body",
    "invalid_robots", "redirect_limit", "access_wall", "empty_page", "page_too_large",
    "duplicate_page", "invalid_policy_envelope"})
# Pre-dispatch fetch blocks are not ledger attempts, so the planner never saw
# them and re-picked the same walled/denied pages every day. They are logged per
# URL and the planner cools them down (days) until the service source changes.
FETCH_BLOCK_COOLDOWN_DAYS = {
    "access_wall": 7, "denied_robots": 7, "denied_http": 7, "invalid_robots": 7, "page_too_large": 7,
    "unsupported_content": 7, "redirect_limit": 7, "empty_page": 7, "blocked_host": 7, "bad_url": 7,
    "fetch_error": 2}
CONTINUE_OUTCOMES = frozenset({"ok", "provider_http_error", "provider_too_large", "provider_error",
    "duplicate_json_key", "invalid_json_number", "invalid_usage", "invalid_completion",
    "invalid_model_json", "invalid_schema"})


def terminal(result):
    if not isinstance(result, dict):
        return True
    if result.get("reason") and result["reason"] not in CONTINUE_REASONS:
        return True
    return (result.get("reason") not in CONTINUE_REASONS if result.get("outcome") == "skipped"
            else result.get("outcome") not in CONTINUE_OUTCOMES)


def proxy_outage(fetcher, wait=None):
    """After a fetch_error, tell a proxy outage apart from a site failure.

    Returns None when no loopback proxy is in use or it still answers (the
    site failed), True when the proxy was down and came back within the wait,
    and False when it stayed down. Never connects anywhere but the loopback proxy.
    """
    if not getattr(fetcher, "proxy", None):
        return None
    try:
        socks_egress.preflight()
        return None
    except socks_egress.ProxyConfigError:
        return False
    except OSError:
        pass
    try:
        (wait or (lambda: socks_egress.wait_for_proxy(log=lambda m: print(m, file=sys.stderr))))()
        return True
    except OSError:
        return False


def execute(runner, root, plan, *, digest_languages=(), proxy_wait=None, **run_options):
    config = load_config(runner.config_path, runner.clock())
    key = (run_options.get("key_loader") or load_key)()
    if key in dumps(plan):
        raise Stop("credential_in_record")
    run_options["key_loader"] = lambda: key
    start_day = runner.clock().date()
    state = State(root)
    with state.locked() as conn:
        state.preflight(conn, runner.clock())
        from .common import append_sync, secure_open
        path = state.output(start_day.isoformat()).with_name("plans_" + start_day.strftime("%Y%m%d") + ".jsonl")
        with secure_open(path, "a+") as f:
            append_sync(f, {"plan_id": digest(dumps(plan)), "plan": plan})
    fetcher = PolicyFetcher(egress=config["egress"])
    skips_path = path.with_name("fetch_skips_" + start_day.strftime("%Y%m%d") + ".jsonl")

    def record_block(result):
        if not (isinstance(result, dict) and result.get("outcome") == "skipped"
                and result.get("reason") in FETCH_BLOCK_COOLDOWN_DAYS):
            return
        with state.locked():
            with secure_open(skips_path, "a+") as f:
                append_sync(f, {"url": target["url"], "content_hash": target.get("content_hash"),
                                "reason": result["reason"], "day": start_day.isoformat(),
                                "timestamp_utc": runner.clock().isoformat()})
    results = []
    for target in plan["targets"]:
        if runner.clock().date() != start_day:
            results.append({"outcome": "skipped", "reason": "dispatch_clock_changed"})
            break
        if load_config(runner.config_path, runner.clock()) != config:
            results.append({"outcome": "skipped", "reason": "config_changed"})
            break
        halted_before = set(fetcher.halted)
        def run_target():
            return runner.run(shadow_root=root, url=target["url"], fetcher=fetcher,
                              target=target, digest_languages=digest_languages, batch_day=start_day.isoformat(), **run_options)
        result = run_target()
        if isinstance(result, dict) and result.get("reason") == "fetch_error":
            outage = proxy_outage(fetcher, proxy_wait)
            if outage is False:
                # The proxy is down, not the site: no cooldown for this URL.
                results.append(result)
                results.append({"outcome": "skipped", "reason": "proxy_unavailable"})
                break
            if outage is True:
                # The outage wait can outlive a config change. A retry is a fresh
                # dispatch, so it must pass the same gate as the next target or
                # it would record an attempt under a config the batch rejects.
                if load_config(runner.config_path, runner.clock()) != config:
                    results.append(result)
                    results.append({"outcome": "skipped", "reason": "config_changed"})
                    break
                # Halts set while the proxy was down are not the site's fault.
                fetcher.halted.clear()
                fetcher.halted.update(halted_before)
                results.append(result)
                result = dict(run_target(), proxy_retry=True)
        results.append(result)
        record_block(result)
        if terminal(result):
            break
    return results
