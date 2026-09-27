package authd

import (
	"context"
	"database/sql"
	"time"

	dbgen "github.com/dongmaoz/buda/authd/internal/db"
)

type OAuthState struct {
	AppID       string
	ReturnPath  string
	BrowserHash string
}

type AuthCode struct {
	Subject     string
	AppID       string
	TargetHost  string
	ReturnPath  string
	BrowserHash string
}

type Store interface {
	CreateOAuthState(context.Context, string, string, string, time.Time) (string, error)
	ConsumeOAuthState(context.Context, string, time.Time) (OAuthState, error)
	CreateAuthCode(context.Context, AuthCode, time.Time) (string, error)
	ConsumeAuthCode(context.Context, string, string, string, string, time.Time) (AuthCode, error)
	DeleteExpired(context.Context, time.Time) error
}

type SQLStore struct{ q *dbgen.Queries }

func NewSQLStore(conn *sql.DB) *SQLStore { return &SQLStore{q: dbgen.New(conn)} }

func (s *SQLStore) CreateOAuthState(ctx context.Context, appID, returnPath, browserHash string, expires time.Time) (string, error) {
	raw := randomString(32)
	now := time.Now().Unix()
	err := s.q.CreateOAuthState(ctx, dbgen.CreateOAuthStateParams{
		StateHash: tokenHash(raw), AppID: appID, ReturnPath: returnPath, BrowserHash: browserHash,
		ExpiresAt: expires.Unix(), CreatedAt: now,
	})
	return raw, err
}

func (s *SQLStore) ConsumeOAuthState(ctx context.Context, raw string, now time.Time) (OAuthState, error) {
	row, err := s.q.ConsumeOAuthState(ctx, dbgen.ConsumeOAuthStateParams{
		StateHash: tokenHash(raw), ExpiresAt: now.Unix(),
	})
	return OAuthState{AppID: row.AppID, ReturnPath: row.ReturnPath, BrowserHash: row.BrowserHash}, err
}

func (s *SQLStore) CreateAuthCode(ctx context.Context, code AuthCode, expires time.Time) (string, error) {
	raw := randomString(32)
	err := s.q.CreateAuthCode(ctx, dbgen.CreateAuthCodeParams{
		CodeHash: tokenHash(raw), Subject: code.Subject, AppID: code.AppID,
		TargetHost: code.TargetHost, ReturnPath: code.ReturnPath, BrowserHash: code.BrowserHash, ExpiresAt: expires.Unix(),
	})
	return raw, err
}

func (s *SQLStore) ConsumeAuthCode(ctx context.Context, raw, appID, host, browserHash string, now time.Time) (AuthCode, error) {
	row, err := s.q.ConsumeAuthCode(ctx, dbgen.ConsumeAuthCodeParams{
		ConsumedAt: sql.NullInt64{Int64: now.Unix(), Valid: true},
		CodeHash:   tokenHash(raw), ExpiresAt: now.Unix(), AppID: appID, TargetHost: host, BrowserHash: browserHash,
	})
	return AuthCode{
		Subject: row.Subject, AppID: row.AppID, TargetHost: row.TargetHost, ReturnPath: row.ReturnPath,
	}, err
}

func (s *SQLStore) DeleteExpired(ctx context.Context, now time.Time) error {
	if _, err := s.q.DeleteExpired(ctx, now.Unix()); err != nil {
		return err
	}
	_, err := s.q.DeleteExpiredCodes(ctx, now.Unix())
	return err
}
