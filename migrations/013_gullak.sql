-- Gullak savings (Phase H). Built, but switched off by the setting gullak.enabled until the CA approves.
CREATE TABLE IF NOT EXISTS gullak_accounts (
    user_id         BIGINT PRIMARY KEY REFERENCES users(id),
    opening_charge  BIGINT NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Each deposit is kept apart: its own 6-month date, rate and interest (Vinay, 15 Oct).
CREATE TABLE IF NOT EXISTS gullak_deposits (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL REFERENCES gullak_accounts(user_id),
    taken             BIGINT NOT NULL CHECK (taken > 0),       -- from the Income Wallet
    charge            BIGINT NOT NULL CHECK (charge >= 0),     -- 3% inside: company income
    amount            BIGINT NOT NULL CHECK (amount > 0),      -- in the Gullak = taken - charge
    interest_percent  NUMERIC(5, 2) NOT NULL,                  -- as it was on the day of the deposit
    interest_from     DATE NOT NULL,                           -- interest only if withdrawn on/after this day
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    interest          BIGINT,                                  -- gross interest paid at withdrawal
    interest_tds      BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at      TIMESTAMPTZ,
    CHECK (taken = amount + charge)
);
CREATE INDEX IF NOT EXISTS gullak_deposits_user_idx ON gullak_deposits (user_id, id DESC);
CREATE INDEX IF NOT EXISTS gullak_deposits_active_idx ON gullak_deposits (status) WHERE status = 'active';
