package authd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

type FeishuProvider struct {
	appID       string
	secret      string
	redirectURI string
	authURL     string
	tokenURL    string
	userURL     string
	client      *http.Client
}

func NewFeishuProvider(cfg Config) *FeishuProvider {
	return &FeishuProvider{
		appID: cfg.FeishuAppID, secret: cfg.FeishuSecret,
		redirectURI: cfg.PublicURL.ResolveReference(&url.URL{Path: "/oauth/feishu/callback"}).String(),
		authURL:     cfg.FeishuAuthURL, tokenURL: cfg.FeishuTokenURL, userURL: cfg.FeishuUserURL,
		client: &http.Client{Timeout: 10 * time.Second},
	}
}

func (p *FeishuProvider) AuthorizationURL(flow OAuthRequest) string {
	u, _ := url.Parse(p.authURL)
	q := u.Query()
	q.Set("client_id", p.appID)
	q.Set("response_type", "code")
	q.Set("redirect_uri", p.redirectURI)
	q.Set("state", flow.State)
	u.RawQuery = q.Encode()
	return u.String()
}

func (p *FeishuProvider) Authenticate(ctx context.Context, code string) (Identity, error) {
	body, _ := json.Marshal(map[string]string{
		"grant_type":    "authorization_code",
		"client_id":     p.appID,
		"client_secret": p.secret,
		"code":          code,
		"redirect_uri":  p.redirectURI,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.tokenURL, bytes.NewReader(body))
	if err != nil {
		return Identity{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		return Identity{}, fmt.Errorf("exchange Feishu code: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return Identity{}, fmt.Errorf("Feishu token endpoint returned %d", resp.StatusCode)
	}
	var token struct {
		Code        int    `json:"code"`
		Message     string `json:"message"`
		AccessToken string `json:"access_token"`
		Data        struct {
			AccessToken string `json:"access_token"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&token); err != nil {
		return Identity{}, fmt.Errorf("decode Feishu token: %w", err)
	}
	if token.AccessToken == "" {
		token.AccessToken = token.Data.AccessToken
	}
	if token.Code != 0 || token.AccessToken == "" {
		return Identity{}, fmt.Errorf("Feishu token rejected: code=%d message=%s", token.Code, token.Message)
	}

	req, err = http.NewRequestWithContext(ctx, http.MethodGet, p.userURL, nil)
	if err != nil {
		return Identity{}, err
	}
	req.Header.Set("Authorization", "Bearer "+token.AccessToken)
	resp, err = p.client.Do(req)
	if err != nil {
		return Identity{}, fmt.Errorf("read Feishu identity: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return Identity{}, fmt.Errorf("Feishu user endpoint returned %d", resp.StatusCode)
	}
	var user struct {
		Code    int    `json:"code"`
		Message string `json:"msg"`
		UnionID string `json:"union_id"`
		OpenID  string `json:"open_id"`
		Data    struct {
			UnionID string `json:"union_id"`
			OpenID  string `json:"open_id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&user); err != nil {
		return Identity{}, fmt.Errorf("decode Feishu identity: %w", err)
	}
	if user.UnionID == "" {
		user.UnionID = user.Data.UnionID
	}
	if user.OpenID == "" {
		user.OpenID = user.Data.OpenID
	}
	if user.Code != 0 || (user.UnionID == "" && user.OpenID == "") {
		return Identity{}, errors.New("Feishu identity did not contain union_id or open_id")
	}
	id := user.UnionID
	if id == "" {
		id = user.OpenID
	}
	return Identity{ID: id}, nil
}
