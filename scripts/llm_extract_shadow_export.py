#!/usr/bin/env python3
"""Write a metadata-only shadow gap export. Never writes the service database."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata.collect.shadow.common import Stop, dumps, utcnow
from pbdata.collect.shadow.planner import write_gap_export


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--service-db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--tmp-dir", type=Path)
    args = p.parse_args()
    data = write_gap_export(args.service_db, args.output, utcnow(), tmp_dir=args.tmp_dir)
    print(dumps({"outcome": "ok", "rows": len(data["rows"]), "exported_at_utc": data["exported_at_utc"]}))


if __name__ == "__main__":
    try:
        main()
    except Stop as exc:
        print(dumps({"outcome": "stopped", "reason": str(exc)}))
        sys.exit(1)
    except Exception:
        print(dumps({"outcome": "stopped", "reason": "export_error"}))
        sys.exit(1)
