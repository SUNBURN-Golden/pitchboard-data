-- Support the daemon's per-source successful-fetch existence check.
CREATE INDEX pb_raw_ref_ok_source ON pb_raw_ref(source_id) WHERE outcome='ok';
