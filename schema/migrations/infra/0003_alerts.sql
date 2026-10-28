CREATE TABLE alert(
  alert_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                          -- extraction_breakage|field_lost|segments_collapsed|login_wall|page_gone|robots_denied
  url TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  raw_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(kind, url, raw_id)
);
CREATE TABLE metric_snapshot(
  snapshot_id TEXT PRIMARY KEY,
  taken_at TEXT NOT NULL,
  label TEXT NOT NULL,
  metrics TEXT NOT NULL
);
