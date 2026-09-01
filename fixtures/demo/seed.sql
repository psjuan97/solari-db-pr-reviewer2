-- Optional representative data, loaded into the known state after schema.sql.
-- Every fork (the reviewer's and the fix agent's) starts from this, so
-- "runs cleanly" is checked against a populated table, not an empty one.
INSERT INTO users (email)
SELECT 'user' || g || '@example.com'
FROM generate_series(3, 500) g;

INSERT INTO orders (user_id, total_cents)
SELECT (random() * 497 + 1)::int, (random() * 20000)::int
FROM generate_series(1, 5000);
