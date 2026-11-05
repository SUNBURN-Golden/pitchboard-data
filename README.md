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
Python 3.9+, standard library only.

## Active collector (PR #2)
`src/pbdata/collect/` + `src/pbdata/profiles/pitchboard.py`; docs: `docs/COLLECTOR.md`, `docs/OPS.md`, `docs/PROOFS.md`.
Run: `PYTHONPATH=src python -m pbdata.collect run --root $PB_DATA_ROOT --run-tag T`. AI extraction is off (cost cap 0). Data roots live outside this repo.

## Optional LLM shadow experiment

The standalone DeepSeek Flash extraction lane is disabled in
`config/llm_extract.json`. It writes only to a private external shadow root and
never updates the live DB or active crawler. See
[the BUILD design and commands](docs/LLM_EXTRACT_DESIGN.md) for the seven-day
comparison, daily caps, evidence validation, and offline held-out scoring.
