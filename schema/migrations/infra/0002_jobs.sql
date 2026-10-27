CREATE TABLE job(
  job_id TEXT PRIMARY KEY,
  idem_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,                          -- discover|evaluate|fetch_page|expand_site|gap_fill|refresh
  budget_class TEXT NOT NULL CHECK(budget_class IN ('backfill','discovery','freshness')),
  payload TEXT NOT NULL DEFAULT '{}',
  priority REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','done','retry_wait','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at TEXT,
  lease_expires TEXT,
  depth INTEGER NOT NULL DEFAULT 0,
  parent_job_id TEXT,
  last_error TEXT,
  result TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX job_pick ON job(state, budget_class, priority);
CREATE TABLE budget_ledger(
  day TEXT NOT NULL,
  budget_class TEXT NOT NULL,
  resource TEXT NOT NULL,                      -- requests|jobs|candidates
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, budget_class, resource)
);
CREATE TABLE host_state(
  host TEXT PRIMARY KEY,
  last_request_at REAL NOT NULL DEFAULT 0,
  robots_text TEXT,
  robots_status INTEGER,
  robots_fetched_at REAL,
  crawl_delay REAL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  halted_until REAL,
  halt_reason TEXT
);
CREATE TABLE checkpoint(
  run_id TEXT NOT NULL,
  name TEXT NOT NULL,
  cursor TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(run_id, name)
);
