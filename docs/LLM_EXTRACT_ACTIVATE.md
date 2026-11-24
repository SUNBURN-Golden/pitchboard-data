# Activate the DeepSeek shadow lane

This lane fetches eligible public pages for DeepSeek extraction with Qwen fallback and writes a private JSONL file.
It does not update the live database, the daily collector, `scripts/daily.sh`, or cron.
The checked-in `config/llm_extract.json` stays `enabled: false`. Use a private config file.

No API key belongs in the config, the command line, or this document.
The process reads `DEEPSEEK_API_KEY` from the environment. If that variable is unset, it parses one assignment from `~/.config/pitchboard/env`.
Do not source that file. Do not set an empty `DEEPSEEK_API_KEY` in cron; an empty value is an error and does not fall back.

Leave `/workspace/pitchboard/devpipe-runs/deepseek-extract/llm_extract.json.bak-20261108` untouched.
It is not the config for this lane.

## Network behavior

`src/pbdata/collect/net.py` documents `198.18.0.0/15` as the sandbox fake-IP egress range.
The reported resolver answer `198.18.0.1` is inside that range and is not a global address, so the old shadow check stopped with `blocked_network` before fetch.

The shadow fetcher has two connect paths. The shipped config uses `direct`. The private config below sets `sandbox`, which is the explicit pin for this runtime.

* **Direct.** Every DNS answer is a global address. The socket connects to the first of those addresses. An answer inside `198.18.0.0/15` is `blocked_network`.
* **Sandbox.** Selected only by `"egress": "sandbox"`. Every DNS answer is global, or every answer is inside the fixed range `198.18.0.0/15`. The socket connects to the first vetted address. TLS still checks the original hostname.

There is no HTTP proxy and no operator-supplied CIDR. `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` are ignored.
`PITCHBOARD_EGRESS_GATEWAY_CIDRS` is not read, so it cannot add other ranges or turn the sandbox path on.
A literal URL whose host is `198.18.0.1`, a private address, loopback, link-local, or a metadata name is denied before DNS.
Redirects are canonicalized again. Mixed DNS answers, including a gateway address plus a global or private address, are denied.
The pinned address is not resolved a second time.

A result of `blocked_network` after this change means the host or the DNS answers were not an allowed route.
`denied_robots`, `access_wall`, `denied_http`, and `empty_page` mean the fetch policy ran.

## Caps removed 2026-11-20

- `config/llm_extract.json` and `src/pbdata/collect/shadow/{common,state,runner}.py`
  removed the 50-call daily cap, the enforced USD 5 default, and the
  `budget_exhausted` stop. Private configs may still contain
  `daily_call_cap`, `daily_cost_cap_usd`, and `monetary_cap`. The runner ignores them.
- `scripts/llm_extract_shadow_batch.py` and `src/pbdata/collect/shadow/planner.py`
  removed the 40-URL default and the 50-URL maximum for `--limit`. Omit the flag
  to select all eligible URLs or pass a nonnegative integer. The held-out
  generation command in `scripts/compare_llm_extract.py` uses the same
  convention for `--sample-size`.
- The shadow lane requires literal `enabled: true` and
  `window_start_utc <= now < window_end_utc`. The UTC window can span at most
  seven days. The shipped config stays disabled.
- The attempts ledger (`budget.sqlite3`) and daily JSONL record provider calls,
  per-call token counts, and estimated cost. Set optional `pricing` for DeepSeek
  or `qwen_pricing` for Qwen with decimal-string rates in
  `input_usd_per_million_ceiling` and `output_usd_per_million_ceiling`, a
  `verified_on` UTC date, and a `valid_until_utc` timestamp. Missing, invalid,
  or expired prices and missing usage produce a null estimate and processing
  continues. The optional `input_token_reservation_ceiling` retains a
  pre-dispatch ceiling estimate in the ledger for that provider.

## Private config

Save this as `~/.config/pitchboard/llm_extract.json` with mode `0600`.
Do not put it inside the shadow root. Do not edit the repo copy to enable the lane.

Set the example window timestamps to your intended UTC run window. If you configure pricing, use the date you verified the rates and their applicable expiry.

```json
{
  "enabled": true,
  "mode": "shadow",
  "model": "deepseek-flash",
  "egress": "sandbox",
  "max_page_utf8_bytes": 24000,
  "max_output_tokens": 2048,
  "timeout_seconds": 30,
  "window_start_utc": "2026-11-20T00:00:00Z",
  "window_end_utc": "2026-11-27T00:00:00Z"
}
```

## Verify once

Use the checkout that contains this change. Do not run it from a `main` checkout that does not have `EGRESS_GATEWAY` in `src/pbdata/collect/shadow/policy.py`.

```sh
set -euo pipefail
REPO=/absolute/path/to/this/checkout
ROOT=/workspace/pitchboard-llm-shadow
CFG=$HOME/.config/pitchboard/llm_extract.json
OPS=$HOME/.local/state/pitchboard-llm-shadow
install -d -m 0700 "$HOME/.config/pitchboard" "$OPS"
if [ -e "$ROOT/experiment.json" ]; then
  :
elif [ -e "$ROOT" ]; then
  test -d "$ROOT" && test -z "$(ls -A "$ROOT")"
else
  install -d -m 0700 "$ROOT"
fi
cd "$REPO"
git rev-parse --abbrev-ref HEAD
grep -n EGRESS_GATEWAY src/pbdata/collect/shadow/policy.py
run() { PYTHONPATH=src python3 -B scripts/llm_extract_shadow.py --config "$CFG" --shadow-root "$ROOT" --url "$1"; }
run 'https://www.ltikorea.or.kr/kr/board/notice/boardList.do'
run 'https://munjang.or.kr/'
run 'https://www.arko.or.kr/board/list/4053'
```

The directory test refuses a shadow root that already contains files.
A root created by an earlier attempt has `experiment.json`; reuse that same root and skip the emptiness check.
Do not delete it to get a clean directory. Deleting the ledger is not a reset.

Stdout is only `outcome`, and `reason` or `attempt_id`.
Stop if any of the three prints `"reason":"blocked_network"`.
A site denial is a completed policy check, not the gateway bug.

Then print only the safe fields from today's shadow file:

```sh
PYTHONPATH=src python3 -B - << PY
import json
from pathlib import Path
day = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y%m%d")
path = Path("/workspace/pitchboard-llm-shadow/data/shadow") / ("llm_extract_" + day + ".jsonl")
for line in path.read_text().splitlines():
    row = json.loads(line)
    print({k: row.get(k) for k in ("outcome", "final_url", "returned_model", "provider_calls", "estimated_cost_usd", "validation_reasons")})
PY
```

Confirm the line does not contain an authorization header or the API key.
The same URL on the same UTC day returns `duplicate_page` and does not call the provider again.
The next UTC day is a new call. This three-page, hardcoded-URL invocation is
**superseded** by the gap-targeted batch; see
[LLM_EXTRACT_IMPROVE_DESIGN.md](LLM_EXTRACT_IMPROVE_DESIGN.md#operator-handoff-after-implementation).

## Separate schedule

Keep the isolated shadow job's lock and log directory separate from the daily
collector. Use a fresh metadata-only gap export. Inspect it with `--plan-only`
before running the batch.

```sh
PYTHONPATH=src python3 -B scripts/llm_extract_shadow_export.py \
  --service-db /absolute/service_db/pitchboard_kg.sqlite3 \
  --output /absolute/private/gaps.json --tmp-dir /absolute/private/tmp

PYTHONPATH=src python3 -B scripts/llm_extract_shadow_batch.py \
  --config /absolute/private/llm_extract.json \
  --shadow-root /absolute/existing/shadow-root \
  --gaps-json /absolute/private/gaps.json --limit 40
```

`scripts/llm_extract_shadow_export.py` never opens the live service database in
SQLite. It byte-copies the database and any WAL/SHM/journal sidecars into a
private temporary snapshot, converts only that copy to rollback-journal format,
and writes a metadata-only JSON export. Collector and daemon locks are not
touched. A fingerprint change during the copy is retried, then stops. The JSON
reader accepts exports up to 64 MiB.

The versioned daily wrapper is `scripts/llm_extract_shadow_daily.sh`. It waits
for the loopback SOCKS5 proxy and skips the run if the proxy is down, refreshes
the export, then runs the batch. Override `PB_SHADOW_LIMIT` to change the
default 40-URL selection. It does not source `~/.config/pitchboard/env`; the
extractor reads `DEEPSEEK_API_KEY` itself.

The cron entry stays a lock around a thin ops wrapper:

```sh
15 6 * * * flock -n "$HOME/.local/state/pitchboard-llm-shadow/cron.lock" bash "$HOME/.local/state/pitchboard-llm-shadow/run_daily.sh" # pitchboard-llm-shadow
```

`run_daily.sh` only execs the versioned wrapper. Keep the log and lock under
`~/.local/state/pitchboard-llm-shadow`. Activation ends at `window_end_utc`.
The private window may span at most seven UTC days; renew it by starting at or
after the previous `window_end_utc`. The private config controls renewal.

## Fetch and output policy

* Instagram, the repo block list, robots, login walls, and challenges are still denied.
* Output stays under the shadow root. There is no live database write.
* If the resolver returns both a gateway address and a global address, the fetch stays `blocked_network` rather than guessing a path.
* TLS verification stays on. A gateway that presents a private certificate fails closed.
