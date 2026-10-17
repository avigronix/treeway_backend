-- Single Leg nightly distribution (Phase C).
-- One run per night. Steps: claimed (pool rows tagged) -> computed (each receiver's total) -> done (all paid).
-- A crash at any step is resumed by the next run of the job; refs make every payment happen once.
CREATE TABLE IF NOT EXISTS single_leg_runs (
    id           BIGSERIAL PRIMARY KEY,
    run_date     DATE NOT NULL UNIQUE,              -- IST night it belongs to
    status       TEXT NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed', 'computed', 'done')),
    pool_rows    INT NOT NULL DEFAULT 0,
    pool_total   BIGINT NOT NULL DEFAULT 0,          -- paise in the pool rows of this run
    shared       BIGINT NOT NULL DEFAULT 0,          -- given to people in the line (paid + lapsed)
    company      BIGINT NOT NULL DEFAULT 0,          -- missing people + rounding
    receivers    INT NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

ALTER TABLE single_leg_pool ADD COLUMN IF NOT EXISTS run_id BIGINT REFERENCES single_leg_runs(id);
CREATE INDEX IF NOT EXISTS single_leg_pool_run_idx ON single_leg_pool (run_id) WHERE run_id IS NOT NULL;

-- Each receiver's total for a run (one Income Wallet entry per receiver per night).
CREATE TABLE IF NOT EXISTS single_leg_shares (
    run_id   BIGINT NOT NULL REFERENCES single_leg_runs(id),
    user_id  BIGINT NOT NULL REFERENCES users(id),
    gross    BIGINT NOT NULL CHECK (gross > 0),
    earners  INT NOT NULL,                           -- how many people's income it came from
    paid_at  TIMESTAMPTZ,
    lapsed   BOOLEAN NOT NULL DEFAULT FALSE,         -- blocked member: the share went to the company
    PRIMARY KEY (run_id, user_id)
);
CREATE INDEX IF NOT EXISTS single_leg_shares_unpaid_idx ON single_leg_shares (run_id, user_id) WHERE paid_at IS NULL;
