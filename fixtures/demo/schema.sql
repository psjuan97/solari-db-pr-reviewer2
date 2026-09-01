-- Base schema the PR's changes run against.
CREATE TABLE users (
    id         SERIAL PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    total_cents INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (email) VALUES ('a@example.com'), ('b@example.com');
INSERT INTO orders (user_id, total_cents) VALUES (1, 1200), (1, 800), (2, 5000);
