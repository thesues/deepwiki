package authd

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestFeishuV2Exchange(t *testing.T) {
	tokenCalls, userCalls := 0, 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/token":
			tokenCalls++
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if r.Method != "POST" || body["client_id"] != "client" || body["client_secret"] != "secret" || body["code"] != "code" || body["grant_type"] != "authorization_code" || body["redirect_uri"] != "https://auth.test/oauth/feishu/callback" {
				t.Errorf("unexpected token request")
			}
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"code":0,"access_token":"temporary-token"}`))
		case "/user":
			userCalls++
			if r.Header.Get("Authorization") != "Bearer temporary-token" {
				t.Error("missing user access token")
			}
			w.Write([]byte(`{"code":0,"data":{"union_id":"union","open_id":"open"}}`))
		default:
			w.WriteHeader(404)
		}
	}))
	defer upstream.Close()
	public, _ := url.Parse("https://auth.test")
	provider := NewFeishuProvider(Config{PublicURL: public, FeishuAppID: "client", FeishuSecret: "secret", FeishuAuthURL: "https://accounts.feishu.cn/open-apis/authen/v1/authorize", FeishuTokenURL: upstream.URL + "/token", FeishuUserURL: upstream.URL + "/user"})
	target, _ := url.Parse(provider.AuthorizationURL(OAuthRequest{State: "state"}))
	if target.Query().Get("client_id") != "client" || target.Query().Get("response_type") != "code" || target.Query().Get("state") != "state" {
		t.Fatal("invalid Feishu authorize URL")
	}
	identity, err := provider.Authenticate(context.Background(), "code")
	if err != nil || identity.ID != "union" || tokenCalls != 1 || userCalls != 1 {
		t.Fatalf("exchange failed: %+v %v", identity, err)
	}
}
