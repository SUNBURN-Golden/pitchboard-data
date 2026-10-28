-- Active-collector layers (additive). Layer 1 raw refs (no raw bytes kept: ref+hash+excerpt only, decision 2),
-- layer 2 extraction, layer 3 labels, layer 4 = pb_fact/pb_call_edition/projection from 0001.
CREATE TABLE pb_discovery_candidate(
  cand_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  host TEXT NOT NULL,
  label TEXT,
  target_id TEXT NOT NULL,
  strategy TEXT NOT NULL,                      -- wikidata_class|directory_list|query_combo|entity_followup|site_chain|sns_link|gap_search
  origin_ref TEXT,                             -- e.g. wikidata QID, list page title, parent candidate/page URL
  parent_cand_id TEXT REFERENCES pb_discovery_candidate(cand_id),
  depth INTEGER NOT NULL DEFAULT 0,
  prior_relevance REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'new' CHECK(state IN ('new','registered','registered_existing','deprioritized','rejected','manual_lead')),
  score_json TEXT,
  reason TEXT,
  source_id TEXT REFERENCES pb_source(source_id),
  discovered_at TEXT NOT NULL,
  evaluated_at TEXT,
  UNIQUE(host, target_id)
);

CREATE TABLE pb_raw_ref(
  raw_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES pb_source(source_id),
  url TEXT NOT NULL,
  final_url TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('html','pdf','image','feed','sitemap','json','other')),
  fetch_method TEXT NOT NULL CHECK(fetch_method IN ('api','feed','html','structured','browser','pdf','ocr')),
  content_type TEXT,
  bytes_len INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT,                           -- sha256 of body bytes (body itself is NOT stored)
  text_hash TEXT,                              -- sha256 of normalized extracted text
  http_status INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('ok','login_wall','challenge','empty_body','denied_robots','denied_http',
                                          'not_found','server_error','timeout','too_large','blocked_network','needs_render','error')),
  error_class TEXT,
  change_state TEXT NOT NULL CHECK(change_state IN ('new','unchanged','changed','failed')),
  prev_raw_id TEXT REFERENCES pb_raw_ref(raw_id),
  seg_hashes TEXT NOT NULL DEFAULT '[]',
  segment_count INTEGER NOT NULL DEFAULT 0,
  page_count INTEGER,
  extractor_version TEXT,
  parent_raw_id TEXT REFERENCES pb_raw_ref(raw_id),
  depth INTEGER NOT NULL DEFAULT 0,
  run_id TEXT,
  fetched_at TEXT NOT NULL
);
CREATE INDEX pb_raw_ref_url ON pb_raw_ref(url, fetched_at);

CREATE TABLE pb_extraction(
  extraction_id TEXT PRIMARY KEY,
  raw_id TEXT NOT NULL REFERENCES pb_raw_ref(raw_id),
  field TEXT NOT NULL,                         -- predicate, or signal.* (year, apply_url, attachment, sns, entity)
  value_state TEXT NOT NULL,
  value_json TEXT,
  excerpt TEXT NOT NULL CHECK(length(excerpt) <= 280),
  locator TEXT NOT NULL DEFAULT '{}',          -- JSON: segment index, kind, page, table row
  extractor TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  confidence REAL,
  created_at TEXT NOT NULL,
  UNIQUE(raw_id, field, excerpt, extractor_version)
);
CREATE INDEX pb_extraction_raw ON pb_extraction(raw_id);

CREATE TABLE pb_label(
  label_id TEXT PRIMARY KEY,
  raw_id TEXT NOT NULL REFERENCES pb_raw_ref(raw_id),
  label TEXT NOT NULL CHECK(label IN ('recruitment','rules','correction','extension','cancellation','results','other')),
  basis TEXT NOT NULL CHECK(basis IN ('fact','claim','inference')),
  status TEXT NOT NULL,                        -- current|stale_prior_year|results_not_call|unknown_period|n/a
  open_call_eligible INTEGER NOT NULL DEFAULT 0,
  rule_ids TEXT NOT NULL DEFAULT '[]',
  years_seen TEXT NOT NULL DEFAULT '[]',
  labeler_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(raw_id, label, labeler_version)
);

CREATE TABLE pb_gap(
  gap_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  target_id TEXT,
  predicate TEXT NOT NULL,
  scope_ref TEXT NOT NULL DEFAULT 'unspecified',
  gap_class TEXT NOT NULL CHECK(gap_class IN ('not_extracted','absent_in_source','inaccessible','conflict')),
  state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','filled','closed_absent','blocked')),
  importance REAL NOT NULL DEFAULT 0.5,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  filled_fact_id TEXT REFERENCES pb_fact(fact_id),
  filled_evidence_id TEXT REFERENCES pb_evidence(evidence_id),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  UNIQUE(subject_id, predicate, scope_ref)
);
CREATE INDEX pb_gap_state ON pb_gap(state, gap_class);

-- Which call/outlet belongs to which stated target list (metrics are scoped to this).
CREATE TABLE pb_target_hit(
  target_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN ('source','outlet','call')),
  subject_id TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  in_seed_list INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(target_id, subject_type, subject_id)
);
CREATE TABLE pb_source_site(
  source_id TEXT PRIMARY KEY REFERENCES pb_source(source_id),
  outlet_id TEXT REFERENCES pb_outlet(outlet_id),
  origin TEXT NOT NULL,
  registered_domain TEXT NOT NULL,
  in_seed_list INTEGER NOT NULL DEFAULT 0,
  wikidata_qid TEXT,
  target_id TEXT
);
