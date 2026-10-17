-- 2-admin approval for big amounts (Vinay, 14 Oct 2026: ₹10,000 and above, `two_admin_threshold` setting).
-- The first admin's action waits here; a DIFFERENT admin approves it and only then it runs.
CREATE TABLE IF NOT EXISTS admin_approvals (
    id            BIGSERIAL PRIMARY KEY,
    action        TEXT NOT NULL,                  -- payment_approve | wallet_debit | pins_issue | reverse_entry
    target        TEXT NOT NULL,                  -- request id / member ID / ledger id
    amount        BIGINT NOT NULL CHECK (amount > 0),
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_by  BIGINT NOT NULL REFERENCES users(id),
    decided_by    BIGINT REFERENCES users(id),
    remark        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ,
    CHECK (decided_by IS NULL OR decided_by <> requested_by)
);
-- One waiting approval per action + target (double click cannot make two).
CREATE UNIQUE INDEX IF NOT EXISTS admin_approvals_one_pending_uq ON admin_approvals (action, target) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS admin_approvals_status_idx ON admin_approvals (status, created_at);
