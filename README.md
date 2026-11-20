# pitchboard-data

Data foundation for Pitchboard (paid book-review/criticism outlets and open calls).
Scope: schema, fact model, collectors, validation rules, migrations, stage runner, de-identified fixtures, ops docs.
**No frontend. No raw source data. No secrets. No service DB in this repo.**

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Representative questions: [docs/QUESTIONS.md](docs/QUESTIONS.md)
- Ops: [docs/OPS.md](docs/OPS.md)

## Data roots (outside the repo)
`PB_DATA_ROOT` (default `/workspace/pitchboard/data-root`) holds `service_db/`, `infra_db/`, `rawstore/` (default empty: only
280-char excerpts + hashes are retained), `backups/`, `locks/`. Nothing in there is ever committed (see `.gitignore`).

## Tests
```sh
PYTHONPATH=src:tests python -m unittest discover -s tests -v
```
Python 3.9+. The runtime uses the standard library. The offline test fixtures use Pillow, Tesseract, and Poppler.

## Active collector (PR #2)
`src/pbdata/collect/` + `src/pbdata/profiles/pitchboard.py`; docs: `docs/COLLECTOR.md`, `docs/OPS.md`, `docs/PROOFS.md`.
Run `PYTHONPATH=src python -m pbdata.collect run --root "$PB_DATA_ROOT" --run-tag T`. Promotion uses rules only.

## Optional LLM shadow experiment

The standalone DeepSeek Flash extraction lane with Qwen fallback is disabled in
`config/llm_extract.json`. It writes only to a private external shadow root and
never updates the live DB or active crawler. See
[the BUILD design and commands](docs/LLM_EXTRACT_DESIGN.md) for the seven-day
comparison, evidence validation, and offline held-out scoring. The private config
requires `enabled: true` and an active UTC window of at most seven days.
[Activation and egress](docs/LLM_EXTRACT_ACTIVATE.md)

## Caps removed 2026-11-20

Omitted CLI limits and absent or JSON `null` count settings mean unlimited.
An explicit nonnegative count sets a limit, including zero.

- The Qwen bot dropped its 30-page and 40-call defaults. The handoff daemon
  dropped its 10-batch default. See `config/qwen_bot.json`, `src/pbdata/handoff/bot.py`,
  and `src/pbdata/handoff/daemon.py`. [Handoff controls](docs/qwen_handoff.md#caps-removed-2026-11-20)
- Shadow extraction dropped the 50-call daily cap, the enforced USD 5 cap,
  `budget_exhausted`, and pricing expiry as a stop. The batch dropped its 40-URL
  default, and held-out generation dropped its 112-page default. See
  `config/llm_extract.json`, `src/pbdata/collect/shadow/`,
  `scripts/llm_extract_shadow_batch.py`, and `scripts/compare_llm_extract.py`.
  [Shadow usage and pricing](docs/LLM_EXTRACT_ACTIVATE.md#caps-removed-2026-11-20)
- The collector and ontology daemon dropped default job, Wikimedia API, and
  lane request caps and refresh enqueue limits. The collector also lifted
  discovery, trial, per-site, sitemap, feed, pagination, link-following, and
  gap-fill counts. See `config/targets/pitchboard.json`, `src/pbdata/collect/`,
  and `src/pbdata/daemon/{cli,stages}.py`. [Collector overrides](docs/COLLECTOR.md#caps-removed-2026-11-20)
- The daemon ledger dropped its resource caps and `CostCapExceeded` in
  `src/pbdata/daemon/runner.py`. It records usage, and promotion enforces the
  zero-AI quality rule in `src/pbdata/daemon/stages.py`.
- `scripts/daily.sh` dropped the 120-call API override, the 600-job run and
  80-job refresh overrides, and the lane request overrides.
  `scripts/daemon_daily.sh` dropped its 600-job and lane request overrides.
  The collector keeps its 3-hour run and 1-hour refresh timeouts to prevent
  overlap with the next day's run.
