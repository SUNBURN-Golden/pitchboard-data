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
PYTHONPATH=src python -m unittest discover -s tests -v
```
Python 3.9+, standard library only.
