package authd

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"
)

type githubFlowProvider struct {
	flow     OAuthRequest
	verifier string
	calls    int
	err      error
}

func (p *githubFlowProvider) AuthorizationURL(flow OAuthRequest) string {
	p.flow = flow
	return "https://github.test/?state=" + flow.State
}
func (p *githubFlowProvider) Authenticate(_ context.Context, code, verifier string) (Identity, error) {
	p.verifier = verifier
	p.calls++
	return Identity{ID: "12345"}, p.err
}

func TestGitHubFlowIsolationAndReplay(t *testing.T) {
	s, _ := fixture(t)
	s.cfg.Provider = "github"
	p := &githubFlowProvider{}
	s.provider = p
	h := s.Router()
	login := request(h, "https://deepwiki.test/auth/login?return=%2Fchat")
	authorize := request(h, expectRedirect(t, login))
	expectRedirect(t, authorize)
	if len(p.flow.Verifier) != 43 {
		t.Fatal("PKCE missing")
	}
	flow := getCookie(t, authorize, "__Host-auth_flow")
	callbackURL := "https://auth.test/oauth/github/callback?code=code&state=" + p.flow.State
	if w := request(h, callbackURL); w.Code != 400 {
		t.Fatal("unbound callback accepted")
	}
	callback := request(h, callbackURL, flow)
	next := request(h, expectRedirect(t, callback), getCookie(t, login, "__Host-auth_request"))
	if expectRedirect(t, next) != "/chat" {
		t.Fatal("return path lost")
	}
	claims, err := s.tokens.VerifySSO(getCookie(t, callback, SSOCookieName).Value)
	if err != nil || claims.Subject != deterministicSubject("default\x00github", "12345") || claims.Subject == deterministicSubject("default", "12345") {
		t.Fatal("provider namespace collision")
	}
	if p.calls != 1 || p.verifier != p.flow.Verifier {
		t.Fatal("PKCE verifier lost")
	}
	// A valid old SSO cookie still has to go through GitHub and the current list.
	check := request(h, expectRedirect(t, login), getCookie(t, callback, SSOCookieName))
	if !strings.HasPrefix(expectRedirect(t, check), "https://github.test/") {
		t.Fatal("GitHub SSO cookie bypassed allowlist revalidation")
	}
	if w := request(h, callbackURL, flow); w.Code != 400 || p.calls != 1 {
		t.Fatal("replay accepted")
	}
	if w := request(h, "https://auth.test/oauth/feishu/callback?code=old&state=old"); w.Code != http.StatusNotFound {
		t.Fatal("inactive Feishu callback available")
	}
	// Existing Feishu OAuth state cannot authenticate through a new provider.
	raw, err := s.store.CreateOAuthState(context.Background(), OAuthState{AppID: "deepwiki", ReturnPath: "/", Provider: "feishu"}, time.Now().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	w := request(h, "https://auth.test/oauth/github/callback?code=code&state="+raw, &http.Cookie{Name: "__Host-auth_flow", Value: raw})
	if w.Code != 400 || p.calls != 1 {
		t.Fatal("cross-provider state accepted")
	}
}

func TestProviderConfiguration(t *testing.T) {
	for k, v := range map[string]string{"AUTH_PUBLIC_URL": "https://auth.test", "AUTH_APPS_JSON": `{"deepwiki":"https://deepwiki.test"}`, "JWT_PRIVATE_KEY_FILE": "/test/key", "JWT_KID": "key", "AUTH_PROVIDER": "github", "GITHUB_CLIENT_ID": "client", "GITHUB_CLIENT_SECRET": "secret", "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""} {
		t.Setenv(k, v)
	}
	cfg, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := NewIdentityProvider(cfg).(*GitHubProvider); !ok {
		t.Fatal("wrong provider")
	}
	for _, name := range []string{"unknown", "github,feishu"} {
		t.Setenv("AUTH_PROVIDER", name)
		if _, err := LoadConfig(); err == nil {
			t.Fatal("invalid selection accepted")
		}
	}
	t.Setenv("AUTH_PROVIDER", "github")
	t.Setenv("GITHUB_CLIENT_SECRET", "")
	if _, err := LoadConfig(); err == nil || !strings.Contains(err.Error(), "GITHUB") {
		t.Fatal("missing credentials accepted")
	}
	t.Setenv("AUTH_PROVIDER", "feishu")
	t.Setenv("FEISHU_APP_ID", "id")
	t.Setenv("FEISHU_APP_SECRET", "secret")
	cfg, err = LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := NewIdentityProvider(cfg).(*FeishuProvider); !ok {
		t.Fatal("Feishu no longer supported")
	}
}

func TestGitHubRejectedAuthorization(t *testing.T) {
	for _, query := range []string{"error=access_denied&code=code", ""} {
		s, _ := fixture(t)
		s.cfg.Provider = "github"
		p := &githubFlowProvider{}
		s.provider = p
		h := s.Router()
		login := request(h, "https://deepwiki.test/auth/login")
		authorize := request(h, expectRedirect(t, login))
		expectRedirect(t, authorize)
		w := request(h, "https://auth.test/oauth/github/callback?state="+p.flow.State+"&"+query, getCookie(t, authorize, "__Host-auth_flow"))
		if w.Code != 400 || p.calls != 0 {
			t.Fatal("denied or missing code was exchanged")
		}
	}
}

func TestGitHubAllowlistHTTPRejection(t *testing.T) {
	for _, tc := range []struct {
		err    error
		status int
	}{
		{errGitHubNotAllowed, http.StatusForbidden},
		{errGitHubAllowlistUnavailable, http.StatusServiceUnavailable},
	} {
		s, _ := fixture(t)
		s.cfg.Provider = "github"
		p := &githubFlowProvider{err: tc.err}
		s.provider = p
		h := s.Router()
		login := request(h, "https://deepwiki.test/auth/login")
		authorize := request(h, expectRedirect(t, login))
		w := request(h, "https://auth.test/oauth/github/callback?code=code&state="+p.flow.State, getCookie(t, authorize, "__Host-auth_flow"))
		if w.Code != tc.status {
			t.Fatalf("got %d want %d", w.Code, tc.status)
		}
		for _, cookie := range w.Result().Cookies() {
			if cookie.Name == SSOCookieName || cookie.Name == AccessCookieName {
				t.Fatal("rejected login minted cookie")
			}
		}
	}
}
