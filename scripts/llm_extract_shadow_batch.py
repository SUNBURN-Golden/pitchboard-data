#!/usr/bin/env python3
"""Plan metadata-only gaps or explicitly execute an isolated shadow batch."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata.collect.shadow.common import CONFIG, Stop, digest, dumps, load_config, utcnow
from pbdata.collect.shadow.planner import read_json, read_db, plan
from pbdata.collect.shadow.batch import execute


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--gaps-json", type=Path)
    source.add_argument("--service-db", type=Path)
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--allow-stale", action="store_true", help="plan-only inspection")
    p.add_argument("--limit", type=int, help="maximum number of distinct URLs to select (default: unlimited)")
    p.add_argument("--config", type=Path, default=CONFIG)
    p.add_argument("--shadow-root", type=Path)
    p.add_argument("--digest-languages", default="", choices=("", "ko", "en", "ko,en"))
    args = p.parse_args()
    if not args.plan_only and (args.allow_stale or args.shadow_root is None):
        raise Stop("invalid_batch_options")
    config = None if args.plan_only else load_config(args.config)
    now = utcnow()
    data = (read_json(args.gaps_json, now, allow_stale=args.allow_stale and args.plan_only)
            if args.gaps_json else read_db(args.service_db, now))
    history = []
    if not args.plan_only:
        from pbdata.collect.shadow.state import State
        state = State(args.shadow_root)
        with state.locked() as conn:
            state.preflight(conn, now)
            from pbdata.collect.shadow.common import loads
            history = [loads(r[0]) for r in conn.execute("SELECT result FROM attempts WHERE result IS NOT NULL")]
    elif args.shadow_root is not None:
        # Plan-only counts should reflect prior static completions (F3). This is
        # a read-only ledger snapshot: no lock, root initialization or writes.
        from pbdata.collect.shadow.state import read_history
        history = read_history(args.shadow_root)
    selected = plan(data, limit=args.limit, history=history, config_hash=digest(dumps(config)) if config else None)
    result = {"selected": len(selected["targets"]), "reasons": selected["reasons"]}
    if not args.plan_only:
        from pbdata.collect.shadow.runner import ShadowRunner
        rows = execute(ShadowRunner(args.config), args.shadow_root, selected,
                       digest_languages=tuple(filter(None, args.digest_languages.split(","))))
        result.update(attempted=sum("attempt_id" in r for r in rows),
                      successful=sum(r.get("outcome") == "ok" for r in rows),
                      processed=len(rows), proxy_retries=sum(bool(r.get("proxy_retry")) for r in rows),
                      outcomes=dict(sorted(__import__("collections").Counter(
                          r.get("reason") or r.get("outcome") or "unknown" for r in rows).items())),
                      last={k: rows[-1][k] for k in ("outcome", "reason") if k in rows[-1]} if rows else None)
    print(dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Stop as exc:
        print(dumps({"outcome": "stopped", "reason": str(exc)}))
        sys.exit(1)
    except Exception:
        print(dumps({"outcome": "stopped", "reason": "batch_error"}))
        sys.exit(1)
