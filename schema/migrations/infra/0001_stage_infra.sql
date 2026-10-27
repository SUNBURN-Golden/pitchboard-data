-- Pitchboard stage-runner infra DB (separate file from the service DB and from the crawler's inbox.sqlite3).
-- Operational state (mutable). ADDITIVE ONLY migrations.
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  report TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE stage_run(
  run_key TEXT PRIMARY KEY,                  -- sha256(stage|subject_id|input_digest)
  stage TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  output_digest TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','done','retry_wait','failed','pending')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at TEXT,
  lease_expires TEXT,
  started_at TEXT,
  finished_at TEXT,
  error_code TEXT,
  run_id TEXT
);
CREATE INDEX stage_run_status ON stage_run(stage, status);
CREATE TABLE failure_queue(
  failure_id TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  error_code TEXT NOT NULL,
  error_detail TEXT NOT NULL DEFAULT '',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  resume_from TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','requeued','dismissed')),
  requeued_at TEXT,
  UNIQUE(stage, subject_id, input_digest)
);
CREATE TABLE cost_ledger(
  day TEXT NOT NULL,
  resource TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(day, resource)
);
