-- New migration in this PR. Intent: index orders by customer + recency, and
-- add a helper view for "big spenders".
--
-- Bugs planted for the reviewer to catch:
--   * `placed_at` is not a column on orders (it's `created_at`)
--   * the view sums `amount_cents`, but the column is `total_cents`
CREATE INDEX idx_orders_user_placed ON orders (user_id, placed_at DESC);

CREATE VIEW big_spenders AS
SELECT user_id, SUM(amount_cents) AS lifetime_cents
FROM orders
GROUP BY user_id
HAVING SUM(amount_cents) > 100000;
