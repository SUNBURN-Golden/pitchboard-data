ALTER TABLE pb_source ADD COLUMN retired_reason TEXT CHECK(retired_reason IN ('share_variant', 'binary_asset'));
