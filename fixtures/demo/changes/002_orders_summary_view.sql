-- A broken change: references a column that does not exist (orders.amount_cents
-- - the real column is total_cents) and groups by a missing users.name.
CREATE VIEW order_summary AS
SELECT u.name        AS customer,
       COUNT(o.id)   AS order_count,
       SUM(o.amount_cents) AS total_spent
FROM users u
JOIN orders o ON o.user_id = u.id
GROUP BY u.name;
