-- Gullak interest is 12% a year (plan; Vinay 12 Nov). The default moves to 12 in base.py. A saved `gullak` setting
-- that still has the old default 10 moves to 12 too; any other rate an admin chose is kept. Each deposit keeps the
-- rate it was made at (gullak_deposits.interest_percent), so no running deposit changes.
UPDATE app_settings SET value = jsonb_set(value, '{interest_percent}', '12')
 WHERE key = 'gullak' AND value->>'interest_percent' = '10';
