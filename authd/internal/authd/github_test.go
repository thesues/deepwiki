package authd

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGitHubExchangeAndIdentity(t *testing.T) {
	var tokenCalls, userCalls int
	verifier := strings.Repeat("v", 43)
	userResponse := `{"id":12345,"login":"before-rename","type":"User"}`
	tokenResponse := `{"access_token":"test-token","token_type":"bearer"}`
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Accept") != "application/json" || r.Header.Get("User-Agent") == "" {
			t.Error("missing required API headers")
		}
		switch r.URL.Path {
		case "/token":
			tokenCalls++
			r.ParseForm()
			if r.Method != "POST" || r.Form.Get("client_secret") != "test-secret" || r.Form.Get("code_verifier") != verifier || r.Form.Get("redirect_uri") != "https://auth.test/oauth/github/callback" {
				t.Error("incorrect token exchange")
			}
			fmt.Fprint(w, tokenResponse)
		case "/user":
			userCalls++
			if r.Header.Get("Authorization") != "Bearer test-token" {
				t.Error("access token missing")
			}
			fmt.Fprint(w, userResponse)
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	public, _ := url.Parse("https://auth.test")
	p := NewGitHubProvider(Config{PublicURL: public, GitHubClientID: "client", GitHubSecret: "test-secret"})
	p.allowlistFile = filepath.Join(t.TempDir(), "allowlist.txt")
	if err := os.WriteFile(p.allowlistFile, []byte("before-rename\nafter-rename\n"), 0600); err != nil {
		t.Fatal(err)
	}
	p.tokenURL = upstream.URL + "/token"
	p.userURL = upstream.URL + "/user"
	target, _ := url.Parse(p.AuthorizationURL(OAuthRequest{State: "state", Verifier: verifier}))
	q := target.Query()
	if q.Get("scope") != "" || q.Get("state") != "state" || q.Get("client_id") != "client" || q.Get("code_challenge_method") != "S256" || q.Get("code_challenge") != base64.RawURLEncoding.EncodeToString(tokenHash(verifier)) {
		t.Fatal("incorrect authorization/PKCE parameters")
	}
	identity, err := p.Authenticate(context.Background(), "code", verifier)
	if err != nil || identity.ID != "12345" || tokenCalls != 1 || userCalls != 1 {
		t.Fatalf("exchange: %+v %v", identity, err)
	}
	userResponse = `{"id":12345,"login":"after-rename","type":"User"}`
	next, err := p.Authenticate(context.Background(), "code", verifier)
	if err != nil || identity != next {
		t.Fatal("username change changed identity")
	}
	userResponse = `{"id":12345,"login":"not-listed","type":"User"}`
	if _, err := p.Authenticate(context.Background(), "code", verifier); !errors.Is(err, errGitHubNotAllowed) {
		t.Fatalf("unlisted username accepted: %v", err)
	}
	for _, body := range []string{`{"id":0,"type":"User"}`, `{"id":12345,"type":"Bot"}`, `{"id":-1,"type":"User"}`, `{"id":12345.1,"type":"User"}`, `not json`} {
		userResponse = body
		if _, err := p.Authenticate(context.Background(), "code", verifier); err == nil {
			t.Fatalf("invalid user accepted: %s", body)
		}
	}
	for _, body := range []string{`{"error":"bad_verification_code","error_description":"must-not-log"}`, `{"access_token":"test-token","token_type":"unknown"}`, `{}`} {
		tokenResponse = body
		before := userCalls
		if _, err := p.Authenticate(context.Background(), "code", verifier); err == nil || strings.Contains(err.Error(), "must-not-log") || userCalls != before {
			t.Fatal("invalid token response accepted or leaked")
		}
	}
	before := tokenCalls
	if _, err := p.Authenticate(context.Background(), "code", ""); err == nil || tokenCalls != before {
		t.Fatal("missing PKCE accepted")
	}
}

func TestGitHubRejectsRedirects(t *testing.T) {
	forwarded := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { forwarded = true }))
	defer target.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer upstream.Close()
	public, _ := url.Parse("https://auth.test")
	p := NewGitHubProvider(Config{PublicURL: public, GitHubSecret: "must-not-leak"})
	p.tokenURL = upstream.URL
	_, err := p.Authenticate(context.Background(), "code", strings.Repeat("v", 43))
	if err == nil || forwarded || strings.Contains(err.Error(), "must-not-leak") {
		t.Fatal("credential redirect not rejected")
	}
}
