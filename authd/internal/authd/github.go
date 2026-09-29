package authd

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type GitHubProvider struct {
	clientID, secret, redirectURI string
	allowlistFile                 string
	authURL, tokenURL, userURL    string
	client                        *http.Client
}

func NewGitHubProvider(cfg Config) *GitHubProvider {
	return &GitHubProvider{
		clientID: cfg.GitHubClientID, secret: cfg.GitHubSecret,
		allowlistFile: cfg.GitHubAllowlistFile,
		redirectURI:   cfg.PublicURL.ResolveReference(&url.URL{Path: "/oauth/github/callback"}).String(),
		authURL:       "https://github.com/login/oauth/authorize", tokenURL: "https://github.com/login/oauth/access_token", userURL: "https://api.github.com/user",
		client: &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
	}
}
func (p *GitHubProvider) AuthorizationURL(flow OAuthRequest) string {
	q := url.Values{"client_id": {p.clientID}, "redirect_uri": {p.redirectURI}, "state": {flow.State}, "scope": {""}, "code_challenge_method": {"S256"}, "code_challenge": {base64.RawURLEncoding.EncodeToString(tokenHash(flow.Verifier))}}
	// No scopes: public account identity only; never request repository or email access.
	return p.authURL + "?" + q.Encode()
}
func (p *GitHubProvider) Authenticate(ctx context.Context, code, verifier string) (Identity, error) {
	ctx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	if code == "" || len(verifier) < 43 || len(verifier) > 128 {
		return Identity{}, errors.New("invalid GitHub authorization flow")
	}
	body := url.Values{"client_id": {p.clientID}, "client_secret": {p.secret}, "code": {code}, "redirect_uri": {p.redirectURI}, "code_verifier": {verifier}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.tokenURL, strings.NewReader(body.Encode()))
	if err != nil {
		return Identity{}, errors.New("invalid GitHub token endpoint")
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	var token struct {
		AccessToken string `json:"access_token"`
		TokenType   string `json:"token_type"`
		Error       string `json:"error"`
	}
	if err = p.readJSON(req, &token); err != nil {
		return Identity{}, err
	}
	if token.Error != "" || token.AccessToken == "" || !strings.EqualFold(token.TokenType, "bearer") {
		return Identity{}, errors.New("GitHub token exchange rejected")
	}
	req, err = http.NewRequestWithContext(ctx, http.MethodGet, p.userURL, nil)
	if err != nil {
		return Identity{}, errors.New("invalid GitHub user endpoint")
	}
	req.Header.Set("Authorization", "Bearer "+token.AccessToken)
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	var user struct {
		ID    int64  `json:"id"`
		Type  string `json:"type"`
		Login string `json:"login"`
	}
	if err = p.readJSON(req, &user); err != nil {
		return Identity{}, err
	}
	if user.ID <= 0 || user.Type != "User" {
		return Identity{}, errors.New("invalid GitHub user identity")
	}
	if err := checkGitHubAllowlist(p.allowlistFile, user.Login); err != nil {
		return Identity{}, err
	}
	// Numeric GitHub IDs survive username changes; never key isolation by login/email.
	return Identity{ID: strconv.FormatInt(user.ID, 10)}, nil
}
func (p *GitHubProvider) readJSON(req *http.Request, out any) error {
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "authd")
	resp, err := p.client.Do(req)
	if err != nil {
		return errors.New("GitHub request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("GitHub endpoint %s returned HTTP %d", req.URL.Path, resp.StatusCode)
	}
	if err = json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out); err != nil {
		return errors.New("invalid GitHub JSON response")
	}
	return nil
}
