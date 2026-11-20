"""Apply pending Qwen batches and publish the Pitchboard site."""
import argparse
from contextlib import closing, nullcontext
import json
import logging
from pathlib import Path

from pbdata import db, timeutil

from . import batch, cards, deploy, replay, site, status

LOG = logging.getLogger(__name__)


def database_phase(root, record, max_batches, clock):
    lock_dir = root / "locks"
    with status.locks(lock_dir, ("qwen_bot",), 30 * 60) as bot_free:
        if not bot_free:
            record["db_skip"] = "qwen_bot_busy"
            return
        with status.locks(lock_dir, ("daily", "collector", "daemon"), 2 * 60 * 60) as available:
            if not available:
                record["db_skip"] = "pipeline_busy"
                return
            folder = root / "handoff/qwen"
            ledger_path = folder / "ledger.sqlite3"
            with replay.read_ledger(ledger_path) as ledger:
                queued = replay.pending(folder, ledger, max_batches, recover=True)
            replay.process_batches(root, folder, queued, record, clock)
            with replay.read_ledger(ledger_path) as ledger, closing(batch.readonly(batch.service_path(root))) as svc:
                overlay = site.collect_overlay(svc, ledger)
                status.atomic_json(root / "handoff/site_overlay.json", overlay)
                replay.export_projection(svc, root, timeutil.iso(clock()))


def run(root, site_repo, *, max_batches=None, no_deploy=False, clock=timeutil.utcnow,
        build_site=site.build_site, alerts=deploy.dispatch_alerts, alerts_dry_run=False, cards_mode="auto",
        generate_card=cards.generate_card):
    started = clock()
    record = dict(started_at=timeutil.iso(started), batches_consumed=0, batches_failed=0, items_ok=0, facts_promoted=0,
                  calls_created=0, backup_path=None, site=dict(commit=None, overlay_changes=0, pages=0,
                  deploy_success=False, deployment_id=None, url=None), alerts_status=None)
    try:
        database_phase(root, record, max_batches, clock)
    except Exception as exc:
        print(f"handoff db phase failed ({type(exc).__name__})", flush=True)
        record.setdefault("db_error", "db_phase_error")

    as_of = clock()
    today = as_of.astimezone(timeutil.KST).date()
    kinds = cards.scheduled_kinds(cards_mode, today)
    record["cards"] = [dict(kind=kind, ok=False, out_dir=None, error="built site data unavailable") for kind in kinds]

    def generate_from_site_data(site_data):
        eastern_date = as_of.astimezone(cards.EASTERN).date()
        for item in record["cards"]:
            try:
                out_dir = generate_card(item["kind"], today, site_data, deadline_start=eastern_date,
                                        site_commit=record["site"]["commit"])
                item.update(ok=True, out_dir=str(out_dir), error=None)
            except Exception as exc:
                item["error"] = str(exc) or type(exc).__name__
                LOG.error("weekly card %s failed: %s", item["kind"], item["error"])

    try:
        options = {"after_build": generate_from_site_data} if kinds else {}
        build_site(root, site_repo, today, record["site"], no_deploy=no_deploy, **options)
    except (site.BuildError, deploy.DeployError) as exc:
        record["site"]["error_code"] = str(exc)
    except Exception as exc:
        print(f"handoff site phase failed ({type(exc).__name__})", flush=True)
        record["site"]["error_code"] = "site_phase_error"
    try:
        record["alerts_status"] = alerts(dry_run=True) if alerts_dry_run else alerts()
    except Exception:
        pass
    record["finished_at"] = timeutil.iso(clock())
    if "error_code" in record["site"]:
        record["outcome"] = record["site"]["error_code"]
    elif record.get("db_skip"):
        record["outcome"] = record["db_skip"]
    elif record.get("db_error"):
        record["outcome"] = record["db_error"]
    elif record["batches_failed"]:
        record["outcome"] = "batch_failures"
    else:
        record["outcome"] = "ok"
    status.update(root, "last_daemon_run", record, {
        "batches_consumed_total": record["batches_consumed"],
        "batches_failed_total": record["batches_failed"],
    })
    print("handoff daemon " + record["outcome"], flush=True)
    return record


def dry_run(root, site_repo, max_batches=None):
    folder = root / "handoff/qwen"
    with replay.read_ledger(folder / "ledger.sqlite3") as ledger:
        planned = replay.pending(folder, ledger, max_batches)
    try:
        commit = site.site_commit(site_repo, fetch=False)
    except site.BuildError:
        commit = None
    print(json.dumps({"batch_ids": [path.name for path, _ in planned], "site_commit": commit,
                      "db_writes": False, "deploy": False, "alerts": False}, sort_keys=True))


def main(argv=None, *, lock_held=False):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=db.data_root())
    parser.add_argument("--site-repo", type=Path, default=site.DEFAULT_REPO)
    parser.add_argument("--max-batches", type=int, help="maximum batches to apply (default: unlimited)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-deploy", action="store_true")
    parser.add_argument("--alerts-dry-run", action="store_true")
    parser.add_argument("--cards", choices=("auto", *cards.KINDS, "both", "none"), default="auto")
    args = parser.parse_args(argv)
    if args.max_batches is not None and args.max_batches < 0:
        parser.error("--max-batches must be nonnegative")
    try:
        if args.dry_run:
            dry_run(args.root, args.site_repo, args.max_batches)
            return 0
        guard = nullcontext(True) if lock_held else status.locks(args.root / "locks", ("handoff_daemon",))
        with guard as available:
            if not available:
                print("handoff daemon already running")
                return 0
            record = run(args.root, args.site_repo, max_batches=args.max_batches, no_deploy=args.no_deploy,
                         alerts_dry_run=args.alerts_dry_run, cards_mode=args.cards)
            return int(bool(record.get("db_error") or record["site"].get("error_code")))
    except Exception:
        print("handoff daemon error")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
