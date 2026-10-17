-- Royalty and Super 3 Level, paid on the 3rd for the previous month (Phase D).
-- One run per month and kind. computed -> done; payments go in chunks with unique refs (resume after a crash).
CREATE TABLE IF NOT EXISTS monthly_runs (
    id            BIGSERIAL PRIMARY KEY,
    month         DATE NOT NULL,                       -- first day of the month the pool belongs to
    kind          TEXT NOT NULL CHECK (kind IN ('royalty', 's3_level')),
    status        TEXT NOT NULL DEFAULT 'computed' CHECK (status IN ('computed', 'done')),
    pool_total    BIGINT NOT NULL DEFAULT 0,
    shared        BIGINT NOT NULL DEFAULT 0,           -- given to members (paid + lapsed)
    company       BIGINT NOT NULL DEFAULT 0,           -- empty pools + rounding
    receivers     INT NOT NULL DEFAULT 0,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- turnover, per rank pool / members / each
    paid_by       BIGINT REFERENCES users(id),         -- NULL = automatic backup run
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    UNIQUE (month, kind)
);

CREATE TABLE IF NOT EXISTS monthly_shares (
    run_id   BIGINT NOT NULL REFERENCES monthly_runs(id),
    user_id  BIGINT NOT NULL REFERENCES users(id),
    gross    BIGINT NOT NULL CHECK (gross > 0),
    rank     SMALLINT,                                 -- royalty: the rank pool it came from
    paid_at  TIMESTAMPTZ,
    lapsed   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, user_id)
);
CREATE INDEX IF NOT EXISTS monthly_shares_unpaid_idx ON monthly_shares (run_id, user_id) WHERE paid_at IS NULL;
CREATE INDEX IF NOT EXISTS users_s3l_reached_idx ON users (s3l_reached_on) WHERE s3l_reached_on IS NOT NULL;
