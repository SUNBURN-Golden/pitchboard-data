#!/usr/bin/env python3
"""Explicit opt-in standalone live shadow fetch; never starts the collector."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata.collect.shadow.common import CONFIG, dumps
from pbdata.collect.shadow.runner import ShadowRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    result = ShadowRunner(args.config).run(shadow_root=args.shadow_root, url=args.url)
    # Only fixed outcome metadata; the complete result stays in the shadow output.
    print(dumps({k: result[k] for k in ("outcome", "reason", "attempt_id") if k in result}))


if __name__ == "__main__":
    main()
