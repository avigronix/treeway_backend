-- BAMS registration number is optional (Vinay, 29 Oct). The CHECK (>= 3 letters) stays: NULL passes it.
ALTER TABLE doctor_profiles ALTER COLUMN bams_reg_no DROP NOT NULL;
ALTER TABLE prescriptions ALTER COLUMN bams_reg_no DROP NOT NULL;
