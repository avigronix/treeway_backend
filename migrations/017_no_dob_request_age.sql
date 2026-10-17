-- Bhaiya, 16 Oct 2026: date of birth is no longer asked (sign-up, profile, admin). The column stays, so old
-- members keep their data; nothing writes it any more. Only doctor requests made before today read it.
COMMENT ON COLUMN users.dob IS 'Not asked since 16 Oct 2026 (bhaiya). Kept for old data; only old doctor requests read it.';

-- "Ask a doctor" asks the age in years with every request (1 to 120); the doctor sees that age.
-- Old requests have none: the doctor sees the age from the old date of birth, as before.
ALTER TABLE doctor_requests ADD COLUMN IF NOT EXISTS age SMALLINT CHECK (age BETWEEN 1 AND 120);
