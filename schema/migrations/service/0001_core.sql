-- Pitchboard service DB, migration 0001. ADDITIVE ONLY (no DROP/DELETE/RENAME). Times are UTC ISO-8601 TEXT.
-- Entities: outlet/organizer -> call edition; sources -> revisions -> evidence -> facts.

CREATE TABLE pb_outlet(
  outlet_id TEXT PRIMARY KEY CHECK(outlet_id GLOB 'out_*'),
  canonical_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  retired_to TEXT REFERENCES pb_outlet(outlet_id)
);

CREATE TABLE pb_organizer(
  organizer_id TEXT PRIMARY KEY CHECK(organizer_id GLOB 'org_*'),
  canonical_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  retired_to TEXT REFERENCES pb_organizer(organizer_id)
);

-- Aliases for any subject (outlet/organizer/call/source). Legacy IDs live here (alias_kind='legacy_id').
CREATE TABLE pb_alias(
  subject_type TEXT NOT NULL CHECK(subject_type IN ('outlet','organizer','call','source')),
  subject_id TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  alias_raw TEXT NOT NULL,
  alias_kind TEXT NOT NULL CHECK(alias_kind IN ('name','domain','legacy_id','title','handle')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(subject_type, alias_norm, subject_id)
);
CREATE INDEX pb_alias_lookup ON pb_alias(subject_type, alias_norm);

-- Controlled vocabulary for scope (genre / submission type). 'unspecified' can never satisfy a scoped query (V-04).
CREATE TABLE pb_scope(
  scope_ref TEXT PRIMARY KEY,
  label_en TEXT NOT NULL,
  label_ko TEXT NOT NULL
);
INSERT INTO pb_scope VALUES
 ('book_review','Book review','서평'),
 ('critical_essay','Critical essay / criticism','비평 에세이'),
 ('pitch','Pitch','피치'),
 ('interview','Interview','인터뷰'),
 ('fiction','Fiction','소설'),
 ('poetry','Poetry','시'),
 ('nonfiction','Nonfiction','논픽션'),
 ('general','All submission types (stated as such)','전체 투고'),
 ('unspecified','Not specified by source','범위 미지정');

-- Call edition (회차): one window / contest round / standing channel for one scope.
CREATE TABLE pb_call_edition(
  call_id TEXT PRIMARY KEY CHECK(call_id GLOB 'call_*'),
  outlet_id TEXT REFERENCES pb_outlet(outlet_id),
  organizer_id TEXT REFERENCES pb_organizer(organizer_id),
  edition_key TEXT NOT NULL,                 -- e.g. '2026-10' or 'standing'
  scope_ref TEXT NOT NULL REFERENCES pb_scope(scope_ref),
  title_raw TEXT,
  status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN
    ('candidate','announced','open','extended','standing','closed','cancelled','status_unknown','superseded','archived')),
  status_since TEXT,
  status_basis TEXT CHECK(status_basis IN ('evidence','system_clock','review','backfill') OR status_basis IS NULL),
  dirty_seq INTEGER NOT NULL DEFAULT 1,       -- bumped whenever projection must be recomputed
  created_at TEXT NOT NULL,
  retired_to TEXT REFERENCES pb_call_edition(call_id),
  CHECK(outlet_id IS NOT NULL OR organizer_id IS NOT NULL),
  UNIQUE(outlet_id, organizer_id, edition_key, scope_ref)
);
CREATE INDEX pb_call_status ON pb_call_edition(status);

-- Source registry (permission, cadence, retention, unsupported scope). Rows are idempotent (stable_id of canonical_url).
CREATE TABLE pb_source(
  source_id TEXT PRIMARY KEY CHECK(source_id GLOB 'src_*'),
  canonical_url TEXT NOT NULL UNIQUE,
  host TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN
    ('official_guidelines','sitemap','rss','aggregator','directory','social_lead','manual_clip')),
  authority TEXT NOT NULL CHECK(authority IN ('official_primary','attributed_secondary','discovery_lead','unknown')),
  owner_outlet_id TEXT REFERENCES pb_outlet(outlet_id),
  owner_name TEXT,
  default_scope TEXT NOT NULL DEFAULT 'unspecified' REFERENCES pb_scope(scope_ref),
  permission TEXT NOT NULL CHECK(permission IN
    ('allowed_public','allowed_conditional','manual_only','not_collectable','pending_review')),
  permission_basis TEXT NOT NULL,
  permission_checked_on TEXT,                -- YYYY-MM-DD
  collectable_fields TEXT NOT NULL DEFAULT '[]',   -- JSON array of intended fields
  observed_fields TEXT NOT NULL DEFAULT '[]',      -- JSON array of fields actually seen so far
  unsupported_scope TEXT NOT NULL DEFAULT '[]',    -- JSON array, stated explicitly, never hidden
  refresh_hours INTEGER NOT NULL DEFAULT 24 CHECK(refresh_hours > 0),
  retention_excerpt_days INTEGER NOT NULL DEFAULT 365 CHECK(retention_excerpt_days > 0),
  rate_min_interval_s INTEGER NOT NULL DEFAULT 5 CHECK(rate_min_interval_s >= 0),
  max_epistemic TEXT NOT NULL DEFAULT 'verified_fact' CHECK(max_epistemic IN ('verified_fact','source_claim','unverified')),
  last_success_at TEXT,
  last_review_at TEXT,
  notes TEXT NOT NULL DEFAULT ''
);

-- A "check" is one fetch attempt outcome (hash only). A revision is a distinct content version.
CREATE TABLE pb_source_check(
  check_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES pb_source(source_id),
  checked_at TEXT NOT NULL,
  http_status INTEGER,
  content_hash TEXT,
  segment_hashes TEXT NOT NULL DEFAULT '[]',   -- JSON array of short hashes of normalized segments
  segment_count INTEGER NOT NULL DEFAULT 0,
  collection_path TEXT NOT NULL DEFAULT 'auto_fetch' CHECK(collection_path IN ('auto_fetch','manual_clip')),
  processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX pb_check_source ON pb_source_check(source_id, checked_at);

CREATE TABLE pb_source_revision(
  revision_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES pb_source(source_id),
  check_id TEXT REFERENCES pb_source_check(check_id),
  content_hash TEXT NOT NULL,
  segment_hashes TEXT NOT NULL DEFAULT '[]',
  prev_revision_id TEXT REFERENCES pb_source_revision(revision_id),
  change_state TEXT NOT NULL CHECK(change_state IN ('new','changed')),
  changed_segments TEXT NOT NULL DEFAULT '[]', -- JSON array of segment indices that differ from the previous revision
  fetched_at TEXT NOT NULL,
  extracted_version TEXT,                       -- extractor version that has processed this revision
  UNIQUE(source_id, content_hash)
);

-- Evidence: only a sanitized excerpt (<=280 chars) + hash + locator. No raw page text is stored (retention decision 2026-10-27).
CREATE TABLE pb_evidence(
  evidence_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES pb_source_revision(revision_id),
  source_id TEXT NOT NULL REFERENCES pb_source(source_id),
  excerpt TEXT NOT NULL CHECK(length(excerpt) <= 280 AND length(excerpt) > 0),
  quote_hash TEXT NOT NULL,                    -- sha256 of excerpt
  heading TEXT NOT NULL DEFAULT '',
  paragraph_index INTEGER NOT NULL,
  char_start INTEGER NOT NULL DEFAULT 0,
  char_end INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  retain_until TEXT,
  UNIQUE(revision_id, quote_hash, paragraph_index)
);

CREATE TABLE pb_review_decision(
  decision_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('merge_entities','merge_calls','resolve_conflict','reject_fact','other')),
  actor TEXT NOT NULL,
  rationale TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE pb_conflict_group(
  group_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  scope_ref TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('value_conflict','lead_discrepancy')),
  opened_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_decision_id TEXT REFERENCES pb_review_decision(decision_id),
  CHECK((resolved_at IS NULL) = (resolution_decision_id IS NULL))
);
CREATE INDEX pb_conflict_open ON pb_conflict_group(subject_id, predicate, scope_ref, resolved_at);

-- FACT: append-only (V-08). All attributes of all subjects. See docs/DESIGN.md section 2.2.
CREATE TABLE pb_fact(
  fact_id TEXT PRIMARY KEY CHECK(fact_id GLOB 'fact_*'),
  subject_type TEXT NOT NULL CHECK(subject_type IN ('outlet','organizer','call','source')),
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL CHECK(predicate GLOB '*.*'),
  scope_ref TEXT NOT NULL DEFAULT 'unspecified' REFERENCES pb_scope(scope_ref),
  value_json TEXT,
  value_raw TEXT CHECK(value_raw IS NULL OR length(value_raw) <= 280),
  value_state TEXT NOT NULL CHECK(value_state IN
    ('stated','explicitly_none','not_stated_in_inspected_scope','not_inspected')),
  epistemic TEXT NOT NULL CHECK(epistemic IN ('verified_fact','source_claim','ai_inference','unverified')),
  evidence_id TEXT REFERENCES pb_evidence(evidence_id),
  inspected_revision_id TEXT REFERENCES pb_source_revision(revision_id),
  source_id TEXT NOT NULL REFERENCES pb_source(source_id),
  source_authority TEXT NOT NULL CHECK(source_authority IN ('official_primary','attributed_secondary','discovery_lead','unknown')),
  collected_at TEXT NOT NULL CHECK(collected_at GLOB '????-??-??T??:??:??Z'),
  effective_from TEXT,                          -- NULL means UNKNOWN, not "since now"
  effective_to TEXT,
  recorded_at TEXT NOT NULL CHECK(recorded_at GLOB '????-??-??T??:??:??Z'),
  superseded_at TEXT,
  superseded_by TEXT REFERENCES pb_fact(fact_id),
  verified_at TEXT,
  verifier TEXT,
  fresh_until TEXT,
  conflict_group_id TEXT REFERENCES pb_conflict_group(group_id),
  correction_of TEXT REFERENCES pb_fact(fact_id),
  correction_reason TEXT,
  extractor TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  run_id TEXT,
  retain_until TEXT,
  -- V-01: stated/explicitly_none need evidence; not_stated needs the inspected revision; others carry no claimed value.
  CHECK(value_state NOT IN ('stated','explicitly_none') OR evidence_id IS NOT NULL),
  CHECK(value_state != 'not_stated_in_inspected_scope' OR inspected_revision_id IS NOT NULL),
  CHECK(value_state IN ('not_stated_in_inspected_scope','not_inspected') OR value_json IS NOT NULL),
  -- V-02 (DB level): verified_fact needs official authority, evidence, verifier, verification time, freshness window.
  CHECK(epistemic != 'verified_fact' OR (
        source_authority = 'official_primary' AND evidence_id IS NOT NULL AND verified_at IS NOT NULL
        AND verifier IS NOT NULL AND fresh_until IS NOT NULL AND value_state IN ('stated','explicitly_none'))),
  -- V-09: model-made values can never exceed ai_inference.
  CHECK(extractor NOT LIKE 'ai:%' OR epistemic IN ('ai_inference','unverified')),
  CHECK((superseded_at IS NULL) = (superseded_by IS NULL)),
  CHECK(correction_of IS NULL OR correction_reason IS NOT NULL)
);
CREATE INDEX pb_fact_subject ON pb_fact(subject_id, predicate, scope_ref, superseded_at);
CREATE INDEX pb_fact_source ON pb_fact(source_id);
CREATE INDEX pb_fact_conflict ON pb_fact(conflict_group_id);

-- V-08: append-only. Only superseded_at/superseded_by/conflict_group_id may be set, once, on a live fact.
CREATE TRIGGER pb_fact_no_delete BEFORE DELETE ON pb_fact
BEGIN SELECT RAISE(ABORT, 'V-08: pb_fact is append-only'); END;

CREATE TRIGGER pb_fact_append_only BEFORE UPDATE ON pb_fact
WHEN NEW.fact_id IS NOT OLD.fact_id OR NEW.subject_type IS NOT OLD.subject_type OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.predicate IS NOT OLD.predicate OR NEW.scope_ref IS NOT OLD.scope_ref OR NEW.value_json IS NOT OLD.value_json
  OR NEW.value_raw IS NOT OLD.value_raw OR NEW.value_state IS NOT OLD.value_state OR NEW.epistemic IS NOT OLD.epistemic
  OR NEW.evidence_id IS NOT OLD.evidence_id OR NEW.inspected_revision_id IS NOT OLD.inspected_revision_id
  OR NEW.source_id IS NOT OLD.source_id OR NEW.source_authority IS NOT OLD.source_authority
  OR NEW.collected_at IS NOT OLD.collected_at OR NEW.effective_from IS NOT OLD.effective_from
  OR NEW.effective_to IS NOT OLD.effective_to OR NEW.recorded_at IS NOT OLD.recorded_at
  OR NEW.verified_at IS NOT OLD.verified_at OR NEW.verifier IS NOT OLD.verifier OR NEW.fresh_until IS NOT OLD.fresh_until
  OR NEW.correction_of IS NOT OLD.correction_of OR NEW.correction_reason IS NOT OLD.correction_reason
  OR NEW.extractor IS NOT OLD.extractor OR NEW.extractor_version IS NOT OLD.extractor_version
  OR NEW.run_id IS NOT OLD.run_id OR NEW.retain_until IS NOT OLD.retain_until
  OR (OLD.superseded_at IS NOT NULL AND (NEW.superseded_at IS NOT OLD.superseded_at OR NEW.superseded_by IS NOT OLD.superseded_by))
  OR (OLD.conflict_group_id IS NOT NULL AND NEW.conflict_group_id IS NOT OLD.conflict_group_id)
BEGIN SELECT RAISE(ABORT, 'V-08: pb_fact is append-only (only supersede / conflict link once)'); END;

-- Re-confirmation of an unchanged fact (does not rewrite the fact): extends effective freshness.
CREATE TABLE pb_fact_confirmation(
  confirmation_id TEXT PRIMARY KEY,
  fact_id TEXT NOT NULL REFERENCES pb_fact(fact_id),
  revision_id TEXT NOT NULL REFERENCES pb_source_revision(revision_id),
  evidence_id TEXT REFERENCES pb_evidence(evidence_id),
  confirmed_at TEXT NOT NULL,
  fresh_until TEXT
);
CREATE INDEX pb_confirm_fact ON pb_fact_confirmation(fact_id, confirmed_at);

-- Candidates between extraction (stage 4) and fact materialisation (stage 7).
CREATE TABLE pb_fact_candidate(
  candidate_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES pb_source_revision(revision_id),
  source_id TEXT NOT NULL REFERENCES pb_source(source_id),
  predicate TEXT NOT NULL,
  scope_ref TEXT NOT NULL REFERENCES pb_scope(scope_ref),
  value_json TEXT,
  value_raw TEXT,
  value_state TEXT NOT NULL,
  evidence_id TEXT REFERENCES pb_evidence(evidence_id),
  window_open TEXT,                             -- window tag (opens_on) when the statement belongs to a dated window
  window_close TEXT,
  collected_at TEXT NOT NULL,
  extractor TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'new' CHECK(state IN ('new','resolved','unresolved','verified','claimed','rejected','materialized')),
  subject_type TEXT,
  subject_id TEXT,
  verdict_epistemic TEXT,
  verdict_reason TEXT,
  verified_at TEXT,
  fresh_until TEXT,
  fact_id TEXT REFERENCES pb_fact(fact_id),
  UNIQUE(revision_id, predicate, scope_ref, value_json, evidence_id)
);
CREATE INDEX pb_candidate_state ON pb_fact_candidate(state);

CREATE TABLE pb_status_event(
  event_id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL REFERENCES pb_call_edition(call_id),
  kind TEXT NOT NULL CHECK(kind IN ('transition','deadline_changed','correction','note')),
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  basis TEXT NOT NULL CHECK(basis IN ('evidence','system_clock','review','backfill')),
  evidence_id TEXT REFERENCES pb_evidence(evidence_id),
  fact_id TEXT REFERENCES pb_fact(fact_id),
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  correction_of TEXT REFERENCES pb_status_event(event_id),
  CHECK(basis != 'evidence' OR evidence_id IS NOT NULL OR fact_id IS NOT NULL)
);
CREATE INDEX pb_status_call ON pb_status_event(call_id, recorded_at);
CREATE TRIGGER pb_status_no_update BEFORE UPDATE ON pb_status_event
BEGIN SELECT RAISE(ABORT, 'pb_status_event is append-only'); END;
CREATE TRIGGER pb_status_no_delete BEFORE DELETE ON pb_status_event
BEGIN SELECT RAISE(ABORT, 'pb_status_event is append-only'); END;

-- V-15: merges are proposals until a review decision exists. No automatic merge.
CREATE TABLE pb_merge_candidate(
  candidate_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK(subject_type IN ('outlet','call')),
  left_id TEXT NOT NULL,
  right_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'needs_review' CHECK(state IN ('needs_review','merged','rejected')),
  decision_id TEXT REFERENCES pb_review_decision(decision_id),
  created_at TEXT NOT NULL,
  UNIQUE(subject_type, left_id, right_id)
);

-- Service-facing projection (what search/service layers read; frontend is out of scope).
CREATE TABLE pb_search_projection(
  call_id TEXT PRIMARY KEY REFERENCES pb_call_edition(call_id),
  projection_version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  as_of TEXT NOT NULL,
  source_revision_ids TEXT NOT NULL DEFAULT '[]',
  computed_at TEXT NOT NULL,
  built_for_dirty_seq INTEGER NOT NULL
);
CREATE TABLE pb_projection_tombstone(
  call_id TEXT NOT NULL,
  removed_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  last_version INTEGER NOT NULL,
  PRIMARY KEY(call_id, removed_at)
);
CREATE TABLE pb_outbox(
  event_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  subject_id TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
