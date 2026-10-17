-- Phase I1: indexes found with EXPLAIN ANALYZE on 1 lakh members (docs/PERFORMANCE.md).
-- Covering indexes (INCLUDE) let the reports and dashboards read only the index, not the big tables.
-- On a live big database build them with CREATE INDEX CONCURRENTLY by hand first (the migration then skips
-- them because of IF NOT EXISTS); before launch the tables are small and this is instant.

-- TDS / income reports and exports: income rows of a period.
CREATE INDEX IF NOT EXISTS wallet_ledger_type_time_cover ON wallet_ledger (type, created_at)
    INCLUDE (user_id, amount, gross, tds, service, single_leg, other_cut);
DROP INDEX IF EXISTS wallet_ledger_type_idx;

-- Member dashboard / income summary / level income: one member's rows of one type.
CREATE INDEX IF NOT EXISTS wallet_ledger_user_type_cover ON wallet_ledger (user_id, type, created_at DESC)
    INCLUDE (amount, gross);
DROP INDEX IF EXISTS wallet_ledger_user_type_idx;

-- Admin totals, P&L, Super 3 stats, TDS: company rows by type and time.
CREATE INDEX IF NOT EXISTS company_ledger_type_time_cover ON company_ledger (type, created_at) INCLUDE (amount, user_id);
DROP INDEX IF EXISTS company_ledger_type_idx;
