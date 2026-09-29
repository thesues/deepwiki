package authd

import (
	"strings"
	"testing"
)

func TestProviderConfiguration(t *testing.T) {
	for k, v := range map[string]string{
		"AUTH_PUBLIC_URL":      "https://auth.test",
		"AUTH_APPS_JSON":       `{"deepwiki":"https://deepwiki.test"}`,
		"JWT_PRIVATE_KEY_FILE": "/test/key",
		"JWT_KID":              "key",
		"AUTH_PROVIDER":        "feishu",
		"FEISHU_APP_ID":        "id",
		"FEISHU_APP_SECRET":    "secret",
	} {
		t.Setenv(k, v)
	}
	cfg, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := NewIdentityProvider(cfg).(*FeishuProvider); !ok {
		t.Fatal("wrong provider")
	}
	t.Setenv("AUTH_PROVIDER", "removed")
	if _, err := LoadConfig(); err == nil || !strings.Contains(err.Error(), "unknown AUTH_PROVIDER") {
		t.Fatal("removed provider accepted")
	}
	t.Setenv("AUTH_PROVIDER", "feishu")
	t.Setenv("FEISHU_APP_SECRET", "")
	if _, err := LoadConfig(); err == nil || !strings.Contains(err.Error(), "FEISHU") {
		t.Fatal("missing Feishu credentials accepted")
	}
}
