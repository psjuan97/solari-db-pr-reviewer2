-- New migration in this PR. Intent: a materialized view of revenue per month,
-- plus an index to make the "recent orders" screen fast.
--
-- Bug planted for the reviewer to catch: `orders.placed_at` does not exist -
-- the timestamp column on `orders` is `created_at`.
CREATE MATERIALIZED VIEW monthly_revenue AS
SELECT date_trunc('month', placed_at) AS month,
       SUM(total_cents)               AS revenue_cents,
       COUNT(*)                       AS order_count
FROM orders
GROUP BY 1
ORDER BY 1;

CREATE INDEX idx_orders_recent ON orders (placed_at DESC);
