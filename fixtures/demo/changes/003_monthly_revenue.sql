-- New migration in this PR. Intent: a materialized view of revenue per month,
-- plus an index to make the "recent orders" screen fast.
--
-- Fix: the timestamp column on `orders` is `created_at`, not `placed_at`.
CREATE MATERIALIZED VIEW monthly_revenue AS
SELECT date_trunc('month', created_at) AS month,
       SUM(total_cents)               AS revenue_cents,
       COUNT(*)                       AS order_count
FROM orders
GROUP BY 1
ORDER BY 1;

CREATE INDEX idx_orders_recent ON orders (created_at DESC);
