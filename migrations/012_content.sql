-- Home / login banners and "Contact us" messages (Phase G).
CREATE TABLE IF NOT EXISTS banners (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    image_path  TEXT NOT NULL,                          -- public: /uploads/banners/...
    link_url    TEXT,
    audience    TEXT NOT NULL DEFAULT 'everyone' CHECK (audience IN ('everyone', 'login')),
    starts_on   DATE,                                   -- IST, inclusive; NULL = from now
    ends_on     DATE,                                   -- IST, inclusive; NULL = no end
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order  INT NOT NULL DEFAULT 0,
    created_by  BIGINT REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends_on IS NULL OR starts_on IS NULL OR ends_on >= starts_on)
);
CREATE INDEX IF NOT EXISTS banners_live_idx ON banners (audience, sort_order, id) WHERE is_active;

CREATE TABLE IF NOT EXISTS contact_messages (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    mobile      TEXT NOT NULL,
    message     TEXT NOT NULL,
    ip          TEXT,
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'replied')),
    reply_note  TEXT,
    replied_by  BIGINT REFERENCES users(id),
    replied_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contact_messages_status_idx ON contact_messages (status, id);
CREATE INDEX IF NOT EXISTS contact_messages_mobile_idx ON contact_messages (mobile, created_at);
