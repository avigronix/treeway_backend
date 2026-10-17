-- "2 new messages" on the complaint lists: the last message id each side has read.
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS member_last_read BIGINT NOT NULL DEFAULT 0;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS admin_last_read BIGINT NOT NULL DEFAULT 0;
