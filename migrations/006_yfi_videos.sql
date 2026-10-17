-- YFI daily videos (Phase B). ₹20 goes to the Product Wallet the moment the last video of the day is done.

-- The admin sets the day's links (4 a day; slot = order shown to members).
CREATE TABLE IF NOT EXISTS daily_videos (
    video_date  DATE NOT NULL,
    slot        SMALLINT NOT NULL CHECK (slot BETWEEN 1 AND 10),
    platform    TEXT NOT NULL CHECK (platform IN ('youtube', 'facebook', 'instagram')),
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    updated_by  BIGINT REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (video_date, slot)
);

-- One row per member per day (IST). Videos go in order; only one can be running.
-- `log` keeps every start / finish time: {"1": {"started": ..., "done": ...}, ...}
-- This is an activity log, not money (money is in wallet_ledger), so rows older than 60 days are cleaned up.
CREATE TABLE IF NOT EXISTS video_days (
    user_id         BIGINT NOT NULL REFERENCES users(id),
    watch_date      DATE NOT NULL,
    done_slots      SMALLINT NOT NULL DEFAULT 0,
    current_slot    SMALLINT,
    current_started TIMESTAMPTZ,
    log             JSONB NOT NULL DEFAULT '{}'::jsonb,
    rewarded_at     TIMESTAMPTZ,
    PRIMARY KEY (user_id, watch_date)
);
CREATE INDEX IF NOT EXISTS video_days_date_idx ON video_days (watch_date);
