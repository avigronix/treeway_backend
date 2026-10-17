-- Treeway initial schema.
-- All money is stored in paise (BIGINT). ₹987 = 98700.

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_by  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- users
CREATE SEQUENCE IF NOT EXISTS single_leg_seq START 1;

CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    user_code       TEXT NOT NULL UNIQUE,
    sponsor_id      BIGINT REFERENCES users(id),
    full_name       TEXT NOT NULL,
    mobile          TEXT NOT NULL,
    email           TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    dob             DATE,
    state           TEXT,
    district        TEXT,
    role            TEXT NOT NULL DEFAULT 'USER' CHECK (role IN ('USER', 'ADMIN', 'DOCTOR')),
    status          TEXT NOT NULL DEFAULT 'inactive' CHECK (status IN ('inactive', 'active', 'blocked', 'removed')),
    activated_at    TIMESTAMPTZ,
    single_leg_no   BIGINT UNIQUE,
    direct_count    INT NOT NULL DEFAULT 0,
    pan             TEXT,
    bank_holder     TEXT,
    bank_account    TEXT,
    bank_ifsc       TEXT,
    bank_branch     TEXT,
    bank_updated_at TIMESTAMPTZ,
    token_version   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One mobile = one ID. A removed (never activated) ID frees its mobile again.
CREATE UNIQUE INDEX IF NOT EXISTS users_mobile_uq ON users (mobile) WHERE status <> 'removed';
CREATE INDEX IF NOT EXISTS users_sponsor_idx ON users (sponsor_id);
CREATE INDEX IF NOT EXISTS users_status_created_idx ON users (status, created_at);

-- Upline chain, only 25 levels deep (enough for level income and team views).
CREATE TABLE IF NOT EXISTS user_uplines (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level       SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 25),
    ancestor_id BIGINT NOT NULL REFERENCES users(id),
    PRIMARY KEY (user_id, level)
);
CREATE INDEX IF NOT EXISTS user_uplines_ancestor_idx ON user_uplines (ancestor_id, level);

-- ---------------------------------------------------------------- wallets
CREATE TABLE IF NOT EXISTS wallets (
    user_id         BIGINT PRIMARY KEY REFERENCES users(id),
    income          BIGINT NOT NULL DEFAULT 0 CHECK (income >= 0),
    fund            BIGINT NOT NULL DEFAULT 0 CHECK (fund >= 0),
    product         BIGINT NOT NULL DEFAULT 0 CHECK (product >= 0),
    total_earned    BIGINT NOT NULL DEFAULT 0,
    total_withdrawn BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every rupee that moves in a member wallet. `ref` is unique, so the same
-- income can never be credited twice (safe against retries and double clicks).
CREATE TABLE IF NOT EXISTS wallet_ledger (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id),
    wallet          TEXT NOT NULL CHECK (wallet IN ('income', 'fund', 'product')),
    type            TEXT NOT NULL,
    amount          BIGINT NOT NULL,
    balance_after   BIGINT NOT NULL,
    gross           BIGINT NOT NULL DEFAULT 0,
    tds             BIGINT NOT NULL DEFAULT 0,
    service         BIGINT NOT NULL DEFAULT 0,
    single_leg      BIGINT NOT NULL DEFAULT 0,
    other_cut       BIGINT NOT NULL DEFAULT 0,
    ref             TEXT NOT NULL UNIQUE,
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    visible_to_user BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wallet_ledger_user_idx ON wallet_ledger (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS wallet_ledger_type_idx ON wallet_ledger (type, created_at);

-- Company side money: turnover, TDS, service charge, Self Help Fund, holds, pools.
CREATE TABLE IF NOT EXISTS company_ledger (
    id          BIGSERIAL PRIMARY KEY,
    type        TEXT NOT NULL,
    amount      BIGINT NOT NULL,
    user_id     BIGINT REFERENCES users(id),
    ref         TEXT NOT NULL UNIQUE,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_ledger_type_idx ON company_ledger (type, created_at);

-- 5% Single Leg cut of every income. Shared every night at 12 (later phase).
CREATE TABLE IF NOT EXISTS single_leg_pool (
    id              BIGSERIAL PRIMARY KEY,
    earner_id       BIGINT NOT NULL REFERENCES users(id),
    amount          BIGINT NOT NULL CHECK (amount > 0),
    ref             TEXT NOT NULL UNIQUE,
    distributed_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS single_leg_pool_pending_idx ON single_leg_pool (created_at) WHERE distributed_at IS NULL;

-- ---------------------------------------------------------------- payments in
-- One table for all money coming in by UTR: ID activation, add funds, buy PINs.
CREATE TABLE IF NOT EXISTS payment_requests (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id),
    purpose      TEXT NOT NULL CHECK (purpose IN ('activation', 'fund', 'pins')),
    amount       BIGINT NOT NULL CHECK (amount > 0),
    pin_qty      INT NOT NULL DEFAULT 0 CHECK (pin_qty >= 0),
    utr          TEXT NOT NULL,
    receipt_path TEXT NOT NULL,
    note         TEXT,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    admin_remark TEXT,
    reviewed_by  BIGINT REFERENCES users(id),
    reviewed_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A UTR can be used once (a rejected UTR can be sent again).
CREATE UNIQUE INDEX IF NOT EXISTS payment_requests_utr_uq ON payment_requests (utr) WHERE status <> 'rejected';
-- Only one activation request waiting at a time per member.
CREATE UNIQUE INDEX IF NOT EXISTS payment_requests_one_activation_uq ON payment_requests (user_id) WHERE purpose = 'activation' AND status = 'pending';
CREATE INDEX IF NOT EXISTS payment_requests_status_idx ON payment_requests (status, created_at);
CREATE INDEX IF NOT EXISTS payment_requests_user_idx ON payment_requests (user_id, created_at DESC);

-- ---------------------------------------------------------------- PINs
CREATE TABLE IF NOT EXISTS pins (
    id                BIGSERIAL PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    owner_id          BIGINT NOT NULL REFERENCES users(id),
    bought_by         BIGINT NOT NULL REFERENCES users(id),
    status            TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'used')),
    used_for          BIGINT REFERENCES users(id),
    used_by           BIGINT REFERENCES users(id),
    used_at           TIMESTAMPTZ,
    source            TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pins_owner_idx ON pins (owner_id, status);

CREATE TABLE IF NOT EXISTS pin_transfers (
    id          BIGSERIAL PRIMARY KEY,
    pin_id      BIGINT NOT NULL REFERENCES pins(id),
    from_user   BIGINT NOT NULL REFERENCES users(id),
    to_user     BIGINT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pin_transfers_from_idx ON pin_transfers (from_user, created_at DESC);
CREATE INDEX IF NOT EXISTS pin_transfers_to_idx ON pin_transfers (to_user, created_at DESC);

-- ---------------------------------------------------------------- withdrawals
CREATE TABLE IF NOT EXISTS withdrawals (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id),
    amount       BIGINT NOT NULL CHECK (amount > 0),
    bank         JSONB NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'rejected')),
    utr          TEXT,
    admin_remark TEXT,
    reviewed_by  BIGINT REFERENCES users(id),
    reviewed_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS withdrawals_utr_uq ON withdrawals (utr) WHERE utr IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS withdrawals_one_pending_uq ON withdrawals (user_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS withdrawals_status_idx ON withdrawals (status, created_at);

-- ---------------------------------------------------------------- Super 3
-- Every journey through the 8 boards. A member has 1 main journey and gets
-- a new "recycle" journey each time a board 2-8 fills.
CREATE TABLE IF NOT EXISTS super3_journeys (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),          -- NULL = company ID
    kind        TEXT NOT NULL CHECK (kind IN ('main', 'recycle', 'company')),
    current_board SMALLINT NOT NULL DEFAULT 1,
    finished    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS super3_journeys_user_idx ON super3_journeys (user_id);

-- One global line per board. Children of spot P are 3P-1, 3P, 3P+1.
CREATE TABLE IF NOT EXISTS super3_nodes (
    id          BIGSERIAL PRIMARY KEY,
    board       SMALLINT NOT NULL CHECK (board BETWEEN 1 AND 8),
    position    BIGINT NOT NULL,
    journey_id  BIGINT NOT NULL REFERENCES super3_journeys(id),
    user_id     BIGINT REFERENCES users(id),
    children    SMALLINT NOT NULL DEFAULT 0 CHECK (children BETWEEN 0 AND 3),
    status      TEXT NOT NULL DEFAULT 'filling' CHECK (status IN ('filling', 'full')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    full_at     TIMESTAMPTZ,
    UNIQUE (board, position)
);
CREATE INDEX IF NOT EXISTS super3_nodes_user_idx ON super3_nodes (user_id, board);
CREATE INDEX IF NOT EXISTS super3_nodes_created_idx ON super3_nodes (board, created_at);

CREATE TABLE IF NOT EXISTS super3_counters (
    board         SMALLINT PRIMARY KEY,
    next_position BIGINT NOT NULL
);

-- ---------------------------------------------------------------- complaints
CREATE TABLE IF NOT EXISTS complaint_categories (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS complaints (
    id              BIGSERIAL PRIMARY KEY,
    complaint_no    TEXT NOT NULL UNIQUE,
    user_id         BIGINT NOT NULL REFERENCES users(id),
    category_id     BIGINT NOT NULL REFERENCES complaint_categories(id),
    status          TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft', 'Submitted', 'In Process', 'Resolved', 'Closed', 'Rejected')),
    incident        JSONB NOT NULL DEFAULT '{}'::jsonb,
    suspect         JSONB NOT NULL DEFAULT '{}'::jsonb,
    complainant     JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence        JSONB NOT NULL DEFAULT '[]'::jsonb,
    share_with_agencies BOOLEAN NOT NULL DEFAULT FALSE,
    remarks         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS complaints_user_idx ON complaints (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS complaints_status_idx ON complaints (status, created_at);

CREATE TABLE IF NOT EXISTS complaint_messages (
    id            BIGSERIAL PRIMARY KEY,
    complaint_id  BIGINT NOT NULL REFERENCES complaints(id),
    sender_id     BIGINT NOT NULL REFERENCES users(id),
    message       TEXT,
    file_path     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (message IS NOT NULL OR file_path IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS complaint_messages_idx ON complaint_messages (complaint_id, created_at);

-- ---------------------------------------------------------------- admin audit
CREATE TABLE IF NOT EXISTS admin_audit (
    id          BIGSERIAL PRIMARY KEY,
    admin_id    BIGINT NOT NULL REFERENCES users(id),
    action      TEXT NOT NULL,
    target      TEXT,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_audit_idx ON admin_audit (created_at DESC);
