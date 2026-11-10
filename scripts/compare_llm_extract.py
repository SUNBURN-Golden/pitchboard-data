#!/usr/bin/env python3
"""Offline held-out scoring (default) or explicit bounded cached-page generation."""
import argparse
from collections import defaultdict
import os
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from pbdata.collect.shadow.common import CONFIG, Stop, digest, dumps, load_config, page_limit, secure_open
from pbdata.collect.shadow.compare import load_cohorts, predictions, score
from pbdata.collect.shadow.state import State, comparison_output

from pbdata.collect.shadow.batch import CONTINUE_REASONS, terminal


def write_report(path, report):
    with secure_open(path, "w") as f:
        f.write(dumps(report) + "\n")
        f.flush()
        os.fsync(f.fileno())


def shadow_report(args):
    if args.operation != "score" or not args.predictions:
        raise Stop("invalid_shadow_report_options")
    import datetime as dt
    from pbdata.collect.shadow.common import loads, utcnow, UTC
    from pbdata.collect.shadow.report import score_shadow, markdown, frozen_read
    from pbdata.collect.shadow.source_cache import atomic_json
    root = comparison_output(args.shadow_root).parent
    now = utcnow()
    if args.as_of:
        try:
            day = dt.date.fromisoformat(args.as_of)
            if day.isoformat() != args.as_of:
                raise ValueError()
            now = dt.datetime.combine(day, dt.time.max, UTC)
        except ValueError:
            raise Stop("invalid_as_of") from None
    checks = {}
    config = loads(frozen_read(args.config, checks))
    limit = page_limit(args.config)
    report = score_shadow(args.predictions, root, now, config, gold_path=args.gold_json,
                          ledger_path=args.ledger or root / "budget.sqlite3", max_page_utf8_bytes=limit)
    report["input_checksums"].update(checks)
    report["scoring_commit"] = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    path = root / ("comparison_" + now.strftime("%Y%m%d") + ".json")
    atomic_json(path, report)
    with secure_open(path.with_suffix(".md"), "w") as f:
        f.write(markdown(report))
        f.flush()
        os.fsync(f.fileno())
    print(dumps({"report": str(path), **report["counts"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-root", type=Path, default=Path("/workspace/pb-eval"))
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, nargs="+", help="one or more daily shadow JSONL files")
    parser.add_argument("--operation", choices=("score", "generate"), default="score")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--sample-size", type=int, default=112)
    parser.add_argument("--input-mode", choices=("heldout", "shadow"), default="heldout")
    parser.add_argument("--gold-json", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--as-of", help="offline report calendar date; extraction dates remain frozen per row")
    args = parser.parse_args()
    if args.input_mode == "shadow":
        shadow_report(args)
        return
    if args.operation == "generate":
        # Disabled generation exits before corpus reads, state, credentials or sockets.
        load_config(args.config)
        state = State(args.shadow_root, (args.eval_root,))
        report_path = state.root / "comparison.json"
    else:
        # Score-only runs validate the root but create no experiment identity or ledger.
        report_path = comparison_output(args.shadow_root, (args.eval_root,))
    items, integrity, inputs = load_cohorts(args.eval_root, page_limit(args.config))
    rows = predictions(args.predictions)
    if args.operation == "generate":
        from pbdata.collect.shadow.runner import ShadowRunner
        with state.locked() as conn:
            experiment = conn.execute("SELECT value FROM meta WHERE key='experiment_id'").fetchone()[0]
            # Deterministic round-robin strata, including rule negatives.
            strata = defaultdict(list)
            for item in items:
                if item.aggregate:
                    strata[(item.cohort, item.language)].append(item)
            for group in strata.values():
                group.sort(key=lambda x: digest(experiment + "\0" + x.url + "\0" + x.body_hash))
            ordered = [group[i] for i in range(max((len(g) for g in strata.values()), default=0))
                       for _, group in sorted(strata.items()) if i < len(group)]
            if not 1 <= args.sample_size <= 112:
                raise Stop("invalid_sample_size")
            selected = ordered[:args.sample_size]
            sample = dumps([{"cohort": i.cohort, "url": i.url, "body_sha256": i.body_hash, "as_of": i.as_of} for i in selected])
            with secure_open(state.root / "frozen_sample.json", "a+") as f:
                f.seek(0)
                old = f.read()
                if old and old != sample:
                    raise Stop("sample_changed")
                if not old:
                    f.write(sample)
                    f.flush()
                    import os
                    os.fsync(f.fileno())
        runner = ShadowRunner(args.config)
        for item in selected:
            result = runner.run(shadow_root=state.root, page=item.page, as_of=item.as_of,
                                extra_protected=(args.eval_root,))
            if result.get("outcome") == "ok":
                rows[item.key] = result
            if terminal(result):
                break
        # Also recover prior completed attempts on resume, without repeat API calls.
        with state.locked() as conn:
            for path in sorted((state.root / "data/shadow").glob("llm_extract_*.jsonl")):
                rows.update(predictions(path))
    report = score(items, rows)
    report["integrity"] = integrity
    report["baseline_commit"] = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    inputs.verify()
    if args.operation == "generate":
        with state.locked():
            write_report(report_path, report)
    else:
        write_report(report_path, report)
    print(dumps({"report": str(report_path), "eligible_pages": len(items),
                 "scored_predictions": sum(r["has_prediction"] for r in report["pages"])}))


if __name__ == "__main__":
    try:
        main()
    except Stop as exc:
        print(dumps({"outcome": "stopped", "reason": str(exc)}))
        sys.exit(1)
    except Exception:
        print(dumps({"outcome": "stopped", "reason": "comparison_error"}))
        sys.exit(1)
