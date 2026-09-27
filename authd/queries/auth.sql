-- name: CreateOAuthState :exec
INSERT INTO oauth_states (state_hash, app_id, return_path, browser_hash, expires_at, created_at)
VALUES (?, ?, ?, ?, ?, ?);

-- name: ConsumeOAuthState :one
DELETE FROM oauth_states
WHERE state_hash = ? AND expires_at >= ?
RETURNING state_hash, app_id, return_path, browser_hash, expires_at, created_at;

-- name: CreateAuthCode :exec
INSERT INTO auth_codes (
    code_hash, subject, app_id, target_host, return_path, browser_hash, expires_at, consumed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, NULL);

-- name: ConsumeAuthCode :one
UPDATE auth_codes
SET consumed_at = ?
WHERE code_hash = ? AND consumed_at IS NULL AND expires_at >= ?
AND app_id = ? AND target_host = ? AND browser_hash = ?
RETURNING code_hash, subject, app_id, target_host, return_path, browser_hash, expires_at, consumed_at;

-- name: DeleteExpired :execrows
DELETE FROM oauth_states WHERE expires_at < ?;

-- name: DeleteExpiredCodes :execrows
DELETE FROM auth_codes WHERE expires_at < ?;
