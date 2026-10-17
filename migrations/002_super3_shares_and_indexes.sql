-- Super 3 entry is decided by REAL deposited shares, not by direct_count (Vinay, 14 Oct 2026).
-- 0 = nothing yet, 1 = ₹300 on hold, 2 = on Board 1 (main journey started).
-- A share that came while the sponsor was blocked lapsed to the company and is not counted.
ALTER TABLE users ADD COLUMN IF NOT EXISTS s3_shares SMALLINT NOT NULL DEFAULT 0 CHECK (s3_shares BETWEEN 0 AND 2);

UPDATE users u SET s3_shares = CASE
    WHEN EXISTS (SELECT 1 FROM super3_journeys j WHERE j.user_id = u.id AND j.kind = 'main') THEN 2
    WHEN (SELECT coalesce(sum(amount), 0) FROM company_ledger c WHERE c.user_id = u.id AND c.type = 's3_hold') > 0 THEN 1
    ELSE 0 END
WHERE u.role = 'USER';

-- Missing indexes found in the Phase A review.
CREATE INDEX IF NOT EXISTS pins_used_by_idx ON pins (used_by, used_at DESC) WHERE used_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS company_ledger_user_type_idx ON company_ledger (user_id, type) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS users_activated_idx ON users (activated_at) WHERE activated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS wallet_ledger_user_type_idx ON wallet_ledger (user_id, type, created_at DESC);
