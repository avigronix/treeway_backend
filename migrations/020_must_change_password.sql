-- An admin set a member's password (Vinay, 28 Oct): the member must choose their own at the next login.
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
