"""Fail-closed shared batch outcome classification and bounded dispatch."""
from .common import Stop, digest, dumps, load_config, load_key
from .policy import PolicyFetcher
from .state import State

CONTINUE_REASONS = frozenset({
    "bad_url", "blocked_host", "blocked_network", "denied_robots", "host_halted",
    "fetch_error", "denied_http", "unsupported_content", "invalid_transport_body",
    "invalid_robots", "redirect_limit", "access_wall", "empty_page", "page_too_large",
    "duplicate_page", "invalid_policy_envelope"})
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


def execute(runner, root, plan, *, digest_languages=(), **run_options):
    config = load_config(runner.config_path, runner.clock())
    key = (run_options.get("key_loader") or load_key)()
    if key in dumps(plan):
        raise Stop("credential_in_record")
    run_options["key_loader"] = lambda: key
    start_day = runner.clock().date()
    state = State(root)
    with state.locked() as conn:
        state.preflight(conn, config, runner.clock())
        from .common import append_sync, secure_open
        path = state.output(start_day.isoformat()).with_name("plans_" + start_day.strftime("%Y%m%d") + ".jsonl")
        with secure_open(path, "a+") as f:
            append_sync(f, {"plan_id": digest(dumps(plan)), "plan": plan})
    fetcher = PolicyFetcher(egress=config["egress"])
    results = []
    for target in plan["targets"]:
        if runner.clock().date() != start_day:
            results.append({"outcome": "skipped", "reason": "dispatch_clock_changed"})
            break
        if load_config(runner.config_path, runner.clock()) != config:
            results.append({"outcome": "skipped", "reason": "config_changed"})
            break
        result = runner.run(shadow_root=root, url=target["url"], fetcher=fetcher,
                            target=target, digest_languages=digest_languages, batch_day=start_day.isoformat(), **run_options)
        results.append(result)
        if terminal(result):
            break
    return results
