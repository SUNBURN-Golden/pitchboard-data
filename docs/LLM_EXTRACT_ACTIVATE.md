# Activate the DeepSeek shadow lane

This lane fetches one public page, optionally calls DeepSeek, and writes a private JSONL file.
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

## Caps and pricing

The shipped default is `monetary_cap: "enforced"` and `daily_cost_cap_usd: "5.00"`.
That dollar stop is about 16 calls a day because each call records a permanent ceiling of USD 0.3024576 (1,000,000 input tokens at USD 0.30/M plus 2,048 output tokens at USD 1.20/M).
The ceiling is an accounting reservation, not an invoice.

The only way to turn the dollar stop off is both of these together:

* `monetary_cap` is the string `none`
* `daily_cost_cap_usd` is JSON `null`

`unlimited`, a missing `monetary_cap`, or `none` next to a dollar amount is invalid.
The call cap stays mandatory. The private example below keeps 50 calls a day.
There is no unlimited call cap, no retry, and no window longer than seven days.
Pricing must still be inside its verification window. `none` does not skip an expired price.

Re-read of https://api-docs.deepseek.com/quick_start/pricing/ on 2026-11-07: `deepseek-flash` peak cache-miss input is USD 0.30 per million tokens and peak output is USD 1.20 per million.
Off-peak and cache-hit prices on that page are lower. This lane keeps the peak ceilings and does not guess a lower rate.
The page says prices may change and does not give an expiry date. If either published peak rate is higher than these ceilings, do not enable until the private ceilings are raised to the published numbers.
The code rejects ceilings below USD 0.30 and USD 1.20.

## Private config

Save this as `~/.config/pitchboard/llm_extract.json` with mode `0600`.
Do not put it inside the shadow root. Do not edit the repo copy to enable the lane.

The dates below are valid from 2026-11-07 until 2026-11-14T00:00:00Z.
On a later day, set `verified_on` to that UTC date and set both end timestamps exactly seven days later, after checking the pricing page again.
`verified_on` cannot be in the future.

```json
{
  "enabled": true,
  "mode": "shadow",
  "model": "deepseek-flash",
  "daily_call_cap": 50,
  "daily_cost_cap_usd": null,
  "monetary_cap": "none",
  "egress": "sandbox",
  "max_page_utf8_bytes": 24000,
  "max_output_tokens": 2048,
  "timeout_seconds": 30,
  "window_start_utc": "2026-11-07T00:00:00Z",
  "window_end_utc": "2026-11-14T00:00:00Z",
  "pricing": {
    "input_usd_per_million_ceiling": "0.30",
    "output_usd_per_million_ceiling": "1.20",
    "input_token_reservation_ceiling": 1000000,
    "verified_on": "2026-11-07",
    "valid_until_utc": "2026-11-14T00:00:00Z"
  }
}
```

## Verify once

Use the checkout that contains this change. Do not run it from a `main` checkout that does not have `EGRESS_GATEWAY` in `src/pbdata/collect/shadow/policy.py`.
Do not pull `main` as part of this command. `scripts/daily.sh` is the collector and must stay as it is.

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
    print({k: row.get(k) for k in ("outcome", "final_url", "returned_model", "charged_ceiling_usd", "validation_reasons")})
PY
```

Confirm the line does not contain an authorization header or the API key.
The same URL on the same UTC day returns `duplicate_page` and does not call the provider again.
The next UTC day is a new call. This three-page, hardcoded-URL invocation is
**superseded** by the gap-targeted batch; see
[LLM_EXTRACT_IMPROVE_DESIGN.md](LLM_EXTRACT_IMPROVE_DESIGN.md#operator-handoff-after-implementation).

## Separate schedule

> **Superseded for the 40-URL activation.** The cron example below is retained
> only as a historical record of the original five/hardcoded-URL job. Do not use
> it for the gap-targeted rollout. Replace only the isolated shadow job's
> command with `scripts/llm_extract_shadow_batch.py ... --limit 40` as documented
> in
> [LLM_EXTRACT_IMPROVE_DESIGN.md](LLM_EXTRACT_IMPROVE_DESIGN.md#operator-handoff-after-implementation).
> This document does not edit cron.

Install this only after the one-time check looks right.
Do not add it to `scripts/daily.sh` and do not change the existing collector crontab lines.
The command stops calling the provider after `window_end_utc` / `valid_until_utc`. Renew the private config by hand. Nothing turns the shipped flag on.

```cron
15 6 * * * flock -n "$HOME/.local/state/pitchboard-llm-shadow/cron.lock" bash -lc 'cd /absolute/path/to/this/checkout && PYTHONPATH=src python3 -B scripts/llm_extract_shadow.py --config "$HOME/.config/pitchboard/llm_extract.json" --shadow-root /workspace/pitchboard-llm-shadow --url "https://www.ltikorea.or.kr/kr/board/notice/boardList.do" >> "$HOME/.local/state/pitchboard-llm-shadow/cron.log" 2>&1; PYTHONPATH=src python3 -B scripts/llm_extract_shadow.py --config "$HOME/.config/pitchboard/llm_extract.json" --shadow-root /workspace/pitchboard-llm-shadow --url "https://munjang.or.kr/" >> "$HOME/.local/state/pitchboard-llm-shadow/cron.log" 2>&1; PYTHONPATH=src python3 -B scripts/llm_extract_shadow.py --config "$HOME/.config/pitchboard/llm_extract.json" --shadow-root /workspace/pitchboard-llm-shadow --url "https://www.arko.or.kr/board/list/4053" >> "$HOME/.local/state/pitchboard-llm-shadow/cron.log" 2>&1'
```

Keep the log and lock under `~/.local/state/pitchboard-llm-shadow`, not inside the shadow root.
A non-empty shadow root without `experiment.json` is rejected.

## What remains true

* Instagram, the repo block list, robots, login walls, and challenges are still denied.
* Output stays under the shadow root. There is no live database write.
* One machine's ledger is not an account-wide spend cap. `monetary_cap: none` means this process will not stop at USD 5. The provider account can still be charged for every accepted call, up to the call cap, until the window ends.
* If the resolver returns both a gateway address and a global address, the fetch stays `blocked_network` rather than guessing a path.
* TLS verification stays on. A gateway that presents a private certificate fails closed.
