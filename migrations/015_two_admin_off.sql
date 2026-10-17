-- 2-admin approval switched OFF (bhaiya, 15 Oct 2026): any one admin approves any amount.
-- The feature stays in the code; `two_admin_threshold` = 0 means OFF.

-- 1. A waiting approval can now also be 'cancelled' (when the feature is switched off).
ALTER TABLE admin_approvals DROP CONSTRAINT IF EXISTS admin_approvals_status_check;
ALTER TABLE admin_approvals ADD CONSTRAINT admin_approvals_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'));

-- 2. Switch it off on databases that already have the old ₹10,000 value.
UPDATE app_settings SET value = '0'::jsonb, updated_at = now() WHERE key = 'two_admin_threshold';

-- 3. Old waiting approvals: cancel them (audited as the main admin). The payment request / debit / PIN issue /
--    reverse they were for did not run, so it is still open and one admin can now do it directly.
WITH c AS (
    UPDATE admin_approvals
       SET status = 'cancelled', decided_at = now(),
           remark = '2-admin approval was switched off. Nothing was done; do it again from its own screen.'
     WHERE status = 'pending'
 RETURNING id, action, target, amount
)
INSERT INTO admin_audit (admin_id, action, target, detail)
SELECT u.id, 'approval_cancelled', c.id::text,
       jsonb_build_object('action', c.action, 'target', c.target, 'amount', c.amount, 'reason', 'migration 015')
  FROM c JOIN users u ON u.user_code = 'TW1000000';
