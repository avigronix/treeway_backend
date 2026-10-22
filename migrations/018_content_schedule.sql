-- Scheduling (Vinay, 22 Oct 2026): a start time for each day's videos, start / end date + time for banners.

-- The day's videos go live at this IST time. No row = 12:00 AM (as before).
CREATE TABLE IF NOT EXISTS video_day_times (
    video_date  DATE PRIMARY KEY,
    start_time  TIME NOT NULL,
    updated_by  BIGINT REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Banners: shown while starts_at <= now < ends_at (either may be empty). The old date-only columns are copied
-- (start 12:00 AM, end 11:59 PM IST) and then left empty; nothing reads them any more.
ALTER TABLE banners ADD COLUMN IF NOT EXISTS starts_at TIMESTAMPTZ;
ALTER TABLE banners ADD COLUMN IF NOT EXISTS ends_at TIMESTAMPTZ;
UPDATE banners SET starts_at = starts_on::timestamp AT TIME ZONE 'Asia/Kolkata'
 WHERE starts_on IS NOT NULL AND starts_at IS NULL;
UPDATE banners SET ends_at = (ends_on + time '23:59') AT TIME ZONE 'Asia/Kolkata'
 WHERE ends_on IS NOT NULL AND ends_at IS NULL;
UPDATE banners SET starts_on = NULL, ends_on = NULL WHERE starts_on IS NOT NULL OR ends_on IS NOT NULL;
ALTER TABLE banners ADD CONSTRAINT banners_times_order CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at);
