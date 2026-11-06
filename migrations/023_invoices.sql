-- Invoice for every shop order (Vinay, 6 Nov): non-GST for now; the number is given when the order is placed,
-- one series per financial year (April to March, India time): TW/INV/2026-27/000001. A number is never reused;
-- a cancelled order keeps its invoice (marked cancelled).
CREATE TABLE IF NOT EXISTS invoice_counters (
    fy       TEXT PRIMARY KEY,               -- '2026-27'
    last_no  INT NOT NULL CHECK (last_no > 0)
);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_no TEXT UNIQUE;

-- Orders placed before this: numbered in the order they were placed, in their own financial year.
WITH x AS (
    SELECT id, created_at,
           CASE WHEN extract(month FROM created_at AT TIME ZONE 'Asia/Kolkata') >= 4
                THEN extract(year FROM created_at AT TIME ZONE 'Asia/Kolkata')::int
                ELSE extract(year FROM created_at AT TIME ZONE 'Asia/Kolkata')::int - 1 END AS fy_start
    FROM orders WHERE invoice_no IS NULL
), n AS (
    SELECT id, fy_start::text || '-' || lpad(((fy_start + 1) % 100)::text, 2, '0') AS fy,
           row_number() OVER (PARTITION BY fy_start ORDER BY created_at, id) AS no
    FROM x
)
UPDATE orders o SET invoice_no = 'TW/INV/' || n.fy || '/' || lpad(n.no::text, 6, '0') FROM n WHERE o.id = n.id;

INSERT INTO invoice_counters (fy, last_no)
SELECT split_part(invoice_no, '/', 3), max(split_part(invoice_no, '/', 4)::int) FROM orders GROUP BY 1
ON CONFLICT (fy) DO UPDATE SET last_no = GREATEST(invoice_counters.last_no, EXCLUDED.last_no);

ALTER TABLE orders ALTER COLUMN invoice_no SET NOT NULL;
