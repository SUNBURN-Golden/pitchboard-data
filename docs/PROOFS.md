# Proof runs (2026-10-27 KST)

> The run measurements below reflect the collector settings at the time of each proof. See the [2026-11-20 cap changes](../README.md#caps-removed-2026-11-20) for current defaults.

Scope: target lists `kr-ko-lit`, `us-en-lit`, `gb-en-lit`, `ca-au-en-lit` (`config/targets/pitchboard.json`). Rules only, AI cost 0.
Real run root: `/workspace/pitchboard/data-root` (outside the repo). Raw outputs: `docs/proof_real_run_metrics.json` (counts only), `docs/proof_fixture_output.json`.

## A. Real web run (Wikidata/Wikipedia discovery -> trial crawl -> fetch)
Bounded: 120 API calls, 421 discovery + ~140 backfill/freshness HTTP requests, 2 s/host, robots honoured.
| metric | value |
|---|---|
| candidates discovered (4 targets) | 258 (22 SNS `manual_lead`, never fetched); 119 left unevaluated when the discovery budget was spent |
| evaluated by trial crawl | 117 -> registered 10, registered_existing 12 (already in seed list / domain), deprioritized 79, rejected 16 |
| **new valid sources outside the 76-seed list** | **5** registered with non-stale recruitment/rules page + >=1 required field (10 new sources/outlets registered in total; evaluation precision 9.5%) |
| new targets (call editions) open now | **0** - every real call page found was `stale_prior_year`, `past_deadline` or `unknown_period`; none was promoted to an open call (correct, not a gap in the pipeline) |
| required-field cells (10 new outlets x 5 outlet-level predicates = 50 cells) | stated 4, explicitly none 1, not stated in inspected scope 23, empty 22 -> known coverage 10%, classified coverage 56% |
| gaps | 70 total: 5 filled with evidence (7.1%), 18 absent_in_source, 47 not_extracted open; 0 filled by same-site reverse search on the real run |
| failures kept as failures | 5 `denied_robots`, 1 `empty_body`, 1 `needs_render`; none retried or bypassed |
| cost | 0 model USD; 559 network requests; 4 verified / 7 stated facts |
| coverage of multi-page/PDF/image on real sites | multi-page list, PDF and image-notice collection were NOT observed on real outlets in this bounded run (real: 59 ok HTML pages); proven on the fixture (B) |

Rerun (`refresh`, 90 jobs, new run tag): sources 10->10, outlets 10->10, call editions 0->0, conflicts 0->0, 26 pages logged `unchanged`, 0 `changed`.
Not strictly zero-delta: facts 47->48 and raw refs 66->110 because gap-fill/backfill visited pages for the first time (a new absence record on a newly inspected page).
Pure idempotency of the same pages is shown in B and in `test_idempotent_rerun`.

Resume: run `run_r1` was killed on purpose while holding a lease (`PB_CRASH_AFTER_LEASE=45`); restarting with the same run id re-queued the expired lease and finished it
(jobs done 45 -> 290, checkpoints in infra DB). I also had to restart once more after fixing a slow plain-http timeout; both restarts resumed from the queue.

## B. Fixture site (loopback test outlet, de-identified; NOT real-world evidence)
`PYTHONPATH=src:tests python scripts/proof_fixture.py OUT` -> `docs/proof_fixture_output.json`:
* html + pdf + image(OCR) collected; list page 2 followed by `rel=next`; image deadline 2026-12-15 extracted at confidence 0.8 as `source_claim` (never verified).
* gap reverse search: 5 gaps filled with evidence.
* rerun: counts identical (facts 23, calls 3), 8 raws `unchanged`.
* change: only `/calls/2026-fall` changed; deadline 2026-11-30 superseded (history kept) by 2026-12-20; status `open -> extended`.
* extraction breakage (page structure collapse): alert `field_lost`; no facts deleted.

## C. Label / extraction accuracy (gold sample `tests/gold/`, 64 hand-labelled items, 178 checks)
Accuracy 99.4% (177/178); label accuracy 63/64; one miss (a04: `other` predicted `recruitment`). Caveat: written by the same author and the rules were fixed after the first pass (first pass 97.2%).
Entity false-merge: 1 of 12 negative pairs merged ("Paris Review" vs "The Paris Review", treated as distinct in the gold) = 8.3%; 0 of 18 positives missed. 30 pairs is too few to claim a rate.

## D. Not proven / limits
SERP-style discovery; JS-heavy sites (needs_render); plain-http hosts; real PDFs/images on live outlets; change latency on the real web (n=0: nothing changed in the observation window).
