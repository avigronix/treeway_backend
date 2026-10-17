-- Reverse entries (Vinay, 14 Oct 2026): reversing an income cancels its Single Leg pool row
-- if that row was not shared yet. A cancelled row is never shared by the nightly job.
ALTER TABLE single_leg_pool ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

DROP INDEX IF EXISTS single_leg_pool_pending_idx;
CREATE INDEX IF NOT EXISTS single_leg_pool_pending_idx ON single_leg_pool (created_at)
    WHERE distributed_at IS NULL AND cancelled_at IS NULL;
