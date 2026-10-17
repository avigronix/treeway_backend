-- Doctor advice (Phase F). Doctors are users with role DOCTOR, created by an admin.

CREATE TABLE IF NOT EXISTS doctor_profiles (
    user_id      BIGINT PRIMARY KEY REFERENCES users(id),
    bams_reg_no  TEXT NOT NULL CHECK (length(trim(bams_reg_no)) >= 3),   -- shown on every prescription
    created_by   BIGINT REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doctor_requests (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id),
    doctor_id    BIGINT REFERENCES users(id),               -- NULL = no active doctor yet
    problem      TEXT NOT NULL,
    files        JSONB NOT NULL DEFAULT '[]'::jsonb,        -- private paths under uploads/health
    status       TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'answered')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_at  TIMESTAMPTZ,
    answered_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS doctor_requests_user_idx ON doctor_requests (user_id, id DESC);
CREATE INDEX IF NOT EXISTS doctor_requests_doctor_idx ON doctor_requests (doctor_id, status, id);
CREATE INDEX IF NOT EXISTS doctor_requests_waiting_idx ON doctor_requests (created_at) WHERE status = 'waiting';

-- A prescription is never changed after it is written (Vinay, 15 Oct): only a new one.
CREATE TABLE IF NOT EXISTS prescriptions (
    id           BIGSERIAL PRIMARY KEY,
    request_id   BIGINT NOT NULL REFERENCES doctor_requests(id),
    doctor_id    BIGINT NOT NULL REFERENCES users(id),
    doctor_name  TEXT NOT NULL,                              -- as it was on the day
    bams_reg_no  TEXT NOT NULL,
    advice       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prescriptions_request_idx ON prescriptions (request_id, id);

CREATE TABLE IF NOT EXISTS prescription_items (
    prescription_id BIGINT NOT NULL REFERENCES prescriptions(id),
    line_no         SMALLINT NOT NULL,
    product_id      BIGINT REFERENCES products(id),          -- NULL = medicine not in the shop (free text)
    name            TEXT NOT NULL,
    dosage          TEXT NOT NULL,                           -- e.g. "morning and evening, after food"
    days            SMALLINT NOT NULL CHECK (days BETWEEN 1 AND 365),
    PRIMARY KEY (prescription_id, line_no)
);

CREATE OR REPLACE FUNCTION prescriptions_read_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'prescriptions cannot be changed; write a new one';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS prescriptions_no_change ON prescriptions;
CREATE TRIGGER prescriptions_no_change BEFORE UPDATE OR DELETE ON prescriptions
    FOR EACH ROW EXECUTE FUNCTION prescriptions_read_only();
DROP TRIGGER IF EXISTS prescription_items_no_change ON prescription_items;
CREATE TRIGGER prescription_items_no_change BEFORE UPDATE OR DELETE ON prescription_items
    FOR EACH ROW EXECUTE FUNCTION prescriptions_read_only();

-- "Order this medicine": the shop order made from a prescription.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS prescription_id BIGINT REFERENCES prescriptions(id);
