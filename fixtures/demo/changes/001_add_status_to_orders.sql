-- A clean migration: add a status column with a safe default.
ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';

UPDATE orders SET status = 'paid' WHERE total_cents > 1000;
