ALTER TABLE pb_source ADD COLUMN deprioritized_reason TEXT CHECK(deprioritized_reason IN ('off_topic_path', 'relative_url'));
