-- Cash Book (Vinay, 2 Nov): a member's own notebook of cash in / out. NOT Treeway money: no wallet, no ledger.
-- Everything is tied to a 10-digit mobile number (no FK to users): a book can be shared with a number before
-- that person has an account; they see it on their first login.
CREATE TABLE IF NOT EXISTS cb_businesses (
    id            BIGSERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    owner_mobile  VARCHAR(10) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cb_businesses_owner_idx ON cb_businesses (owner_mobile);

CREATE TABLE IF NOT EXISTS cb_books (
    id            BIGSERIAL PRIMARY KEY,
    business_id   BIGINT NOT NULL REFERENCES cb_businesses(id) ON DELETE CASCADE,
    name          VARCHAR(100) NOT NULL,
    owner_mobile  VARCHAR(10) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cb_books_business_idx ON cb_books (business_id);
CREATE INDEX IF NOT EXISTS cb_books_owner_idx ON cb_books (owner_mobile);

CREATE TABLE IF NOT EXISTS cb_book_members (
    id        BIGSERIAL PRIMARY KEY,
    book_id   BIGINT NOT NULL REFERENCES cb_books(id) ON DELETE CASCADE,
    mobile    VARCHAR(10) NOT NULL,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (book_id, mobile)
);
CREATE INDEX IF NOT EXISTS cb_book_members_mobile_idx ON cb_book_members (mobile);

CREATE TABLE IF NOT EXISTS cb_entries (
    id                 BIGSERIAL PRIMARY KEY,
    book_id            BIGINT NOT NULL REFERENCES cb_books(id) ON DELETE CASCADE,
    type               VARCHAR(3) NOT NULL CHECK (type IN ('in', 'out')),
    amount_paise       BIGINT NOT NULL CHECK (amount_paise > 0),
    entry_date         TIMESTAMPTZ NOT NULL,
    remark             VARCHAR(300),
    created_by_mobile  VARCHAR(10) NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cb_entries_book_date_idx ON cb_entries (book_id, entry_date DESC);
