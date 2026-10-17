-- Ranks (Phase D). Computed every night over the FULL team depth (user_uplines only keeps 25 levels).
-- rank: 0 = none, 1 Distributor, 2 Silver, 3 Gold, 4 Platinum, 5 Emerald, 6 Diamond.
CREATE TABLE IF NOT EXISTS user_rank_stats (
    user_id          BIGINT PRIMARY KEY REFERENCES users(id),
    active_directs   INT NOT NULL DEFAULT 0,
    active_team      INT NOT NULL DEFAULT 0,       -- full depth, active IDs only, not counting the member
    biggest_leg      INT NOT NULL DEFAULT 0,       -- active IDs in the biggest direct line (incl. that direct)
    biggest_leg_user BIGINT REFERENCES users(id),
    rank             SMALLINT NOT NULL DEFAULT 0 CHECK (rank BETWEEN 0 AND 6),
    next_counted     INT NOT NULL DEFAULT 0,       -- toward the next rank, after the 50:50 rule
    computed_at      TIMESTAMPTZ NOT NULL           -- "as of" time (start of the night)
);
CREATE INDEX IF NOT EXISTS user_rank_stats_rank_idx ON user_rank_stats (rank) WHERE rank > 0;

-- Rank on the last day of each month (Vinay, 14 Oct): royalty for that month uses this.
CREATE TABLE IF NOT EXISTS rank_snapshot_months (
    month     DATE PRIMARY KEY,                     -- first day of the month
    as_of     TIMESTAMPTZ NOT NULL,                 -- end of that month (IST midnight)
    taken_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rank_snapshots (
    month           DATE NOT NULL REFERENCES rank_snapshot_months(month),
    user_id         BIGINT NOT NULL REFERENCES users(id),
    rank            SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 6),
    active_directs  INT NOT NULL,
    active_team     INT NOT NULL,
    PRIMARY KEY (month, user_id)
);
CREATE INDEX IF NOT EXISTS rank_snapshots_rank_idx ON rank_snapshots (month, rank);

-- Super 3 Level: the day a member first had 10 active directs. Payouts: the 3rd of the next 6 months.
ALTER TABLE users ADD COLUMN IF NOT EXISTS s3l_reached_on DATE;
