package authd

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dongmaoz/buda/authd/migrations"
	"github.com/golang-jwt/jwt/v5"
	_ "github.com/mattn/go-sqlite3"
	"github.com/pressly/goose/v3"
)

type fakeProvider struct{ calls int }

func (p *fakeProvider) AuthorizationURL(flow OAuthRequest) string {
	return "https://feishu.test/?state=" + flow.State
}
func (p *fakeProvider) Authenticate(_ context.Context, code string) (Identity, error) {
	p.calls++
	return Identity{ID: "union-1"}, nil
}

func database(t *testing.T) (*sql.DB, *SQLStore) {
	t.Helper()
	conn, err := sql.Open("sqlite3", "file:"+filepath.Join(t.TempDir(), "auth.db")+"?_busy_timeout=5000&_journal_mode=WAL")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	goose.SetBaseFS(migrations.Files)
	if err := goose.SetDialect("sqlite3"); err != nil {
		t.Fatal(err)
	}
	if err := goose.Up(conn, "."); err != nil {
		t.Fatal(err)
	}
	return conn, NewSQLStore(conn)
}
func fixture(t *testing.T) (*Server, *fakeProvider) {
	t.Helper()
	_, store := database(t)
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	public, _ := url.Parse("https://auth.test")
	cfg := Config{PublicURL: public, PublicHost: public.Host, TenantKey: "default", Apps: map[string]App{}, AppsByHost: map[string]App{}}
	for _, id := range []string{"deepwiki", "lerobot"} {
		app := App{ID: id, Host: id + ".test", Origin: "https://" + id + ".test"}
		cfg.Apps[id] = app
		cfg.AppsByHost[app.Host] = app
	}
	provider := &fakeProvider{}
	return NewServer(cfg, store, provider, NewTokenManager(key, "one", "default"), nil), provider
}
func request(h http.Handler, raw string, cookies ...*http.Cookie) *httptest.ResponseRecorder {
	r := httptest.NewRequest("GET", raw, nil)
	for _, c := range cookies {
		r.AddCookie(c)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func getCookie(t *testing.T, w *httptest.ResponseRecorder, name string) *http.Cookie {
	t.Helper()
	for _, c := range w.Result().Cookies() {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("cookie %s missing", name)
	return nil
}
func expectRedirect(t *testing.T, w *httptest.ResponseRecorder) string {
	t.Helper()
	if w.Code != 302 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	return w.Header().Get("Location")
}
func TestSSOTwoServicesAndReplay(t *testing.T) {
	s, p := fixture(t)
	h := s.Router()
	login := request(h, "https://deepwiki.test/auth/login?return=%2Fbuda%2F")
	appNonce := getCookie(t, login, "__Host-auth_request")
	authorize := request(h, expectRedirect(t, login))
	upstream, _ := url.Parse(expectRedirect(t, authorize))
	state := upstream.Query().Get("state")
	flow := getCookie(t, authorize, "__Host-auth_flow")
	callbackURL := "https://auth.test/oauth/feishu/callback?code=feishu-code&state=" + state
	if w := request(h, callbackURL); w.Code != 400 {
		t.Fatalf("unbound OAuth callback accepted: %d", w.Code)
	}
	callback := request(h, callbackURL, flow)
	sso := getCookie(t, callback, SSOCookieName)
	appURL := expectRedirect(t, callback)
	if w := request(h, appURL); w.Code != 400 {
		t.Fatal("unbound application callback accepted")
	}
	wrongURL, _ := url.Parse(appURL)
	wrongURL.Host = "lerobot.test"
	if w := request(h, wrongURL.String(), appNonce); w.Code != 400 {
		t.Fatal("code accepted on another host")
	}
	appCallback := request(h, appURL, appNonce)
	if got := expectRedirect(t, appCallback); got != "/buda/" {
		t.Fatal(got)
	}
	access := getCookie(t, appCallback, AccessCookieName)
	if !access.Secure || !access.HttpOnly || access.Domain != "" || access.Path != "/" || access.SameSite != http.SameSiteLaxMode || access.MaxAge != 28800 {
		t.Fatalf("cookie attributes: %+v", access)
	}
	claims := &Claims{}
	_, err := jwt.ParseWithClaims(access.Value, claims, func(*jwt.Token) (any, error) { return &s.tokens.private.PublicKey, nil }, jwt.WithAudience("deepwiki"), jwt.WithIssuer(Issuer))
	if err != nil || claims.Subject != deterministicSubject("default", "union-1") {
		t.Fatalf("claims: %+v %v", claims, err)
	}
	if w := request(h, appURL, appNonce); w.Code != 400 {
		t.Fatal("code replay accepted")
	}
	if w := request(h, callbackURL, flow); w.Code != 400 {
		t.Fatal("OAuth state replay accepted")
	}
	second := request(h, "https://lerobot.test/auth/login")
	next := request(h, expectRedirect(t, second), sso)
	final := request(h, expectRedirect(t, next), getCookie(t, second, "__Host-auth_request"))
	expectRedirect(t, final)
	if p.calls != 1 {
		t.Fatalf("SSO called Feishu %d times", p.calls)
	}
	token := getCookie(t, final, AccessCookieName)
	if _, err := jwt.Parse(token.Value, func(*jwt.Token) (any, error) { return &s.tokens.private.PublicKey, nil }, jwt.WithAudience("deepwiki")); err == nil {
		t.Fatal("LeRobot token accepted by DeepWiki")
	}
}
func TestCodesExpireAndConsumeAtomically(t *testing.T) {
	_, store := database(t)
	ctx := context.Background()
	now := time.Now()
	code := AuthCode{Subject: "u_one", AppID: "deepwiki", TargetHost: "deepwiki.test", ReturnPath: "/", BrowserHash: "browser"}
	raw, err := store.CreateAuthCode(ctx, code, now.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	var successes atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := store.ConsumeAuthCode(ctx, raw, code.AppID, code.TargetHost, code.BrowserHash, now); err == nil {
				successes.Add(1)
			}
		}()
	}
	wg.Wait()
	if successes.Load() != 1 {
		t.Fatalf("consumed %d times", successes.Load())
	}
	expired, _ := store.CreateAuthCode(ctx, code, now.Add(-time.Second))
	if _, err := store.ConsumeAuthCode(ctx, expired, code.AppID, code.TargetHost, code.BrowserHash, now); err == nil {
		t.Fatal("expired code accepted")
	}
}
func TestReturnPathsAndUnknownHosts(t *testing.T) {
	s, _ := fixture(t)
	h := s.Router()
	for _, raw := range []string{"https://evil.test/", "//evil.test/", "/\\evil.test/", "/%2f/evil.test/", "/auth/login"} {
		if cleanReturnPath(raw) != "/" {
			t.Fatalf("unsafe return: %q", raw)
		}
	}
	if w := request(h, "https://evil.test/auth/login"); w.Code != 400 {
		t.Fatal("unknown host accepted")
	}
	if w := request(h, "https://deepwiki.test/sso/authorize?app=deepwiki"); w.Code != 400 {
		t.Fatal("SSO served on app host")
	}
}
func TestRotationRetainsPublicKeys(t *testing.T) {
	old, _ := rsa.GenerateKey(rand.Reader, 2048)
	next, _ := rsa.GenerateKey(rand.Reader, 2048)
	first := NewTokenManager(old, "old", "default")
	second := NewTokenManager(next, "new", "default")
	raw, _ := json.Marshal(first.JWKS())
	path := filepath.Join(t.TempDir(), "previous.json")
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err := second.LoadPreviousJWKS(path); err != nil {
		t.Fatal(err)
	}
	token, _ := first.Mint("u_one", SSOAudience)
	if _, err := second.VerifySSO(token); err != nil {
		t.Fatal(err)
	}
	if len(second.JWKS()["keys"].([]JWK)) != 2 {
		t.Fatal("old public key missing")
	}
}
func TestDatabaseContainsOnlyHashes(t *testing.T) {
	conn, store := database(t)
	ctx := context.Background()
	now := time.Now()
	browser := base64.RawURLEncoding.EncodeToString(tokenHash("browser"))
	raw, _ := store.CreateOAuthState(ctx, OAuthState{AppID: "deepwiki", ReturnPath: "/", BrowserHash: browser, Provider: "feishu"}, now.Add(time.Minute))
	var saved []byte
	if err := conn.QueryRow("SELECT state_hash FROM oauth_states").Scan(&saved); err != nil {
		t.Fatal(err)
	}
	if string(saved) == raw || len(saved) != 32 {
		t.Fatal("state was not SHA256 hashed")
	}
}
