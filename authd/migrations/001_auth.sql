-- +goose Up
CREATE TABLE oauth_states (
    state_hash  BLOB PRIMARY KEY,
    app_id      TEXT NOT NULL,
    return_path TEXT NOT NULL,
    browser_hash TEXT NOT NULL,
    expires_at  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE INDEX oauth_states_expires_at_idx ON oauth_states(expires_at);

CREATE TABLE auth_codes (
    code_hash   BLOB PRIMARY KEY,
    subject     TEXT NOT NULL,
    app_id      TEXT NOT NULL,
    target_host TEXT NOT NULL,
    return_path TEXT NOT NULL,
    browser_hash TEXT NOT NULL,
    expires_at  INTEGER NOT NULL,
    consumed_at INTEGER
);

CREATE INDEX auth_codes_expires_at_idx ON auth_codes(expires_at);

-- +goose Down
DROP TABLE auth_codes;
DROP TABLE oauth_states;
