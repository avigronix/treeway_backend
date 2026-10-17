-- Shop, courier and orders (Phase E).

CREATE TABLE IF NOT EXISTS products (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    mrp          BIGINT NOT NULL CHECK (mrp > 0),              -- paise
    stock        INT NOT NULL DEFAULT 0 CHECK (stock >= 0),    -- can never go below 0
    images       JSONB NOT NULL DEFAULT '[]'::jsonb,           -- public paths under /uploads/products
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS products_active_idx ON products (is_active, id);

-- India Post pincode -> state (scripts/import_pincodes.py). Empty until imported; a prefix rule is used meanwhile.
CREATE TABLE IF NOT EXISTS pincodes (
    pincode   TEXT PRIMARY KEY CHECK (pincode ~ '^[1-9][0-9]{5}$'),
    state     TEXT NOT NULL,
    district  TEXT
);

-- Admin's courier rate for a pincode. rate NULL = reviewed, use the state default.
-- A pincode used in an order with no row here is on the admin's "rate not set" list.
CREATE TABLE IF NOT EXISTS pincode_rates (
    pincode     TEXT PRIMARY KEY CHECK (pincode ~ '^[1-9][0-9]{5}$'),
    rate        BIGINT CHECK (rate >= 0),
    updated_by  BIGINT REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS order_no_seq START 1001;

CREATE TABLE IF NOT EXISTS orders (
    id               BIGSERIAL PRIMARY KEY,
    order_no         TEXT NOT NULL UNIQUE,                       -- TW-ORD-1001
    user_id          BIGINT NOT NULL REFERENCES users(id),
    status           TEXT NOT NULL DEFAULT 'placed'
                     CHECK (status IN ('placed', 'packed', 'shipped', 'delivered', 'cancelled')),
    items_total      BIGINT NOT NULL CHECK (items_total > 0),    -- MRP of the products (max ₹2,000)
    paid_product     BIGINT NOT NULL DEFAULT 0,                  -- taken from each wallet
    paid_fund        BIGINT NOT NULL DEFAULT 0,
    paid_income      BIGINT NOT NULL DEFAULT 0,
    courier_charge   BIGINT NOT NULL CHECK (courier_charge >= 0),
    courier_state    TEXT,                                       -- from the pincode; NULL = not known
    courier_utr      TEXT NOT NULL,                              -- UPI payment to the company QR
    courier_receipt  TEXT NOT NULL,
    courier_status   TEXT NOT NULL DEFAULT 'pending' CHECK (courier_status IN ('pending', 'approved', 'rejected')),
    courier_remark   TEXT,
    courier_reviewed_by BIGINT REFERENCES users(id),
    courier_rejected_at TIMESTAMPTZ,                             -- start of the 7 days to send a new UTR
    courier_refund_due  BOOLEAN NOT NULL DEFAULT FALSE,          -- cancelled after the courier was paid
    courier_refunded_at TIMESTAMPTZ,
    courier_refund_utr  TEXT,
    address          JSONB NOT NULL,                             -- name, mobile, address, pincode
    courier_name     TEXT,
    tracking_no      TEXT,
    cancel_reason    TEXT,
    cancelled_by     BIGINT REFERENCES users(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    packed_at        TIMESTAMPTZ,
    shipped_at       TIMESTAMPTZ,
    delivered_at     TIMESTAMPTZ,
    cancelled_at     TIMESTAMPTZ,
    CHECK (paid_product + paid_fund + paid_income = items_total)
);
CREATE INDEX IF NOT EXISTS orders_user_idx ON orders (user_id, id DESC);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status, id);
CREATE INDEX IF NOT EXISTS orders_courier_idx ON orders (courier_status, courier_rejected_at) WHERE status = 'placed';
-- A courier UTR can be used once (a rejected one can be sent again on the same order only after a fix).
CREATE UNIQUE INDEX IF NOT EXISTS orders_courier_utr_uq ON orders (courier_utr) WHERE courier_status <> 'rejected';

CREATE TABLE IF NOT EXISTS order_items (
    order_id    BIGINT NOT NULL REFERENCES orders(id),
    product_id  BIGINT NOT NULL REFERENCES products(id),
    name        TEXT NOT NULL,                                 -- as it was when ordered
    mrp         BIGINT NOT NULL,
    qty         INT NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, product_id)
);
